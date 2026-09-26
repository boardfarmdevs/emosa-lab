"""AP metrics from the pod's own statistics (spec §3.8).

With telemetry the agent knows, from the pod's own reports (``PodStats``):
the channel's busy percentage (a raw on-channel survey) and, per station, the
last SNR and rates. It answers AP Metrics Queries with them and, when the
controller's Metric Reporting Policy sets an interval, reports them
unsolicited at that interval, as a native agent does. RDK's controller learns
a client's signal only this way.

Per operating BSS of the pod, a Profile-1 AP Metrics Response carries:

- the AP Metrics TLV (``0x94``): channel utilization measured (busy percent
  scaled to 0..255), the BSS's station count, and the best-effort Estimated
  Service Parameters **declared** by the profile (reported as configured, not
  measured);
- when the radio's policy asks for link metrics, an Associated STA Link
  Metrics TLV (``0x96``) per station with a fresh report: its age, the last
  downlink/uplink rates (Mb/s) and the uplink RCPI from the SNR;
- when it asks for traffic statistics, an Associated STA Traffic Stats TLV
  (``0xA2``) per station whose counters the pod measures.

Nothing is filled in: without a fresh survey there is no response, and a
station without a fresh report is left out of the link and traffic TLVs.
"""

import struct
import time

from emosa.errors import EmosaError, Reason
from emosa.wire.ap_metrics import APMetrics
from emosa.wire.autoconfiguration import SECURITY_ENVELOPES
from emosa.wire.cmdu import Tlv, fragment_message, invalid
from emosa.wire.disassociation import TrafficCounters
from emosa.wire.reports import PreparedReport

# OpenSync reports SNR = signal - its fixed noise floor
# (osw_channel_nf_20mhz_fixup(0) = -96 dBm); the RSSI is the inverse.
NOISE_FLOOR_DBM = -96


def rcpi(snr_db):
    """Uplink RCPI (IEEE 802.11, 0..220) from the pod's SNR."""
    rssi = snr_db + NOISE_FLOOR_DBM
    return max(0, min(220, 2 * (rssi + 110)))


def utilization(busy_percent):
    return max(0, min(255, round(busy_percent * 255 / 100)))


def unavailable(message):
    raise EmosaError(Reason.NOT_READY, message)


class PodMetricReporter:
    """AP Metrics Responses from the pod's statistics, queried and periodic."""

    def __init__(
        self,
        source,
        stats,
        send_frame,
        mids,
        *,
        admitted,
        policy,
        esp_be,
        freshness,
        clock=time.monotonic,
        wall=time.time,
    ):
        if type(esp_be) is not bytes or len(esp_be) != 3:
            invalid("declared best-effort ESP must be three octets")
        self.source, self.binding, self.stats = source, source.binding, stats
        self.send_frame, self.mids = send_frame, mids
        self.admitted, self.policy, self.esp_be = admitted, policy, esp_be
        self.freshness, self.clock, self.wall = freshness, clock, wall
        self.closed = False
        self.counts = {}

    def record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        return event

    def stamp(self):
        if self.closed or not self.admitted():
            return None
        snapshot = self.source.current()
        return snapshot.stamp if snapshot else None

    def _radio_policy(self, policy, ruid):
        radios = policy.get("metrics", {}).get("radios", [])
        return next((r for r in radios if r.get("ruid") == ruid.hex()), {})

    def tlvs(self, policy, bssids=None):
        """The response's TLVs for the pod's BSSes (all, or the queried ones)."""
        snapshot = self.source.current()
        if snapshot is None:
            unavailable("the pod's state is not available")
        radios = snapshot.topology.operational.radios
        if len(radios) != 1:
            unavailable("one represented radio required")
        radio, now = radios[0], self.wall()
        clients = {
            b.bssid: tuple(c.mac for c in b.clients) for b in snapshot.topology.clients.bsses
        }
        channel = next((s.channel for s in self.stats.surveys.values()), None)
        survey = self.stats.surveys.get(channel) if channel is not None else None
        if survey is None or now - survey.measured_at > self.freshness:
            unavailable("no fresh channel survey from the pod")
        util = utilization(survey.busy_percent)
        selected = self._radio_policy(policy, radio.ruid)
        result = []
        for bss in radio.bsses:
            bssid = bss.ap_mac
            if bssids is not None and bssid not in bssids:
                continue
            stations = clients.get(bssid, ())
            result.append(APMetrics(bssid, util, len(stations), (("BE", self.esp_be),)).tlv())
            for mac in stations:
                station = self.stats.stations.get(mac.hex(":"))
                if station is None or now - station.measured_at > self.freshness:
                    continue
                if selected.get("include_traffic", False):
                    counters = [
                        getattr(station, name)
                        for name in (
                            "tx_bytes",
                            "rx_bytes",
                            "tx_frames",
                            "rx_frames",
                            "tx_errors",
                            "rx_errors",
                            "tx_retries",
                        )
                    ]
                    if None not in counters:
                        result.append(TrafficCounters(*counters).tlv(mac, profile=1, byte_units=0))
                if selected.get("include_link", False) and station.snr_db is not None:
                    age_ms = int(max(0.0, now - station.measured_at) * 1000)
                    rates = (
                        int(station.tx_rate_mbps or 0),  # the AP's rate to the station
                        int(station.rx_rate_mbps or 0),
                    )
                    result.append(
                        Tlv(
                            0x96,
                            mac
                            + b"\1"
                            + bssid
                            + struct.pack("!3IB", age_ms, *rates, rcpi(station.snr_db)),
                        )
                    )
        if not result:
            unavailable("no queried BSS of the pod")
        return result

    def send(self, mid, deadline, policy, bssids=None):
        if self.stamp() is None:
            unavailable("the agent is not admitted or its state is unavailable")
        snapshot = self.source.current()
        tlvs = self.tlvs(policy, bssids)
        PreparedReport(
            0x800C,
            mid,
            snapshot.stamp,
            min(deadline, snapshot.stamp.valid_until),
            fragment_message(self.binding.controller_al, self.binding.local_al, 0x800C, mid, tlvs),
        ).send(self.send_frame, self.stamp, clock=self.clock)

    def handle(self, message, received_at, *, ingress, generation):
        if message.message_type != 0x800B:
            return None
        self.binding.check(message, ingress=ingress, generation=generation)
        if message.relay or any(t.kind in SECURITY_ENVELOPES for t in message.tlvs):
            invalid("invalid AP metric query envelope")
        if not received_at <= self.clock() < received_at + 1:
            unavailable("AP metric response deadline expired")
        query = [t.value for t in message.tlvs if t.kind == 0x93]
        if len(query) != 1 or len(query[0]) < 1 or len(query[0]) != 1 + 6 * query[0][0]:
            invalid("one AP Metric Query TLV required")
        bssids = {query[0][1 + 6 * i : 7 + 6 * i] for i in range(query[0][0])}
        try:
            self.send(message.mid, received_at + 1, self.policy(), bssids)
        except EmosaError as exc:
            if exc.code != Reason.NOT_READY:
                raise
            return self.record("ap_measurements_unavailable")
        return self.record("ap_metric_query_answered")

    def periodic(self, policy, due):
        # One current report, never one burst per missed interval.
        self.send(self.mids.next(), max(due, self.clock()) + 1, policy)
        self.record("periodic_ap_metric_report_transmitted")

    def status(self):
        return {"counts": dict(self.counts), "esp_be": self.esp_be.hex(), "esp_source": "profile"}

    def close(self):
        self.closed = True
