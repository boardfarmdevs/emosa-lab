"""The pod's own statistics: the telemetry scope's write, and the report cache."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from emosa.agent.pod import telemetry_intent
from emosa.agent.telemetry import DEADLINE, TelemetrySetup
from emosa.clock import ManualClock
from emosa.errors import EmosaError, Reason
from emosa.model import State
from emosa.opensync.schema import Schema, reference_path
from emosa.opensync.stats import PodStats, Report
from emosa.opensync.telemetry import TelemetryBackend, TelemetryIntent
from emosa.secrets import SecretStore
from emosa.store import Store

pytestmark = pytest.mark.unit

SERIAL = "MVXPOD023F87E628DD"
TOPIC = f"emosa/stats/{SERIAL}"
RECORDED = Path("tests/fixtures/opensync/pod-6.6.1-hwsim-client-stats.hex")
NODE = "00000000-0000-4000-8000-0000000000b1"
STATS_ROW = "00000000-0000-4000-8000-0000000000c1"
STA1, STA2 = "02:00:00:00:12:00", "02:00:00:00:13:00"


def recorded():
    """Three publishes captured from pod-1 (emosa-osl-0925, 2026-09-25)."""
    return [
        (topic, bytes.fromhex(data))
        for topic, data in (line.split() for line in RECORDED.read_text().splitlines())
    ]


def report(stamp_ms, *clients, band=0, channel=6):
    """A raw client report: clients are (mac, tx_bytes, rx_bytes, connects) tuples."""
    r = Report(nodeID="")
    radio = r.clients.add(band=band, timestamp_ms=stamp_ms, channel=channel)
    for mac, tx, rx, connects in clients:
        c = radio.client_list.add(
            mac_address=mac, ssid="emosa-mesh", connected=True, connect_count=connects
        )
        c.duration_ms = 10000
        c.stats.tx_bytes, c.stats.rx_bytes = tx, rx
        c.stats.tx_frames, c.stats.rx_frames = tx // 100, rx // 100
        c.stats.tx_rate, c.stats.rx_rate, c.stats.rssi = 72.2, 65.0, 60
    return r.SerializeToString()


def cache(now):
    return PodStats(TOPIC, interval=10, clock=lambda: now)


def test_recorded_reports_decode_into_measured_station_totals():
    stats = cache(1790313745.0)
    for topic, payload in recorded():
        assert stats.receive(topic, payload)
    assert stats.accepted == 3 and stats.rejected == 0
    # the first publish's period was followed by a missing one: a new epoch after it
    assert stats.gaps == 1
    stations = stats.current()
    assert set(stations) == {STA1, STA2}
    one = stations[STA1]
    assert one.periods == 11 and one.channel == 6 and one.band == "2.4G"
    assert (one.tx_bytes, one.rx_bytes) == (0, 3 * 26)  # the summed per-period deltas
    assert one.tx_rate_mbps == 1.0 and one.snr_db == 76
    # hwsim reports no retries or errors: they stay unknown, never zero
    assert one.tx_retries is None and one.rx_errors is None


def test_a_join_or_a_missing_period_starts_a_new_counter_epoch():
    stats = cache(1000.0)
    assert stats.receive(TOPIC, report(960_000, (STA1, 100, 200, 1)))
    assert stats.receive(TOPIC, report(970_000, (STA1, 100, 200, 0)))
    assert stats.current()[STA1].tx_bytes == 200 and stats.current()[STA1].periods == 2
    assert stats.receive(TOPIC, report(980_000, (STA1, 100, 200, 1)))  # joined again
    assert stats.current()[STA1].tx_bytes == 100 and stats.current()[STA1].periods == 1
    assert stats.receive(TOPIC, report(1000_000, (STA1, 100, 200, 0)))  # 990 s missing
    assert stats.current()[STA1].periods == 1 and stats.gaps == 1


def strip(payload, *fields):
    r = Report.FromString(payload)
    for client in r.clients[0].client_list:
        for field in fields:
            client.stats.ClearField(field)
    return r.SerializeToString()


def test_an_absent_counter_is_zero_only_once_the_pod_has_measured_it():
    # OpenSync sends a counter only when it is not zero (dppline.c)
    stats = cache(1000.0)
    stats.receive(TOPIC, strip(report(970_000, (STA1, 100, 200, 1)), "tx_bytes", "tx_frames"))
    first = stats.current()[STA1]
    assert first.tx_bytes is None and first.rx_bytes == 200  # tx never seen: unknown
    # tx_bytes becomes known: a new epoch, rather than an unknown total for good
    stats.receive(TOPIC, report(980_000, (STA1, 100, 200, 0)))
    second = stats.current()[STA1]
    assert (second.tx_bytes, second.rx_bytes, second.periods) == (100, 200, 1)
    stats.receive(TOPIC, strip(report(990_000, (STA1, 100, 200, 0)), "tx_bytes"))
    third = stats.current()[STA1]
    assert (third.tx_bytes, third.rx_bytes, third.periods) == (100, 400, 2)  # absent: zero
    assert "tx_bytes" in stats.status()["measured_counters"]


def test_tx_retries_is_never_implied_by_its_absence():
    stats = cache(1000.0)
    r = Report.FromString(report(990_000, (STA1, 100, 200, 1)))
    r.clients[0].client_list[0].stats.tx_retries = 3
    r.clients[0].client_list[0].stats.rx_retries = 2
    stats.receive(TOPIC, r.SerializeToString())
    assert stats.current()[STA1].tx_retries == 3
    stats.receive(TOPIC, report(1000_000, (STA1, 100, 200, 0)))
    assert stats.current()[STA1].tx_retries is None and stats.current()[STA1].rx_retries == 2


def test_only_live_in_order_reports_on_the_pods_topic_are_used():
    stats = cache(1000.0)
    assert not stats.receive("emosa/stats/OTHER", report(990_000, (STA1, 1, 1, 1)))
    assert not stats.receive(TOPIC, report(990_000, (STA1, 1, 1, 1)), retained=True)
    assert not stats.receive(TOPIC, b"\xff" * 8)
    assert stats.receive(TOPIC, report(990_000, (STA1, 1, 1, 1)))
    assert not stats.receive(TOPIC, report(990_000, (STA1, 1, 1, 0)))  # again: duplicate
    assert stats.rejected == 4 and stats.accepted == 1


def test_a_station_without_a_recent_period_is_not_current():
    now = [1000.0]
    stats = PodStats(TOPIC, interval=10, clock=lambda: now[0])
    stats.receive(TOPIC, report(995_000, (STA1, 1, 1, 1)))
    assert STA1 in stats.current()
    now[0] = 1085.0  # within three periods and qm's one-minute batching
    assert STA1 in stats.current()
    now[0] = 1096.0
    assert stats.current() == {} and stats.status()["stations"] == {}


class Pod:
    """The pod's database as the telemetry scope sees it (pinned OpenSync schema)."""

    def __init__(self, mqtt=()):
        self.schema = Schema(json.loads(reference_path().read_text()))
        self.generation, self.sent = 1, []
        self.restart("00000000-0000-4000-8000-0000000000a1", mqtt)

    def restart(self, start, mqtt=()):
        self.generation += 1
        self.tables = {
            "AWLAN_Node": {
                NODE: {
                    "serial_number": SERIAL,
                    "mqtt_settings": ["map", [list(p) for p in sorted(mqtt)]],
                }
            },
            "Wifi_Radio_Config": {start: {"if_name": "phy3", "freq_band": "2.4G"}},
            "Wifi_Stats_Config": {},
        }

    async def snapshot(self):
        return {
            "tables": copy.deepcopy(self.tables),
            "generation": self.generation,
            "revision": 1,
            "ready": True,
            "schema": self.schema,
        }

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(operations)
        results = []
        for op in operations:
            if op["op"] == "wait":
                row = self.tables["AWLAN_Node"][NODE]
                if row["mqtt_settings"] != op["rows"][0]["mqtt_settings"]:
                    return [{"error": "timed out"}]
                results.append({})
            elif op["op"] == "update":
                self.tables["AWLAN_Node"][NODE].update(op["row"])
                results.append({"count": 1})
            else:
                self.tables["Wifi_Stats_Config"][STATS_ROW] = op["row"]
                results.append({"uuid": ["uuid", STATS_ROW]})
        return results


