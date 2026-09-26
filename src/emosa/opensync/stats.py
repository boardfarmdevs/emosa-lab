"""The pod's own statistics: OpenSync client reports over MQTT (``sts.Report``).

OpenSync 6.6 publishes them from ``owm`` (its OSW stats layer) through ``qm``
to the broker in ``AWLAN_Node.mqtt_settings``, one topic per pod
(``emosa.opensync.telemetry`` configures both). A raw client report carries,
per connected station and reporting period, the period's byte and frame deltas,
the last rates and an SNR (``rssi``). ``qm`` batches several periods into one
publish.

``PodStats`` keeps what was measured and nothing else:
- counters are summed per association (counter epoch). A period with a join
  or leave (its ``connect_count`` or ``disconnect_count``: events in that
  period, not an association ID) or a missed period (a gap between reports)
  starts a new epoch, so a total never silently lacks a period;
- OpenSync's encoder (``dppline.c``) sends a counter only when it is not zero,
  so an absent counter is zero, but only for a counter the platform measures.
  A counter counts as measured on this pod once it has been seen non-zero;
  until then it is unknown (``None``), and so is any total that includes an
  unknown period (hwsim has shown no retries or errors so far). When it
  becomes known, the station's next period starts a new epoch. ``tx_retries``
  is sent only when ``rx_retries`` is not zero (an upstream slip), so its
  absence never means zero;
- a station is current only while its last period is recent: three periods
  plus ``qm``'s batching (it publishes about once a minute). Each station
  carries the end of its last period, so its age is always known.

The report's ``nodeID`` is empty on the lab pods, so a report is bound to its
pod by topic, which only the pod's device certificate may publish to.
"""

import hashlib
import time
from dataclasses import dataclass
from importlib.resources import files

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.message import DecodeError

# Pinned upstream schema (opensync 78d8a71, src/lib/protobuf/opensync_stats.proto)
DESCRIPTOR_SHA256 = "604de5ab4e0f085c0ef15d44c49d2a36aa99d39c4e0dc6fd2b3701b61384ad8e"
PROTO_SHA256 = "0bf534da0d677d7bb41a4f591fcb1ad0b25a9f1c9eedf3abc5b69ed16ef9a6cd"
BANDS = {0: "2.4G", 1: "5G", 2: "5GL", 3: "5GU", 4: "6G", 5: "6GL", 6: "6GU"}
COUNTERS = (
    "tx_bytes",
    "rx_bytes",
    "tx_frames",
    "rx_frames",
    "tx_retries",
    "rx_retries",
    "tx_errors",
    "rx_errors",
)
NEVER_IMPLIED = {"tx_retries"}  # dppline.c sends it only together with rx_retries
MAX_PAYLOAD = 65536
MAX_STATIONS = 64
QM_BATCH = 60  # seconds: qm publishes the queued reports about once a minute


def report_type():
    data = files("emosa").joinpath("data/opensync_stats.desc").read_bytes()
    if hashlib.sha256(data).hexdigest() != DESCRIPTOR_SHA256:
        raise RuntimeError("OpenSync statistics descriptor differs from the pinned input")
    descriptor = descriptor_pb2.FileDescriptorSet.FromString(data)
    pool = descriptor_pool.DescriptorPool()
    for item in descriptor.file:
        pool.Add(item)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("sts.Report"))


Report = report_type()


@dataclass(frozen=True)
class SurveyStats:
    """The last raw on-channel survey sample: the channel's busy percentage."""

    band: str
    channel: int
    busy_percent: int
    duration_ms: int | None
    measured_at: float  # epoch seconds: the sample's time

    def public(self):
        return {
            "band": self.band,
            "channel": self.channel,
            "busy_percent": self.busy_percent,
            "duration_ms": self.duration_ms,
            "measured_at": self.measured_at,
        }


@dataclass(frozen=True)
class StationStats:
    """One station as the pod measured it, lower-case MAC; counters since ``epoch_start``."""

    mac: str
    ssid: str
    band: str
    channel: int
    measured_at: float  # wall-clock end of the last period (the report's timestamp)
    periods: int  # reporting periods summed in this counter epoch
    epoch_start: float  # wall-clock start of the counter epoch's first period
    tx_rate_mbps: float | None
    rx_rate_mbps: float | None
    snr_db: int | None
    tx_bytes: int | None
    rx_bytes: int | None
    tx_frames: int | None
    rx_frames: int | None
    tx_retries: int | None
    rx_retries: int | None
    tx_errors: int | None
    rx_errors: int | None

    def public(self):
        return {k: v for k, v in self.__dict__.items() if k != "mac"}


