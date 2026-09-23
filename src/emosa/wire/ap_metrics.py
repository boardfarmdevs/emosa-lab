"""Guarded AP/radio/STA report assembly for the selected non-MLD radio.

EasyMesh 6.1 §10.2.1 and Tables 44/45/47/58/83/84/85/96 define
composition and wire layouts. This internal handoff does not qualify a sensor.
ESP octets and Data Elements-derived values must already have their specified
representation: pending source definitions must not become guessed conversions.
"""

import math
import struct
import time
import uuid
from dataclasses import dataclass

from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import SECURITY_ENVELOPES
from emosa.wire.cmdu import Tlv, fragment_message, invalid, uint
from emosa.wire.disassociation import TrafficCounters, unicast
from emosa.wire.reports import PreparedReport, ReportStamp

MAX_STATIONS = 32  # Local budget; not a protocol maximum.
AC_ORDER = ("BE", "BK", "VO", "VI")  # Table 45 order, not numeric IEEE AC order.


def unavailable(message):
    raise EmosaError(Reason.NOT_READY, message)


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        invalid("finite measurement time required")
    return value


@dataclass(frozen=True)
class APMetrics:
    bssid: bytes
    utilization: int
    station_count: int
    # Explicit, already encoded 3-octet ESP values. No PHY/idle-time inference.
    esp: tuple[tuple[str, bytes], ...]

    def tlv(self):
        unicast(self.bssid)
        uint(self.utilization, 8, "channel utilization")
        uint(self.station_count, 16, "associated station count")
        if type(self.esp) is not tuple or not 1 <= len(self.esp) <= 4:
            invalid("explicit bounded ESP information required")
        values = {}
        for entry in self.esp:
            if type(entry) is not tuple or len(entry) != 2:
                invalid("typed access category and encoded ESP required")
            ac, encoded = entry
            if (
                ac not in AC_ORDER
                or ac in values
                or type(encoded) is not bytes
                or len(encoded) != 3
            ):
                invalid("duplicate, unknown or malformed ESP field")
            values[ac] = encoded
        if "BE" not in values:
            invalid("best-effort ESP is mandatory")
        flags = sum(0x80 >> i for i, ac in enumerate(AC_ORDER) if ac in values)
        return Tlv(
            0x94,
            self.bssid
            + struct.pack("!BHB", self.utilization, self.station_count, flags)
            + b"".join(values[ac] for ac in AC_ORDER if ac in values),
        )


@dataclass(frozen=True)
class APExtendedMetrics:
    bssid: bytes
    # Already encoded in the explicitly stated advertised byte units.
    # Table 84 order: unicast TX/RX, multicast TX/RX, broadcast TX/RX.
    byte_counts: tuple[int, int, int, int, int, int]
    byte_units: int

    def tlv(self):
        unicast(self.bssid)
        if type(self.byte_counts) is not tuple or len(self.byte_counts) != 6:
            invalid("six explicit AP byte counters required")
        for value in self.byte_counts:
            uint(value, 32, "encoded AP byte counter")
        if type(self.byte_units) is not int or self.byte_units not in (0, 1, 2):
            invalid("explicit AP counter units required")
        return Tlv(0xC7, self.bssid + struct.pack("!6I", *self.byte_counts))


@dataclass(frozen=True)
class RadioMetrics:
    ruid: bytes
    # Table 83 order. No guessed dBm conversion or percentages.
    noise_encoded: int
    transmit_encoded: int
    receive_self_encoded: int
    receive_other_encoded: int

    def tlv(self):
        unicast(self.ruid)
        values = (
            self.noise_encoded,
            self.transmit_encoded,
            self.receive_self_encoded,
            self.receive_other_encoded,
        )
        for value in values:
            uint(value, 8, "encoded radio metric")
        return Tlv(0xC6, self.ruid + bytes(values))


@dataclass(frozen=True)
class StationLinkMetrics:
    earliest_measurement: float
    downlink_mac_mbps: int
    uplink_mac_mbps: int
    rcpi: int
    # Table 85 values, already encoded in the selected Data Elements units.
    last_downlink_encoded: int
    last_uplink_encoded: int
    receive_utilization_encoded: int
    transmit_utilization_encoded: int

    def tlvs(self, station, bssid, sent_at):
        unicast(station)
        unicast(bssid)
        age = finite(sent_at) - finite(self.earliest_measurement)
        if not 0 <= age <= 12:
            unavailable("STA rate estimate outside the selected measurement-age budget")
        base = (int(age * 1000), self.downlink_mac_mbps, self.uplink_mac_mbps)
        extended = (
            self.last_downlink_encoded,
            self.last_uplink_encoded,
            self.receive_utilization_encoded,
            self.transmit_utilization_encoded,
        )
        for value in (*base, *extended):
            uint(value, 32, "STA link metric")
        uint(self.rcpi, 8, "uplink RCPI")
        if self.rcpi > 220:
            invalid("reserved uplink RCPI")
        prefix = station + b"\1" + bssid
        return (
            Tlv(0x96, prefix + struct.pack("!3IB", *base, self.rcpi)),
            Tlv(0xC8, prefix + struct.pack("!4I", *extended)),
        )