def setup(tmp_path, pod):
    intent = TelemetryIntent("pod-1", "10.101.0.1", 8883, TOPIC)
    backend = TelemetryBackend("pod-1", pod, serial=SERIAL, radio_type="2.4G")
    clock = ManualClock()
    vault = SecretStore(tmp_path / "secrets")
    return TelemetrySetup(
        "pod-1", backend, Store(tmp_path / "telemetry"), vault, intent, run_id="r", clock=clock
    ), clock


def test_one_guarded_write_sets_the_broker_and_a_raw_client_report(tmp_path):
    pod = Pod()
    telemetry, _ = setup(tmp_path, pod)
    asyncio.run(telemetry.tick())
    (sent,) = pod.sent
    assert [op["op"] for op in sent] == ["wait", "update", "insert"]
    assert sent[0]["rows"][0] == {"serial_number": SERIAL, "mqtt_settings": ["map", []]}
    assert dict(sent[1]["row"]["mqtt_settings"][1]) == {
        "broker": "10.101.0.1",
        "port": "8883",
        "topics": TOPIC,
        "qos": "0",
        "compress": "none",
    }
    assert sent[2]["row"] == {
        "stats_type": "client",
        "radio_type": "2.4G",
        "report_type": "raw",
        "reporting_interval": 10,
        "sampling_interval": 5,
    }
    asyncio.run(telemetry.tick())
    assert telemetry.latest().state == State.OBSERVED_APPLIED and len(pod.sent) == 1


