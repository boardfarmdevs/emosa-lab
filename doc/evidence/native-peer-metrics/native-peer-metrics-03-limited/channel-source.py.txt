"""Selected sole-radio channel procedures (EasyMesh 6.1 §§8.1–2, 15.1).

The initial radio profile supports only class 81/channel 6. Compatible requests
need no actuator change; their acceptance still requires durable preferences and
fresh observed operating parameters. This is not a generic channel actuator.
"""

import json
import sqlite3
import time
from dataclasses import dataclass, field

from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import SECURITY_ENVELOPES
from emosa.wire.cmdu import Reassembler, Tlv, fragment_message, invalid
from emosa.wire.reports import PreparedReport


@dataclass(frozen=True)
class OperatingRadio:
    ruid: bytes
    operating_class: int
    channel: int
    tx_power_dbm: int

    def __post_init__(self):
        if (
            type(self.ruid) is not bytes
            or len(self.ruid) != 6
            or not any(self.ruid)
            or self.ruid[0] & 1
            or self.operating_class != 81
            or self.channel != 6
            or type(self.tx_power_dbm) is not int
            or not -128 <= self.tx_power_dbm <= 127
        ):
            invalid("operating parameters outside the selected sole-radio contract")

    def tlv(self):
        return Tlv(0x8F, self.ruid + bytes((1, 81, 6, self.tx_power_dbm & 255)))


class ChannelPolicyStore:
    """One durable, bounded record per owned virtual-agent worker.

    Reboot reset is permitted by §8.2. It is explicit and recorded, rather than
    mistaking a recovered policy for evidence that the radio applied it.
    """

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS channel_policy "
            "(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)"
        )

    def save(self, value):
        encoded = json.dumps(value, sort_keys=True)
        if len(encoded) > 8192:
            invalid("channel policy exceeds durable record budget")
        with self.db:
            self.db.execute(
                "INSERT INTO channel_policy VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                (encoded,),
            )

    def read(self):
        row = self.db.execute("SELECT value FROM channel_policy WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()


def selected_policy(tlvs, radio):
    """Validate the complete selected request before persisting or accepting it.

    A contradictory request excluding the only advertised operable channel is
    outside this controller/profile contract. It must not replace a usable policy
    and then silently keep operating on a newly forbidden channel.
    """
    preferences, power = None, None
    for tlv in tlvs:
        if tlv.kind == 0x8B:
            if preferences is not None or len(tlv.value) < 7 or tlv.value[:6] != radio.ruid:
                invalid("duplicate, truncated or foreign channel preferences")
            count, body = tlv.value[6], tlv.value[7:]
            if count > 8:
                invalid("channel preference group budget exceeded")
            preferences, specified = [], set()
            for _ in range(count):
                if len(body) < 3:
                    invalid("truncated channel preference group")
                opclass, n = body[:2]
                if n > 13 or len(body) < 3 + n:
                    invalid("invalid channel preference count")
                channels, value = tuple(body[2 : 2 + n]), body[2 + n]
                body = body[3 + n :]
                score, reason = value >> 4, value & 15
                if (
                    opclass != 81
                    or len(set(channels)) != len(channels)
                    or any(c not in range(1, 14) for c in channels)
                ):
                    raise EmosaError(
                        Reason.UNSUPPORTED_OPERATION,
                        "preferences outside advertised operating class",
                    )
                if score == 15 or reason > 13 or reason in (6, 7, 8, 9, 10):
                    invalid("reserved preference or agent-only reason in controller request")
                applies = set(channels or range(1, 14))
                if applies & specified:
                    invalid("overlapping controller channel preferences")
                if 6 in applies and score == 0:
                    raise EmosaError(
                        Reason.UNSUPPORTED_OPERATION,
                        "controller forbids the sole advertised operable channel",
                    )
                specified |= applies
                preferences.append(
                    {
                        "class": opclass,
                        "channels": list(channels),
                        "preference": score,
                        "reason": reason,
                    }
                )
            if body:
                invalid("trailing channel preference octets")
        elif tlv.kind == 0x8D:
            if power is not None or len(tlv.value) != 7 or tlv.value[:6] != radio.ruid:
                invalid("duplicate, truncated or foreign power limit")
            power = int.from_bytes(tlv.value[6:], "big", signed=True)
            if power < radio.tx_power_dbm:
                raise EmosaError(
                    Reason.UNSUPPORTED_OPERATION, "power actuation requires a qualified mapping"
                )
        else:
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "unimplemented channel configuration companion"
            )
    return {"preferences": preferences or [], "power_limit_dbm": power}


@dataclass
class PendingOperating:
    radio: OperatingRadio
    context: str
    deadline: float
    retry_at: float
    mids: set[int] = field(default_factory=set)
    transmissions: int = 0


