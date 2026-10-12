# SPDX-License-Identifier: Apache-2.0
"""Selected sole-radio channel procedures (EasyMesh 6.1 §§8.1–2, 15.1).

The initial radio profile supports only class 81/channel 6. Compatible requests
need no actuator change; their acceptance still requires durable preferences and
fresh observed operating parameters. This is not a generic channel actuator.

Every well-formed Channel Selection Request is answered within its deadline, as
§8.2 requires (a controller that gets no response retries and gives the radio
up, as RDK's does). Preferences for operating classes the radio does not
advertise do not concern it and are recorded as ignored. A request EMOSA cannot
carry out on the pod (it forbids the channel the pod operates on, or limits the
power below what the pod transmits) is declined with response code 0x02: it
violates the preferences and capabilities last reported. A decline keeps the
stored policy. The Operating Channel Report of the actual operation follows
either answer. Malformed requests get no answer.

A Channel Scan Request (§8.4) is acknowledged, and answered with a Channel Scan
Report whose result for every requested channel of the radio is "scan not
supported" (status 0x01): EMOSA does not scan on the pod's radio. A controller
that gets no Ack retries (RDK's every few seconds).
"""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import SECURITY_ENVELOPES
from emosa.wire.cmdu import Reassembler, Tlv, fragment_message, invalid
from emosa.wire.reports import PreparedReport

# The operating classes a reported radio may be on and their channels (emosa.opensync.easymesh_view
# OPERATING_CLASSES): 2.4 GHz, and the non-DFS 5 GHz UNII-1 and UNII-3 classes of a pod's second
# radio (spec 9). A radio advertises every class of its band (BAND_CLASSES), its current channel
# the only operable one.
CLASS_CHANNELS = {
    81: tuple(range(1, 14)),
    115: (36, 40, 44, 48),
    124: (149, 153, 157, 161),
    125: (149, 153, 157, 161, 165, 169, 173, 177),
}
BAND_CLASSES = ((81,), (115, 124, 125))


def advertised(operating_class):
    """The classes a radio operating in this class advertises: every class of its band."""
    return next(classes for classes in BAND_CLASSES if operating_class in classes)


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
            or self.channel not in CLASS_CHANNELS.get(self.operating_class, ())
            or type(self.tx_power_dbm) is not int
            or not -128 <= self.tx_power_dbm <= 127
        ):
            invalid("operating parameters outside the selected sole-channel contract")

    def tlv(self):
        return Tlv(
            0x8F,
            self.ruid + bytes((1, self.operating_class, self.channel, self.tx_power_dbm & 255)),
        )


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