def test_written_again_after_every_opensync_start_and_only_then(tmp_path):
    pod = Pod()
    telemetry, _ = setup(tmp_path, pod)
    for _ in range(3):
        asyncio.run(telemetry.tick())
    assert len(pod.sent) == 1
    pod.restart("00000000-0000-4000-8000-0000000000a2")  # the database from its template
    asyncio.run(telemetry.tick())
    asyncio.run(telemetry.tick())
    assert len(pod.sent) == 2 and telemetry.latest().state == State.OBSERVED_APPLIED
    assert len(telemetry.store.operations()) == 2


def test_another_managers_broker_is_not_taken_over(tmp_path):
    pod = Pod(mqtt=[("broker", "cloud.example.net"), ("topics", "operator/stats")])
    telemetry, _ = setup(tmp_path, pod)
    asyncio.run(telemetry.tick())
    op = telemetry.latest()
    assert not pod.sent and op.state == State.REJECTED
    assert op.reason == Reason.OWNERSHIP_CONFLICT
    asyncio.run(telemetry.tick())
    assert not pod.sent and "not configured on this start" in telemetry.status()["waiting"]


def test_an_unconfirmed_write_times_out_and_waits_for_the_next_start(tmp_path):
    pod = Pod()
    telemetry, clock = setup(tmp_path, pod)

    async def lost(*args, **kwargs):
        raise ConnectionError("session gone")

    pod.transact = lost
    asyncio.run(telemetry.tick())
    assert telemetry.latest().state == State.INDETERMINATE
    clock.advance(DEADLINE + 1)
    asyncio.run(telemetry.tick())
    assert telemetry.latest().state == State.TIMED_OUT
    asyncio.run(telemetry.tick())
    assert len(telemetry.store.operations()) == 1  # not retried on this start


def test_agent_configuration():
    assert telemetry_intent("pod-1", SERIAL, {"mode": "off"}) is None
    intent = telemetry_intent("pod-1", SERIAL, {"mode": "mqtt", "broker": "10.101.0.1"})
    assert intent.topic == TOPIC and intent.port == 8883 and intent.reporting_interval == 10
    assert intent.publish_interval is None and "publish_interval" not in intent.record()
    assert "agg_stats_interval" not in intent.settings()
    fast = telemetry_intent(
        "pod-1", SERIAL, {"mode": "mqtt", "broker": "10.101.0.40", "publish_interval": 5}
    )
    assert fast.settings()["agg_stats_interval"] == "5" and fast.record()["publish_interval"] == 5
    for bad in (
        {"mode": "mqtt"},
        {"mode": "mqtt", "broker": "10.101.0.1", "topic": "emosa/#"},
        {"mode": "mqtt", "broker": "10.101.0.1", "sampling_interval": 20},
        {"mode": "mqtt", "broker": "10.101.0.1", "publish_interval": 0},
    ):
        with pytest.raises(EmosaError):
            telemetry_intent("pod-1", SERIAL, bad)
