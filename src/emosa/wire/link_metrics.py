"""IEEE 1905.1-2013 neighbor metrics with the 1905.1a-2014 field rules.

Measurements are explicit publisher inputs, never inferred from traffic success
or topology. The guarded source is an internal handoff, not an attestation that
an arbitrary Linux interface counter satisfies the specification.
"""

import math
import struct
import time
import uuid
from dataclasses import dataclass

from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import SECURITY_ENVELOPES
from emosa.wire.cmdu import Tlv, fragment_message, invalid, mac, uint
from emosa.wire.reports import PreparedReport, ReportStamp

MEDIA = frozenset((0, 1, *range(0x100, 0x10A), 0x200, 0x201, 0x300, 0xFFFF))
TX = struct.Struct("!6s6sHBIIHHH")
RX = struct.Struct("!6s6sHIIB")
MAX_LINKS = 32  # Local resource budget, not a protocol maximum.


def unavailable(detail):
    raise EmosaError(Reason.NOT_READY, detail)


def identity(value):
    mac(value)
    if not any(value) or value[0] & 1:
        invalid("link identity must be a nonzero unicast MAC")
    return value


def media(value):
    uint(value, 16, "link media type")
    if value not in MEDIA:
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "reserved media type; ignore entire TLV")
    return value


@dataclass(frozen=True)
class LinkMetricQuery:
    neighbor: bytes | None
    direction: int  # 0 TX, 1 RX, 2 both.

    def tlv(self):
        uint(self.direction, 8, "metric direction")
        if self.direction > 2:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "reserved direction; ignore query TLV")
        return Tlv(
            8,
            (b"\0" if self.neighbor is None else b"\1" + mac(self.neighbor))
            + bytes((self.direction,)),
        )


def decode_query(tlvs):
    queries = [t for t in tlvs if t.kind == 8]
    if len(queries) != 1:
        invalid("exactly one link metric query TLV required")
    raw = queries[0].value
    if not raw:
        invalid("truncated link metric query")
    if raw[0] > 1:
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "reserved scope; ignore query TLV")
    # Table 6-16 lists length 8 but explicitly omits the six-byte address for
    # all-neighbor queries. Apply that conditional layout: 2 bytes or 8 bytes.
    if len(raw) != (2 if raw[0] == 0 else 8):
        invalid("incorrect conditional link query length")
    query = LinkMetricQuery(None if raw[0] == 0 else raw[1:7], raw[-1])
    query.tlv()
    return query


@dataclass(frozen=True)
class TxLink:
    local_interface: bytes
    neighbor_interface: bytes
    media_type: int
    bridges_present: bool
    packet_errors: int
    transmitted_packets: int
    mac_throughput_mbps: int
    availability_percent: int
    phy_rate_mbps: int

    def encode(self):
        identity(self.local_interface)
        identity(self.neighbor_interface)
        media(self.media_type)
        if type(self.bridges_present) is not bool:
            invalid("bridge presence must be observed explicitly")
        uint(self.packet_errors, 32, "transmit packet errors")
        uint(self.transmitted_packets, 32, "transmitted packets")
        uint(self.mac_throughput_mbps, 16, "MAC throughput capacity")
        uint(self.availability_percent, 16, "link availability")
        uint(self.phy_rate_mbps, 16, "PHY rate")
        if self.availability_percent > 100:
            invalid("link availability exceeds 100 percent")
        if self.media_type >> 8 not in (0, 2, 3, 255) and self.phy_rate_mbps != 65535:
            invalid("this media type requires the unspecified PHY-rate sentinel")
        return TX.pack(
            self.local_interface,
            self.neighbor_interface,
            self.media_type,
            self.bridges_present,
            self.packet_errors,
            self.transmitted_packets,
            self.mac_throughput_mbps,
            self.availability_percent,
            self.phy_rate_mbps,
        )