def selected_policy(tlvs, radios):
    """Validate the complete selected request before persisting or accepting it.

    ``radios``: the agent's operating radios (spec 9: each radio it reports, the fronthaul
    first). Each named radio's preferences and power limit are judged on their own; the result
    lists them in the order first named, or every radio for a request naming none.

    A contradictory request excluding a radio's only advertised operable channel is outside
    this controller/profile contract (a second radio's channel follows the pod's station, spec
    9). It must not replace a usable policy and then silently keep operating on a newly
    forbidden channel.
    """
    by_ruid = {r.ruid: r for r in radios}
    named = {}  # RUID -> its policy, in the order first named

    def policy(ruid):
        return named.setdefault(
            ruid, {"preferences": None, "power_limit_dbm": None, "ignored": [], "decline": None}
        )

    for tlv in tlvs:
        if tlv.kind == 0x8B:
            ruid = tlv.value[:6]
            if (
                len(tlv.value) < 7
                or ruid not in by_ruid
                or (ruid in named and named[ruid]["preferences"] is not None)
            ):
                invalid("duplicate, truncated or foreign channel preferences")
            radio, entry = by_ruid[ruid], policy(ruid)
            classes = advertised(radio.operating_class)
            count, body = tlv.value[6], tlv.value[7:]
            if count > 8:
                invalid("channel preference group budget exceeded")
            preferences, specified = [], set()  # (class, channel) pairs already given
            for _ in range(count):
                if len(body) < 3:
                    invalid("truncated channel preference group")
                opclass, n = body[:2]
                if n > 13 or len(body) < 3 + n:
                    invalid("invalid channel preference count")
                channels, value = tuple(body[2 : 2 + n]), body[2 + n]
                body = body[3 + n :]
                score, reason = value >> 4, value & 15
                if score == 15 or reason > 13 or reason in (6, 7, 8, 9, 10):
                    invalid("reserved preference or agent-only reason in controller request")
                if opclass not in classes:
                    # Not an operating class the radio advertises (e.g. RDK's 40 MHz
                    # classes 83/84): nothing in it applies to this radio.
                    entry["ignored"].append({"class": opclass, "channels": list(channels)})
                    continue
                own = CLASS_CHANNELS[opclass]
                if len(set(channels)) != len(channels) or any(c not in own for c in channels):
                    invalid("channels outside the operating class")
                applies = {(opclass, c) for c in channels or own}
                if applies & specified:
                    invalid("overlapping controller channel preferences")
                if (opclass, radio.channel) in applies and score == 0:
                    # in any class it advertises (124 and 125 both have 149)
                    entry["decline"] = "controller forbids the sole advertised operable channel"
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
            entry["preferences"] = preferences
        elif tlv.kind == 0x8D:
            ruid = tlv.value[:6]
            if (
                len(tlv.value) != 7
                or ruid not in by_ruid
                or (ruid in named and named[ruid]["power_limit_dbm"] is not None)
            ):
                invalid("duplicate, truncated or foreign power limit")
            entry = policy(ruid)
            entry["power_limit_dbm"] = int.from_bytes(tlv.value[6:], "big", signed=True)
            if entry["power_limit_dbm"] < by_ruid[ruid].tx_power_dbm:
                entry["decline"] = (
                    entry["decline"] or "power actuation requires a qualified mapping"
                )
        else:
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "unimplemented channel configuration companion"
            )
    if not named:  # a request naming no radio: accepted for every radio, as for one radio
        for radio in radios:
            policy(radio.ruid)
    return [
        {"ruid": ruid, **entry, "preferences": entry["preferences"] or []}
        for ruid, entry in named.items()
    ]


def decode_scan_request(tlvs):
    """The one Channel Scan Request TLV: [(RUID, [(operating class, [channels])])]."""
    found = [t for t in tlvs if t.kind == 0xA6]
    if len(found) != 1 or len(found[0].value) < 2:
        invalid("one Channel Scan Request TLV required")
    data, radios, offset = found[0].value, [], 2
    for _ in range(data[1]):
        if len(data) < offset + 7:
            invalid("truncated channel scan radio")
        ruid, count, offset = data[offset : offset + 6], data[offset + 6], offset + 7
        classes = []
        for _ in range(count):
            if len(data) < offset + 2 or len(data) < offset + 2 + data[offset + 1]:
                invalid("truncated channel scan operating class")
            opclass, n = data[offset], data[offset + 1]
            classes.append((opclass, list(data[offset + 2 : offset + 2 + n])))
            offset += 2 + n
        radios.append((ruid, classes))
    if offset != len(data):
        invalid("trailing channel scan request octets")
    return radios


@dataclass
class PendingOperating:
    radios: tuple  # an OperatingRadio per radio the agent reports (spec 9)
    context: str
    deadline: float
    retry_at: float
    mids: set[int] = field(default_factory=set)
    transmissions: int = 0