class PodStats:
    """Bounded cache of one pod's client reports; fed from the MQTT thread via the loop."""

    def __init__(self, topic, *, interval, clock=time.time, batch=QM_BATCH):
        self.topic, self.interval, self.clock = topic, interval, clock
        self.lifetime = 3 * interval + batch
        self.last_timestamp = {}  # band -> ms of the last accepted client report
        self.stations = {}  # mac -> StationStats
        self.surveys = {}  # channel -> SurveyStats (raw on-channel samples)
        self.measured = set()  # counters seen non-zero on this pod
        self.accepted = self.rejected = self.gaps = 0
        self.last_error = None
        self.last_report_at = None

    def receive(self, topic, payload, *, retained=False):
        """One MQTT message; False (and counted) when it is not a usable report."""
        try:
            if retained or topic != self.topic or not 1 <= len(payload) <= MAX_PAYLOAD:
                raise ValueError("not a live report on this pod's topic")
            report = Report.FromString(payload)
            if not report.IsInitialized():
                raise ValueError("incomplete report")
            for radio in sorted(report.clients, key=lambda r: r.timestamp_ms):
                self._client_report(radio)
            for survey in report.survey:
                self._survey(survey)
        except (ValueError, DecodeError) as exc:
            self.rejected += 1
            self.last_error = str(exc)
            return False
        self.accepted += 1
        self.last_report_at = self.clock()
        return True

    def _client_report(self, radio):
        if radio.band not in BANDS or not radio.HasField("timestamp_ms"):
            raise ValueError("client report without band or time")
        band, stamp = BANDS[radio.band], radio.timestamp_ms
        last = self.last_timestamp.get(band)
        if last is not None and stamp <= last:
            raise ValueError("duplicate or out-of-order client report")
        # A missed period breaks every running total on this band.
        broken = last is None or stamp - last > 1500 * self.interval
        if last is not None and broken:
            self.gaps += 1
        self.last_timestamp[band] = stamp
        if len(radio.client_list) > MAX_STATIONS:
            raise ValueError("station inventory exceeds the local bound")
        end = stamp / 1000
        for client in radio.client_list:
            mac = client.mac_address.lower()
            if len(mac) != 17 or not client.HasField("stats") or not client.connected:
                continue  # a leave or an entry without measurements: nothing to add
            stats = client.stats
            present = {n for n in COUNTERS if stats.HasField(n)}
            self.measured |= present - NEVER_IMPLIED
            deltas = {
                n: getattr(stats, n) if n in present else (0 if n in self.measured else None)
                for n in COUNTERS
            }
            old = self.stations.get(mac)
            events = client.connect_count or client.disconnect_count
            # A counter that has become known would stay unknown for the rest of
            # an epoch that began before it was: start a new epoch instead.
            now_known = old is not None and any(
                getattr(old, n) is None and deltas[n] is not None
                for n in COUNTERS
                if n not in NEVER_IMPLIED
            )
            same = (
                old is not None
                and not broken
                and not events
                and not now_known
                and old.band == band
                and end - old.measured_at <= 1.5 * self.interval
            )
            if same:
                totals = {
                    n: None
                    if deltas[n] is None or getattr(old, n) is None
                    else getattr(old, n) + deltas[n]
                    for n in COUNTERS
                }
                periods, epoch_start = old.periods + 1, old.epoch_start
            else:
                totals, periods = deltas, 1
                period = client.duration_ms / 1000 if client.HasField("duration_ms") else None
                epoch_start = end - (period or self.interval)
            self.stations[mac] = StationStats(
                mac=mac,
                ssid=client.ssid,
                band=band,
                channel=radio.channel,
                measured_at=end,
                periods=periods,
                epoch_start=epoch_start,
                tx_rate_mbps=stats.tx_rate if stats.HasField("tx_rate") else None,
                rx_rate_mbps=stats.rx_rate if stats.HasField("rx_rate") else None,
                snr_db=stats.rssi if stats.HasField("rssi") else None,
                **totals,
            )

    def _survey(self, survey):
        # ON_CHANNEL (0) raw samples of the operating channel; busy is a percentage
        if (
            survey.survey_type != 0
            or survey.band not in BANDS
            or not survey.HasField("timestamp_ms")
        ):
            return
        for sample in survey.survey_list:
            if not sample.HasField("busy") or not 0 <= sample.busy <= 100:
                continue
            at = (survey.timestamp_ms + sample.offset_ms) / 1000
            old = self.surveys.get(sample.channel)
            if old is not None and at <= old.measured_at:
                continue
            self.surveys[sample.channel] = SurveyStats(
                BANDS[survey.band],
                sample.channel,
                sample.busy,
                sample.duration_ms if sample.HasField("duration_ms") else None,
                at,
            )

    def current(self):
        """Stations whose last period ended within the lifetime, by MAC."""
        now = self.clock()
        stale = [m for m, s in self.stations.items() if now - s.measured_at > self.lifetime]
        for mac in stale:
            del self.stations[mac]
        return dict(self.stations)

    def status(self):
        return {
            "topic": self.topic,
            "reports_accepted": self.accepted,
            "reports_rejected": self.rejected,
            "gaps": self.gaps,
            "measured_counters": sorted(self.measured),
            "last_error": self.last_error,
            "last_report_at": self.last_report_at,
            "stations": {mac: s.public() for mac, s in sorted(self.current().items())},
            "surveys": {str(c): s.public() for c, s in sorted(self.surveys.items())},
        }