@dataclass(frozen=True)
class RxLink:
    local_interface: bytes
    neighbor_interface: bytes
    media_type: int
    packet_errors: int
    received_packets: int
    rssi_db: int

    def encode(self):
        identity(self.local_interface)
        identity(self.neighbor_interface)
        media(self.media_type)
        uint(self.packet_errors, 32, "receive packet errors")
        uint(self.received_packets, 32, "received packets")
        uint(self.rssi_db, 8, "link RSSI")
        if self.media_type >> 8 not in (1, 255) and self.rssi_db != 255:
            invalid("this media type requires the unspecified RSSI sentinel")
        return RX.pack(
            self.local_interface,
            self.neighbor_interface,
            self.media_type,
            self.packet_errors,
            self.received_packets,
            self.rssi_db,
        )


@dataclass(frozen=True)
class LinkMetrics:
    local_al: bytes
    neighbor_al: bytes
    links: tuple[TxLink, ...] | tuple[RxLink, ...]

    def tlv(self):
        identity(self.local_al)
        identity(self.neighbor_al)
        if self.local_al == self.neighbor_al:
            invalid("a device cannot be its own link-metric neighbor")
        if type(self.links) is not tuple or not 1 <= len(self.links) <= MAX_LINKS:
            invalid("link metric interface-pair budget exceeded or empty")
        kind = type(self.links[0])
        if kind not in (TxLink, RxLink) or any(type(row) is not kind for row in self.links):
            invalid("link metric TLV must contain one direction")
        encoded = tuple(row.encode() for row in self.links)
        pairs = [(row.local_interface, row.neighbor_interface) for row in self.links]
        if len(set(pairs)) != len(pairs):
            invalid("duplicate interface pair")
        return Tlv(
            9 if kind is TxLink else 10,
            self.local_al + self.neighbor_al + b"".join(encoded),
        )


def decode_metrics(tlv):
    if tlv.kind not in (9, 10):
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "not a transmitter/receiver metric TLV")
    layout, kind = (TX, TxLink) if tlv.kind == 9 else (RX, RxLink)
    raw = tlv.value
    if (
        not 12 + layout.size <= len(raw) <= 12 + MAX_LINKS * layout.size
        or (len(raw) - 12) % layout.size
    ):
        invalid("truncated or partial link-metric interface pair")
    rows = []
    for fields in layout.iter_unpack(raw[12:]):
        if kind is TxLink:
            if fields[3] > 1:
                raise EmosaError(Reason.UNSUPPORTED_OPERATION, "reserved bridge flag; ignore TLV")
            fields = (*fields[:3], bool(fields[3]), *fields[4:])
        rows.append(kind(*fields))
    result = LinkMetrics(raw[:6], raw[6:12], tuple(rows))
    result.tlv()
    return result


@dataclass(frozen=True)
class LinkBinding:
    neighbor_al: bytes
    local_interface: bytes
    neighbor_interface: bytes
    media_type: int
    bridges_present: bool


def topology_key(snapshot):
    return snapshot.topology.device, snapshot.topology.neighbors1905


@dataclass(frozen=True)
class LinkSample:
    context_token: str
    topology: tuple
    counter_epoch: str
    interval_started: float
    stamp: ReportStamp
    inventory: tuple[LinkBinding, ...]
    metrics: tuple[LinkMetrics, ...]