class ChannelCoordinator:
    """One bound radio, bounded retry state and fresh-read report transmission."""

    def __init__(self, source, send_frame, store, mids, *, clock=time.monotonic, reset_policy=True):
        self.source, self.binding, self.send_frame = source, source.binding, send_frame
        self.store, self.mids, self.clock = store, mids, clock
        self.pending = None
        self.last_operating = None
        self.queries = {}
        self.counts = {}
        self.closed = False
        self.assembly = Reassembler(
            clock=clock, max_contexts=2, max_fragments=4, max_message_bytes=2048, max_bytes=4096
        )
        if reset_policy:
            self.store.save(
                {
                    "status": "reboot_default",
                    "controller": self.binding.controller_al.hex(),
                    "local_al": self.binding.local_al.hex(),
                    "preferences": [],
                    "power_limit_dbm": None,
                }
            )

    def record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        return event

    def snapshot(self):
        current = self.source.current()
        if self.closed or self.source.binding != self.binding or current is None:
            raise EmosaError(Reason.NOT_READY, "channel report source lost")
        return current

    def radio(self, snapshot):
        if len(snapshot.operating_radios) != 1:
            raise EmosaError(Reason.NOT_READY, "measured operating parameters unavailable")
        radio = snapshot.operating_radios[0]
        caps = snapshot.capabilities.radios
        if len(caps) != 1 or caps[0].basic.ruid != radio.ruid:
            raise EmosaError(Reason.NOT_READY, "channel capability identity mismatch")
        classes = caps[0].basic.operating_classes
        if (
            len(classes) != 1
            or classes[0].operating_class != 81
            or set(classes[0].non_operable_channels) != set(range(1, 14)) - {6}
        ):
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "a sole-channel capability contract is required"
            )
        if radio.tx_power_dbm > classes[0].max_eirp_dbm:
            raise EmosaError(Reason.NOT_READY, "observed power exceeds advertised capability")
        return radio

    def stamp(self):
        return self.snapshot().stamp

    def send(self, kind, mid, tlvs, snapshot, deadline):
        report = PreparedReport(
            kind,
            mid,
            snapshot.stamp,
            min(deadline, snapshot.stamp.valid_until),
            fragment_message(self.binding.controller_al, self.binding.local_al, kind, mid, tlvs),
        )
        report.send(self.send_frame, self.stamp, clock=self.clock)

    def handle(self, message, received_at):
        if message.message_type not in (0x8004, 0x8006):
            return None
        self.tick()
        snapshot = self.snapshot()
        radio = self.radio(snapshot)
        key = (message.message_type, message.mid)
        if key in self.queries:
            return self.record("duplicate_channel_request")
        if len(self.queries) >= 64:
            return self.record("channel_request_budget_exhausted")
        if message.message_type == 0x8004:
            if message.tlvs:
                invalid("channel preference query must not carry configuration")
            # §8.1: omitted preferences imply 15. Permanently unavailable
            # channels already appear in Basic Capabilities, not this report.
            self.send(0x8005, message.mid, (), snapshot, received_at + 1)
            self.queries[key] = self.clock() + 5
            return self.record("channel_preference_report_sent")
        policy = selected_policy(message.tlvs, radio)
        snapshot.stamp.check(self.stamp(), self.clock())
        if self.clock() >= received_at + 1:
            raise EmosaError(Reason.NOT_READY, "channel response deadline expired")
        try:
            self.store.save(
                {
                    **policy,
                    "status": "accepted_no_adjustment",
                    "mid": message.mid,
                    "controller": self.binding.controller_al.hex(),
                    "local_al": self.binding.local_al.hex(),
                    "ruid": radio.ruid.hex(),
                    "context": snapshot.context_token,
                }
            )
        except sqlite3.Error as exc:
            raise EmosaError(Reason.NOT_READY, "channel preference persistence failed") from exc
        self.send(0x8007, message.mid, (Tlv(0x8E, radio.ruid + b"\0"),), snapshot, received_at + 1)
        self.queries[key] = self.clock() + 5
        self.record("channel_selection_accepted")
        self.notify(snapshot, radio)
        return "channel_selection_accepted"

    def notify(self, snapshot, radio):
        now = self.clock()
        self.pending = PendingOperating(radio, snapshot.context_token, now + 1, now)
        self.transmit(snapshot)
        self.last_operating = radio

    def transmit(self, snapshot):
        pending = self.pending
        mid = self.mids.next()
        self.send(0x8008, mid, (pending.radio.tlv(),), snapshot, pending.deadline)
        pending.mids.add(mid)
        pending.transmissions += 1
        pending.retry_at = self.clock() + 0.3
        self.record("operating_channel_report_sent")

    def tick(self):
        now = self.clock()
        self.queries = {k: v for k, v in self.queries.items() if v > now}
        self.assembly.expire()
        pending = self.pending
        if pending is None:
            if self.last_operating is not None and not self.closed:
                try:
                    snapshot = self.snapshot()
                    radio = self.radio(snapshot)
                except EmosaError:
                    return
                if radio != self.last_operating:
                    self.notify(snapshot, radio)
                    self.record("autonomous_operating_change_reported")
            return
        try:
            snapshot = self.snapshot()
            radio = self.radio(snapshot)
            if snapshot.context_token != pending.context or radio != pending.radio:
                raise EmosaError(Reason.NOT_READY, "operating report context changed")
        except EmosaError:
            self.pending = None
            self.record("operating_report_source_lost")
            return
        if now >= pending.deadline:
            self.pending = None
            self.record("operating_channel_ack_timeout")
        elif now >= pending.retry_at and pending.transmissions < 3:
            self.transmit(snapshot)

    def ack(self, frame, *, ingress, generation):
        message = self.assembly.feed(frame, ingress=ingress)
        if message is None:
            return None
        self.binding.check(message, ingress=ingress, generation=generation)
        if self.pending is None or message.mid not in self.pending.mids:
            return None
        snapshot = self.snapshot()
        if (
            self.clock() >= self.pending.deadline
            or snapshot.context_token != self.pending.context
            or self.radio(snapshot) != self.pending.radio
        ):
            self.pending = None
            return self.record("stale_operating_ack_rejected")
        if (
            message.message_type != 0x8000
            or message.relay
            or any(t.kind in (*SECURITY_ENVELOPES, 0xA3, 0xBC) for t in message.tlvs)
        ):
            invalid("invalid operating channel acknowledgment")
        self.pending = None
        return self.record("operating_channel_acknowledged")

    def close(self):
        self.closed = True
        self.pending = None