class ChannelCoordinator:
    """One bound radio, bounded retry state and fresh-read report transmission."""

    def __init__(
        self,
        source,
        send_frame,
        store,
        mids,
        *,
        clock=time.monotonic,
        reset_policy=True,
        utc=lambda: datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    ):
        self.source, self.binding, self.send_frame = source, source.binding, send_frame
        self.store, self.mids, self.clock, self.utc = store, mids, clock, utc
        self.pending = None
        self.last_decline = None
        self.last_operating = None
        self.queries = {}
        self.waiting = {}
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
                    "radios": [],  # no radio's preferences kept (spec 9: each radio its own)
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

    @staticmethod
    def unmeasured(snapshot):
        """A reported radio has no fresh operating parameters yet (spec 9: every radio's)."""
        measured = {r.ruid for r in snapshot.operating_radios}
        return any(c.basic.ruid not in measured for c in snapshot.capabilities.radios)

    def radios(self, snapshot):
        """Each radio the agent reports (spec 9: a second radio too), the fronthaul first, with
        its measured operating parameters; each operable on its one current channel only (the
        fronthaul's channel, a second radio's its station's)."""
        measured = {r.ruid: r for r in snapshot.operating_radios}
        caps = snapshot.capabilities.radios
        if not snapshot.operating_radios or self.unmeasured(snapshot):
            raise EmosaError(Reason.NOT_READY, "measured operating parameters unavailable")
        if len(measured) != len(caps):
            raise EmosaError(Reason.NOT_READY, "channel capability identity mismatch")
        radios = []
        for cap in caps:
            radio = measured[cap.basic.ruid]
            classes = cap.basic.operating_classes
            # every class of its band, the current channel the only operable one in each
            if tuple(c.operating_class for c in classes) != advertised(
                radio.operating_class
            ) or any(
                set(c.non_operable_channels)
                != set(CLASS_CHANNELS[c.operating_class]) - {radio.channel}
                for c in classes
            ):
                raise EmosaError(
                    Reason.UNSUPPORTED_OPERATION, "a sole-channel capability contract is required"
                )
            if any(radio.tx_power_dbm > c.max_eirp_dbm for c in classes):
                raise EmosaError(Reason.NOT_READY, "observed power exceeds advertised capability")
            radios.append(radio)
        return tuple(radios)

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
        if message.message_type == 0x801B:
            return self.scan(message, received_at)
        if message.message_type not in (0x8004, 0x8006):
            return None
        self.tick()
        snapshot = self.snapshot()
        key = (message.message_type, message.mid)
        if key in self.queries or key in self.waiting:
            return self.record("duplicate_channel_request")
        if len(self.queries) + len(self.waiting) >= 64:
            return self.record("channel_request_budget_exhausted")
        if self.clock() >= received_at + 1:
            raise EmosaError(Reason.NOT_READY, "channel response deadline expired")
        if self.unmeasured(snapshot):
            # A controller can send these immediately after M2, before the
            # first fresh manager sample (of any of the agent's radios). Wait only within the
            # original wire deadline and control context; duplicates cannot extend either.
            if len(self.waiting) >= 4:
                return self.record("channel_observation_wait_budget_exhausted")
            self.waiting[key] = (message, received_at, snapshot.context_token)
            return self.record("channel_waiting_for_observation")
        return self.answer(message, received_at, snapshot)

    def answer(self, message, received_at, snapshot):
        radios = self.radios(snapshot)
        key = (message.message_type, message.mid)
        if message.message_type == 0x8004:
            if message.tlvs:
                invalid("channel preference query must not carry configuration")
            # §8.1: omitted preferences imply 15. Permanently unavailable
            # channels already appear in Basic Capabilities, not this report.
            self.send(0x8005, message.mid, (), snapshot, received_at + 1)
            self.queries[key] = self.clock() + 5
            return self.record("channel_preference_report_sent")
        policies = selected_policy(message.tlvs, radios)
        snapshot.stamp.check(self.stamp(), self.clock())
        if self.clock() >= received_at + 1:
            raise EmosaError(Reason.NOT_READY, "channel response deadline expired")
        accepted = [p for p in policies if not p["decline"]]
        declined = [p for p in policies if p["decline"]]
        if accepted:
            # the accepted radios' preferences (spec 9: each radio its own); a declined radio
            # does not replace the stored policy
            try:
                self.store.save(
                    {
                        "status": "accepted_no_adjustment",
                        "mid": message.mid,
                        "controller": self.binding.controller_al.hex(),
                        "local_al": self.binding.local_al.hex(),
                        "radios": [
                            {
                                "ruid": p["ruid"].hex(),
                                "preferences": p["preferences"],
                                "power_limit_dbm": p["power_limit_dbm"],
                                "ignored": p["ignored"],
                            }
                            for p in accepted
                        ],
                        "context": snapshot.context_token,
                    }
                )
            except sqlite3.Error as exc:
                raise EmosaError(Reason.NOT_READY, "channel preference persistence failed") from exc
        if declined:
            # 0x02: the request violates the most recently reported preferences.
            self.last_decline = {"mid": message.mid, "reason": declined[0]["decline"]}
        # one Channel Selection Response per radio the request names, as named
        responses = tuple(
            Tlv(0x8E, p["ruid"] + (b"\x02" if p["decline"] else b"\0")) for p in policies
        )
        self.send(0x8007, message.mid, responses, snapshot, received_at + 1)
        self.queries[key] = self.clock() + 5
        result = self.record(
            "channel_selection_declined" if declined else "channel_selection_accepted"
        )
        self.notify(snapshot, radios)
        return result

    def scan(self, message, received_at):
        """Channel Scan Request: Ack, then a report of "scan not supported" results."""
        self.tick()
        snapshot = self.snapshot()
        key = (message.message_type, message.mid)
        if key in self.queries:
            return self.record("duplicate_channel_request")
        if self.clock() >= received_at + 1:
            raise EmosaError(Reason.NOT_READY, "channel scan Ack deadline expired")
        by_ruid = {r.ruid: r for r in self.radios(snapshot)}
        requested = decode_scan_request(message.tlvs)
        stamp = self.utc().encode()
        results = []
        for ruid, classes in requested:
            radio = by_ruid.get(ruid)
            if radio is None:
                continue  # not a radio of this agent
            own = radio.operating_class
            for opclass, channels in classes or [(own, [radio.channel])]:
                for channel in channels or ([radio.channel] if opclass == own else []):
                    # status 0x01: scan not supported on this class and channel
                    results.append(Tlv(0xA7, ruid + bytes((opclass, channel, 0x01))))
        self.send(0x8000, message.mid, (), snapshot, received_at + 1)
        self.queries[key] = self.clock() + 5
        report = (Tlv(0xA8, bytes([len(stamp)]) + stamp), *results)
        self.send(0x801C, self.mids.next(), report, snapshot, self.clock() + 1)
        return self.record("channel_scan_not_supported_reported")

    def notify(self, snapshot, radios):
        """The Operating Channel Report: one Operating Channel Report TLV per radio (spec 9)."""
        now = self.clock()
        self.pending = PendingOperating(radios, snapshot.context_token, now + 1, now)
        self.transmit(snapshot)
        self.last_operating = radios

    def transmit(self, snapshot):
        pending = self.pending
        mid = self.mids.next()
        self.send(0x8008, mid, tuple(r.tlv() for r in pending.radios), snapshot, pending.deadline)
        pending.mids.add(mid)
        pending.transmissions += 1
        pending.retry_at = self.clock() + 0.3
        self.record("operating_channel_report_sent")

    def tick(self):
        now = self.clock()
        self.queries = {k: v for k, v in self.queries.items() if v > now}
        self.assembly.expire()
        for key, (message, received_at, context) in tuple(self.waiting.items()):
            if now >= received_at + 1:
                del self.waiting[key]
                self.record("channel_observation_wait_expired")
                continue
            try:
                snapshot = self.snapshot()
                if snapshot.context_token != context:
                    raise EmosaError(Reason.NOT_READY, "queued channel context changed")
                if self.unmeasured(snapshot):
                    continue
                del self.waiting[key]
                self.answer(message, received_at, snapshot)
            except EmosaError as exc:
                self.waiting.pop(key, None)
                self.record("queued_channel_rejected_" + exc.code.value)
        pending = self.pending
        if pending is None:
            if self.last_operating is not None and not self.closed:
                try:
                    snapshot = self.snapshot()
                    radios = self.radios(snapshot)
                except EmosaError:
                    return
                if radios != self.last_operating:
                    self.notify(snapshot, radios)
                    self.record("autonomous_operating_change_reported")
            return
        try:
            snapshot = self.snapshot()
            radios = self.radios(snapshot)
            if snapshot.context_token != pending.context or radios != pending.radios:
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
            or self.radios(snapshot) != self.pending.radios
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
        self.waiting.clear()