class LinkMetricSource:
    """One complete, bounded link inventory plus separately available directions.

    A qualified publisher must establish peer-interface attribution, bridge state,
    counter epoch and a common measurement interval. None of these are derived
    from the controller's AL address or inferred from a successful ping.
    """

    def __init__(self, reports, *, clock=time.monotonic):
        self.reports, self.clock = reports, clock
        self.sample = None
        self.watermark = None

    def invalidate(self):
        self.sample = None

    def publish(
        self,
        *,
        context_token,
        counter_epoch,
        interval_started,
        observed_at,
        inventory,
        metrics,
        inventory_complete,
        lifetime=2,
    ):
        try:
            snapshot = self.reports.current()
            if snapshot is None or snapshot.context_token != context_token:
                unavailable("link measurement does not belong to the live control context")
            if inventory_complete is not True:
                unavailable("complete independently observed neighbor inventory required")
            times = (interval_started, observed_at, lifetime)
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in times):
                invalid("invalid link measurement time")
            if not 0 <= interval_started < observed_at <= self.clock() or not 0 < lifetime <= 2:
                invalid("invalid link measurement interval or lifetime")
            if self.clock() >= observed_at + lifetime:
                unavailable("link measurements already expired")
            if not isinstance(counter_epoch, str) or not 1 <= len(counter_epoch) <= 128:
                invalid("explicit bounded link-counter epoch required")
            if type(inventory) is not tuple or len(inventory) > MAX_LINKS:
                invalid("neighbor inventory exceeds local budget")
            if type(metrics) is not tuple or len(metrics) > 2 * MAX_LINKS:
                invalid("link metrics exceed local budget")
            local = {i.mac: i.media_type for i in snapshot.topology.device.interfaces}
            expected = {
                (r.local_interface, n.al_mac): n.bridges_present
                for r in snapshot.topology.neighbors1905
                for n in r.neighbors
            }
            pairs = {}
            for row in inventory:
                if type(row) is not LinkBinding:
                    invalid("typed neighbor interface binding required")
                for address in (row.neighbor_al, row.local_interface, row.neighbor_interface):
                    identity(address)
                key = (row.neighbor_al, row.local_interface, row.neighbor_interface)
                if key in pairs or row.neighbor_al == self.reports.binding.local_al:
                    invalid("duplicate or self neighbor binding")
                if (
                    local.get(row.local_interface) != media(row.media_type)
                    or type(row.bridges_present) is not bool
                    or expected.get((row.local_interface, row.neighbor_al))
                    is not row.bridges_present
                ):
                    unavailable("measured interface/bridge binding differs from current topology")
                pairs[key] = row
            if {(row.local_interface, row.neighbor_al) for row in inventory} != set(expected):
                unavailable("measured neighbor inventory omits current topology")
            seen = set()
            for report in metrics:
                if type(report) is not LinkMetrics:
                    invalid("typed link metrics required")
                tlv = report.tlv()
                if report.local_al != self.reports.binding.local_al:
                    invalid("foreign reporting AL identity")
                key = (report.neighbor_al, tlv.kind)
                if key in seen:
                    invalid("duplicate neighbor/direction metrics")
                seen.add(key)
                wanted = {k for k in pairs if k[0] == report.neighbor_al}
                supplied = {
                    (report.neighbor_al, row.local_interface, row.neighbor_interface)
                    for row in report.links
                }
                if not wanted or supplied != wanted:
                    unavailable("metric report must cover all observed pairs for its neighbor")
                for row in report.links:
                    binding = pairs[
                        (report.neighbor_al, row.local_interface, row.neighbor_interface)
                    ]
                    if row.media_type != binding.media_type or (
                        isinstance(row, TxLink) and row.bridges_present != binding.bridges_present
                    ):
                        unavailable("metric media/bridge fields differ from measured binding")
            previous = self.watermark
            if previous and previous.context_token == context_token:
                if observed_at <= previous.stamp.observed_at:
                    invalid("duplicate or out-of-order link sample")
                if (
                    counter_epoch == previous.counter_epoch
                    and interval_started < previous.interval_started
                ):
                    invalid("measurement interval moved backwards within one counter epoch")
            self.sample = LinkSample(
                context_token,
                topology_key(snapshot),
                counter_epoch,
                interval_started,
                ReportStamp(uuid.uuid4().hex, observed_at, observed_at + lifetime),
                inventory,
                metrics,
            )
            self.watermark = self.sample
        except (EmosaError, TypeError, AttributeError):
            self.invalidate()
            raise
        return self.sample

    def current(self):
        snapshot = self.reports.current()
        sample = self.sample
        if sample is not None and (
            snapshot is None
            or snapshot.context_token != sample.context_token
            or topology_key(snapshot) != sample.topology
            or self.clock() >= sample.stamp.valid_until
        ):
            self.invalidate()
        return self.sample


