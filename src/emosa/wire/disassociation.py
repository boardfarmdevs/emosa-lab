"""Qualified final-session reports (EasyMesh 6.1 §§6.3, 15.1, 17.1.41).

Internal measurement-to-wire component, not an endpoint or qualification tool.
The caller must establish the publisher's final-session/counter/reason semantics.
The current hwsim polling publisher cannot supply that evidence. A last periodic
sample, membership disappearance or missing Protobuf field is never a final zero.
"""

import math
import struct
import time
from collections import deque
from dataclasses import dataclass, field

from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import SECURITY_ENVELOPES
from emosa.wire.cmdu import Reassembler, Tlv, decode_frame, fragment_message, invalid
from emosa.wire.reports import PreparedReport

# IEEE 802.11-2024 §9.4.1.7/Table 9-79, printed pp.837–840.
REASONS = frozenset(range(1, 40)) | frozenset(range(46, 70)) | {71}


def unicast(address):
    if type(address) is not bytes or len(address) != 6 or not any(address) or address[0] & 1:
        invalid("expected a unicast station/BSS address")


@dataclass(frozen=True)
class TrafficCounters:
    """Unsigned raw counters with explicitly qualified AP-relative semantics.

    tx_packets counts successful packets; tx_retries counts transmitted packets
    carrying the retry flag, including repeated retransmissions of one packet.
    The seven fields have no defaults. These meanings are
    prerequisites for a publisher mapping, not consequences of decoding bytes.
    """

    tx_bytes: int
    rx_bytes: int
    tx_packets: int
    rx_packets: int
    tx_errors: int
    rx_errors: int
    tx_retries: int

    def __post_init__(self):
        if any(type(v) is not int or not 0 <= v < 2**64 for v in self.values()):
            invalid("seven explicit unsigned counters are required")

    def values(self):
        return (
            self.tx_bytes,
            self.rx_bytes,
            self.tx_packets,
            self.rx_packets,
            self.tx_errors,
            self.rx_errors,
            self.tx_retries,
        )

    @classmethod
    def from_qualified_opensync(cls, stats):
        """Decode only after qualifying the publisher's units/session semantics.

        Proto2 getters return zero for absent fields; HasField is essential.
        This function performs no source qualification or finality inference.
        """
        fields = (
            "tx_bytes",
            "rx_bytes",
            "tx_frames",
            "rx_frames",
            "tx_errors",
            "rx_errors",
            "tx_retries",
        )
        if getattr(
            getattr(stats, "DESCRIPTOR", None), "full_name", None
        ) != "sts.Client.Stats" or any(not stats.HasField(name) for name in fields):
            raise EmosaError(Reason.NOT_READY, "complete qualified traffic counters unavailable")
        return cls(*(getattr(stats, name) for name in fields))

    def tlv(self, station, *, profile, byte_units):
        unicast(station)
        if type(profile) is not int or profile not in (1, 2, 3):
            invalid("explicit selected Multi-AP profile required")
        if type(byte_units) is not int or byte_units not in (0, 1, 2):
            invalid("reserved byte counter units")
        # Table 58 requires bytes for Profile-1. Do not choose silently between
        # that rule and an inconsistent last advertised Profile-2 counter unit.
        if profile == 1 and byte_units != 0:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "Profile-1 requires byte counters")
        scale = (1, 1024, 1024 * 1024)[byte_units]
        counts = (self.tx_bytes // scale, self.rx_bytes // scale, *self.values()[2:])
        # Table 58 makes rollover a receiver responsibility. Scale the lifetime
        # byte counter first, then take its low 32 bits; never saturate it.
        return Tlv(0xA2, station + struct.pack("!7I", *(v & 0xFFFFFFFF for v in counts)))


@dataclass(frozen=True)
class FinalSession:
    """A complete final record from a qualified publisher/session join.

    context is the live ReportSource context token; session identifies this
    particular association, not just its MAC. observed_at uses the same monotonic
    clock as ReportSource after the publisher's clock/age qualification.
    """

    context: str
    session: str
    bssid: bytes
    station: bytes
    observed_at: float
    reason: int
    counters: TrafficCounters

    def __post_init__(self):
        unicast(self.bssid)
        unicast(self.station)
        if (
            type(self.context) is not str
            or not 1 <= len(self.context) <= 512
            or type(self.session) is not str
            or not 1 <= len(self.session) <= 128
            or type(self.observed_at) not in (int, float)
            or not math.isfinite(self.observed_at)
            or type(self.reason) is not int
            or self.reason not in REASONS
            or type(self.counters) is not TrafficCounters
        ):
            invalid("complete, session-bound final traffic and actual reason required")

    @property
    def key(self):
        return self.context, self.bssid, self.station, self.session

    def tlvs(self, *, profile, byte_units):
        return (
            Tlv(0x95, self.station),
            Tlv(0xCA, struct.pack("!H", self.reason)),
            self.counters.tlv(self.station, profile=profile, byte_units=byte_units),
        )


@dataclass
class PendingFinal:
    event: FinalSession
    byte_units: int
    deadline: float
    retry_at: float
    mids: set[int] = field(default_factory=set)
    transmissions: int = 0


class DisassociationCoordinator:
    """Bounded unsolicited final stats, withdrawn on source loss or rejoin."""

    def __init__(self, source, send_frame, mids, *, profile=1, clock=time.monotonic):
        self.source, self.binding, self.send_frame = source, source.binding, send_frame
        self.mids, self.profile, self.clock = mids, profile, clock
        self.pending, self.seen = {}, {}
        self.closed = False
        self.counts, self.events = {}, deque(maxlen=32)
        self.assembly = Reassembler(
            clock=clock, max_contexts=2, max_fragments=4, max_message_bytes=2048, max_bytes=4096
        )

    def record(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1
        self.events.append(name)
        return name

    def snapshot(self, event, *, expected_units=None):
        snapshot = self.source.current() if self.source.binding == self.binding else None
        if self.closed or snapshot is None or snapshot.context_token != event.context:
            raise EmosaError(Reason.NOT_READY, "final-session source context lost")
        if not 0 <= self.clock() - event.observed_at < 2:
            raise EmosaError(Reason.NOT_READY, "final counters are stale or future-dated")
        topology = snapshot.topology
        bsses = {b.ap_mac for r in topology.operational.radios for b in r.bsses}
        if event.bssid not in bsses or not topology.inventory_complete:
            raise EmosaError(Reason.NOT_READY, "final-session BSS/membership unavailable")
        if any(c.mac == event.station for b in topology.clients.bsses for c in b.clients):
            raise EmosaError(
                Reason.NOT_READY, "station is associated again; withdraw old final report"
            )
        if (
            expected_units is not None
            and snapshot.capabilities.profile2.byte_counter_units != expected_units
        ):
            raise EmosaError(Reason.NOT_READY, "counter-unit advertisement changed")
        return snapshot

    def submit(self, event):
        if type(event) is not FinalSession:
            invalid("qualified final-session record required")
        self.tick()
        snapshot = self.snapshot(event)
        # Validate byte interpretation before consuming a queue slot or sending.
        event.tlvs(
            profile=self.profile, byte_units=snapshot.capabilities.profile2.byte_counter_units
        )
        if event.key in self.seen:
            if self.seen[event.key] != event:
                invalid("conflicting final records for the same association")
            return self.record("duplicate_final_session")
        if len(self.pending) >= 16 or len(self.seen) >= 64:
            return self.record("final_session_budget_exhausted")
        now = self.clock()
        pending = PendingFinal(
            event,
            snapshot.capabilities.profile2.byte_counter_units,
            min(now + 1, event.observed_at + 2),
            now,
        )
        self.seen[event.key] = event
        self.pending[event.key] = pending
        try:
            self.transmit(pending, snapshot)
        except (EmosaError, OSError):
            self.pending.pop(event.key, None)
            self.record("final_session_send_failed")
            raise
        return "final_session_report_sent"

    def transmit(self, pending, snapshot):
        mid = self.mids.next()
        report = PreparedReport(
            0x8022,
            mid,
            snapshot.stamp,
            min(pending.deadline, snapshot.stamp.valid_until),
            fragment_message(
                self.binding.controller_al,
                self.binding.local_al,
                0x8022,
                mid,
                pending.event.tlvs(
                    profile=self.profile,
                    byte_units=snapshot.capabilities.profile2.byte_counter_units,
                ),
            ),
        )
        report.send(
            self.send_frame,
            lambda: self.snapshot(pending.event, expected_units=pending.byte_units).stamp,
            clock=self.clock,
        )
        pending.mids.add(mid)
        pending.transmissions += 1
        pending.retry_at = self.clock() + 0.3
        self.record("final_session_report_sent")

    def tick(self):
        now = self.clock()
        self.assembly.expire()
        self.seen = {k: v for k, v in self.seen.items() if now < v.observed_at + 2}
        for key, pending in list(self.pending.items()):
            try:
                snapshot = self.snapshot(pending.event, expected_units=pending.byte_units)
            except EmosaError:
                self.pending.pop(key)
                self.record("final_session_source_lost")
                continue
            if now >= pending.deadline:
                self.pending.pop(key)
                self.record("final_session_ack_timeout")
            elif now >= pending.retry_at and pending.transmissions < 3:
                try:
                    self.transmit(pending, snapshot)
                except (EmosaError, OSError):
                    self.pending.pop(key)
                    self.record("final_session_send_failed")

    def ack(self, frame, *, ingress, generation):
        fragment = decode_frame(frame)
        self.binding.check(fragment, ingress=ingress, generation=generation)
        if fragment.message_type != 0x8000:
            return None
        message = self.assembly.feed(frame, ingress=ingress)
        if message is None:
            return None
        pending = next((p for p in self.pending.values() if message.mid in p.mids), None)
        if pending is None:
            return None
        self.snapshot(pending.event, expected_units=pending.byte_units)
        if self.clock() >= pending.deadline:
            self.pending.pop(pending.event.key)
            return self.record("final_session_late_ack")
        if message.relay or any(t.kind in (*SECURITY_ENVELOPES, 0xA3, 0xBC) for t in message.tlvs):
            invalid("invalid final-session acknowledgment")
        self.pending.pop(pending.event.key)
        return self.record("final_session_acknowledged")

    def close(self):
        self.closed = True
        self.pending.clear()
        self.seen.clear()