@dataclass(frozen=True)
class StationMetrics:
    station: bytes
    association_id: str
    traffic: TrafficCounters | None
    link: StationLinkMetrics | None
    # None means unavailable. An explicit empty tuple means no known TID queues.
    # Queue-size octets retain IEEE's encoding, not a guessed number of bytes.
    wifi6_queues: tuple[tuple[int, int], ...] | None

    def wifi6_tlv(self):
        unicast(self.station)
        if self.wifi6_queues is None:
            unavailable("requested Wi-Fi 6 status is unavailable")
        if type(self.wifi6_queues) is not tuple or len(self.wifi6_queues) > 8:
            invalid("bounded data-TID queue inventory required")
        seen, encoded = set(), bytearray()
        for entry in self.wifi6_queues:
            if type(entry) is not tuple or len(entry) != 2:
                invalid("explicit TID and encoded queue size required")
            tid, size = entry
            uint(tid, 3, "data TID")
            uint(size, 8, "encoded queue size")
            if tid in seen:
                invalid("duplicate queue TID")
            seen.add(tid)
            encoded.extend((tid, size))
        return Tlv(0xB0, self.station + bytes((len(seen),)) + bytes(encoded))


@dataclass(frozen=True)
class APMetricBundle:
    ap: APMetrics
    extended: APExtendedMetrics
    radio: RadioMetrics
    stations: tuple[StationMetrics, ...]


@dataclass(frozen=True)
class APMetricSample:
    context: str
    control_token: str
    counter_epoch: str
    stamp: ReportStamp
    bundle: APMetricBundle


class APMetricSource:
    """Complete membership and explicit observations from a qualified publisher.

    The control revision binds the membership, capabilities and byte units used
    to construct the report. Any change withdraws the sample until a new full
    observation is published. Publisher association IDs/epochs must originate
    from actual session lifetimes; this guard cannot invent them from a MAC.
    """

    def __init__(self, reports, *, clock=time.monotonic):
        self.reports, self.clock = reports, clock
        self.sample = self.watermark = None

    def invalidate(self):
        self.sample = None

    def publish(
        self, *, context, counter_epoch, observed_at, bundle, inventory_complete, lifetime=2
    ):
        try:
            snapshot = self.reports.current()
            now = self.clock()
            if snapshot is None or context != snapshot.context_token:
                unavailable("AP measurements lack current control authority")
            if inventory_complete is not True or not snapshot.topology.inventory_complete:
                unavailable("complete current AP/STA membership required")
            if not 0 <= finite(observed_at) <= now < observed_at + finite(lifetime) or not (
                0 < lifetime <= 2
            ):
                unavailable("AP measurement lease expired or invalid")
            if type(counter_epoch) is not str or not 1 <= len(counter_epoch) <= 128:
                invalid("explicit AP counter epoch required")
            if type(bundle) is not APMetricBundle or (
                type(bundle.ap) is not APMetrics
                or type(bundle.extended) is not APExtendedMetrics
                or type(bundle.radio) is not RadioMetrics
            ):
                invalid("typed AP, extended and radio observations required")
            bundle.ap.tlv()
            bundle.extended.tlv()
            bundle.radio.tlv()
            radios = snapshot.topology.operational.radios
            if len(radios) != 1 or len(radios[0].bsses) != 1:
                unavailable("selected non-MLD sole-radio/BSS measurement scope required")
            radio = radios[0]
            bssid = radio.bsses[0].ap_mac
            if (bundle.radio.ruid, bundle.ap.bssid, bundle.extended.bssid) != (
                radio.ruid,
                bssid,
                bssid,
            ):
                unavailable("measurement identity differs from observed radio/BSS")
            units = snapshot.capabilities.profile2.byte_counter_units
            if units != 0 or bundle.extended.byte_units != units:
                unavailable("selected Profile-1 source requires consistent byte units")
            if type(bundle.stations) is not tuple or len(bundle.stations) > MAX_STATIONS:
                invalid("STA measurement inventory exceeds local budget")
            expected = {
                (b.bssid, c.mac) for b in snapshot.topology.clients.bsses for c in b.clients
            }
            actual = set()
            for station in bundle.stations:
                if type(station) is not StationMetrics:
                    invalid("typed station observations required")
                unicast(station.station)
                if (bssid, station.station) in actual or (
                    type(station.association_id) is not str
                    or not 1 <= len(station.association_id) <= 128
                ):
                    invalid("unique station and bounded association identity required")
                actual.add((bssid, station.station))
                if station.traffic is not None:
                    if type(station.traffic) is not TrafficCounters:
                        invalid("qualified traffic counter record required")
                    station.traffic.tlv(station.station, profile=1, byte_units=units)
                if station.link is not None:
                    if type(station.link) is not StationLinkMetrics or not (
                        observed_at - 10 <= finite(station.link.earliest_measurement) <= observed_at
                    ):
                        invalid("qualified STA link measurement interval required")
                    station.link.tlvs(station.station, bssid, now)
                if station.wifi6_queues is not None:
                    station.wifi6_tlv()
            if actual != expected or bundle.ap.station_count != len(actual):
                unavailable("metric inventory/count differs from current complete membership")
            mark = (observed_at, context, counter_epoch, snapshot.stamp.token, bundle)
            if self.watermark is not None and observed_at <= self.watermark[0]:
                if mark == self.watermark and self.sample is not None:
                    return self.current()  # Never extend an existing sample's lease.
                unavailable("replayed or conflicting AP observation")
            self.watermark = mark
            self.sample = APMetricSample(
                context,
                snapshot.stamp.token,
                counter_epoch,
                ReportStamp(uuid.uuid4().hex, observed_at, observed_at + lifetime),
                bundle,
            )
            return self.sample
        except (EmosaError, TypeError, AttributeError, ValueError):
            self.invalidate()
            raise

    def current(self):
        snapshot = self.reports.current()
        sample = self.sample
        if sample is not None and (
            snapshot is None
            or not snapshot.topology.inventory_complete
            or snapshot.context_token != sample.context
            or snapshot.stamp.token != sample.control_token
            or self.clock() >= sample.stamp.valid_until
        ):
            self.invalidate()
        return self.sample