class LinkMetricCoordinator:
    """Same-MID response, no Ack or fabricated invalid-neighbor fallback."""

    def __init__(self, measurements, send_frame, *, clock=time.monotonic):
        self.measurements, self.send_frame, self.clock = measurements, send_frame, clock
        self.binding = measurements.reports.binding
        self.counts = {}
        self.closed = False
        self.waiting = {}

    def _stamp(self):
        sample = None if self.closed else self.measurements.current()
        return sample.stamp if sample else None

    def handle(self, message, received_at, *, ingress, generation):
        if message.message_type != 5:
            return None
        if self.closed:
            unavailable("link metric coordinator closed")
        self.binding.check(message, ingress=ingress, generation=generation)
        if message.relay or any(t.kind in SECURITY_ENVELOPES for t in message.tlvs):
            invalid("link metric query must not be relayed")
        if type(received_at) not in (int, float) or not math.isfinite(received_at):
            invalid("invalid query receipt time")
        if not received_at <= self.clock() < received_at + 1:
            unavailable("link metric query response deadline expired")
        query = decode_query(message.tlvs)
        key = (message.mid, query)
        pending = self.waiting.get(key)
        if pending is not None:
            snapshot = self.measurements.reports.current()
            if (
                snapshot is None
                or snapshot.context_token != pending[2]
                or topology_key(snapshot) != pending[3]
                or self.clock() >= pending[1] + 1
            ):
                del self.waiting[key]
                unavailable("queued link query context or deadline expired")
            received_at = pending[1]  # Duplicates cannot extend the first deadline.
        # Remove before I/O, including a duplicate that now has measurements.
        # An uncertain partial send must not remain queued for tick() to retry.
        self.waiting.pop(key, None)
        if self._answer(message, query, received_at):
            return "neighbor_link_metric_response_sent"
        snapshot = self.measurements.reports.current()
        if snapshot is not None and key not in self.waiting and len(self.waiting) < 4:
            self.waiting[key] = (
                message,
                received_at,
                snapshot.context_token,
                topology_key(snapshot),
            )
        self.counts["measurement_unavailable"] = self.counts.get("measurement_unavailable", 0) + 1
        return "neighbor_measurement_unavailable"

    def _answer(self, message, query, received_at):
        sample = self.measurements.current()
        if sample is None:
            return False
        neighbors = {row.neighbor_al for row in sample.inventory}
        selected = neighbors if query.neighbor is None else {query.neighbor}
        if query.neighbor is not None and query.neighbor not in neighbors:
            tlvs = (Tlv(12, b"\0"),)
        else:
            kinds = {9, 10} if query.direction == 2 else {9 + query.direction}
            available = {(r.neighbor_al, r.tlv().kind): r.tlv() for r in sample.metrics}
            wanted = {(neighbor, kind) for neighbor in selected for kind in kinds}
            if not wanted <= available.keys():
                return False
            tlvs = tuple(available[key] for key in sorted(wanted))
        response = PreparedReport(
            6,
            message.mid,
            sample.stamp,
            min(received_at + 1, sample.stamp.valid_until),
            fragment_message(
                self.binding.controller_al, self.binding.local_al, 6, message.mid, tlvs
            ),
        )
        response.send(self.send_frame, self._stamp, clock=self.clock)
        self.counts["response_sent"] = self.counts.get("response_sent", 0) + 1
        return True

    def tick(self):
        snapshot = self.measurements.reports.current()
        for key, (message, received_at, context, topology) in tuple(self.waiting.items()):
            if (
                self.closed
                or snapshot is None
                or snapshot.context_token != context
                or topology_key(snapshot) != topology
                or self.clock() >= received_at + 1
            ):
                del self.waiting[key]
                self.counts["waiting_query_withdrawn"] = (
                    self.counts.get("waiting_query_withdrawn", 0) + 1
                )
                continue
            try:
                if self._answer(message, key[1], received_at):
                    del self.waiting[key]
            except (EmosaError, OSError):
                # A partial send cannot be retried as an unrelated fresh reply.
                del self.waiting[key]
                self.counts["waiting_query_send_failed"] = (
                    self.counts.get("waiting_query_send_failed", 0) + 1
                )

    def close(self):
        self.closed = True
        self.waiting.clear()