class APMetricCoordinator:
    """Complete selected AP responses; send success is not controller receipt."""

    def __init__(self, measurements, send_frame, mids, *, admitted, policy, clock=time.monotonic):
        self.measurements, self.send_frame, self.mids = measurements, send_frame, mids
        self.binding = measurements.reports.binding
        self.admitted, self.policy, self.clock = admitted, policy, clock
        self.closed = False
        self.counts = {}

    def record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        return event

    def stamp(self):
        if self.closed or not self.admitted():
            return None
        sample = self.measurements.current()
        return sample.stamp if sample else None

    def send(self, mid, deadline, *, include_radio, policy):
        sample = self.measurements.current()
        if self.stamp() is None or sample is None:
            unavailable("complete admitted AP measurement source unavailable")
        bundle = sample.bundle
        metrics = policy.get("metrics", {})
        radios = metrics.get("radios", [])
        selected = next((r for r in radios if r["ruid"] == bundle.radio.ruid.hex()), {})
        tlvs = [bundle.ap.tlv(), bundle.extended.tlv()]
        if include_radio:
            tlvs.append(bundle.radio.tlv())
        for station in bundle.stations:
            if selected.get("include_traffic", False):
                if station.traffic is None:
                    unavailable("requested STA traffic counters are unavailable")
                tlvs.append(station.traffic.tlv(station.station, profile=1, byte_units=0))
            if selected.get("include_link", False):
                if station.link is None:
                    unavailable("requested STA link/extended observations are unavailable")
                tlvs.extend(station.link.tlvs(station.station, bundle.ap.bssid, self.clock()))
            if selected.get("include_wifi6_status", False):
                tlvs.append(station.wifi6_tlv())
        PreparedReport(
            0x800C,
            mid,
            sample.stamp,
            min(deadline, sample.stamp.valid_until),
            fragment_message(self.binding.controller_al, self.binding.local_al, 0x800C, mid, tlvs),
        ).send(self.send_frame, self.stamp, clock=self.clock)
        self.record("ap_metric_report_transmitted")

    def handle(self, message, received_at, *, ingress, generation):
        if message.message_type != 0x800B:
            return None
        self.binding.check(message, ingress=ingress, generation=generation)
        if message.relay or any(t.kind in SECURITY_ENVELOPES for t in message.tlvs):
            invalid("invalid AP metric query envelope")
        if not finite(received_at) <= self.clock() < received_at + 1:
            unavailable("AP metric response deadline expired")
        query = [t.value for t in message.tlvs if t.kind == 0x93]
        radios = [t.value for t in message.tlvs if t.kind == 0x82]
        if (
            len(query) != 1
            or len(radios) > 1
            or any(t.kind not in (0x93, 0x82) for t in message.tlvs)
        ):
            invalid("one AP query and at most one selected radio required")
        if len(query[0]) != 7 or query[0][0] != 1:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "selected query requires one BSSID")
        unicast(query[0][1:])
        for radio in radios:
            unicast(radio)
        sample = self.measurements.current()
        if sample is None:
            return self.record("ap_measurements_unavailable")
        if query[0][1:] != sample.bundle.ap.bssid or (
            radios and radios[0] != sample.bundle.radio.ruid
        ):
            unavailable("query outside represented AP/radio")
        try:
            self.send(
                message.mid, received_at + 1, include_radio=bool(radios), policy=self.policy()
            )
        except EmosaError as exc:
            if exc.code != Reason.NOT_READY:
                raise
            return self.record("ap_measurements_unavailable")
        return self.record("ap_metric_query_answered")

    def periodic(self, policy, due):
        # One current report, never one burst per missed historical interval.
        self.send(self.mids.next(), due + 1, include_radio=True, policy=policy)
        self.record("periodic_ap_metric_report_transmitted")

    def close(self):
        self.closed = True
