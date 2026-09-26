"""AP metrics from the pod's statistics (spec §3.8)."""

import struct
from dataclasses import replace

import pytest

from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients
from emosa.errors import EmosaError, Reason
from emosa.opensync.stats import PodStats, StationStats, SurveyStats
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.coordinator import ReportSource
from emosa.wire.pod_metrics import PodMetricReporter, rcpi, utilization
from emosa_lab.simulation.wire_reports import BSSID, RUID, fixtures

pytestmark = pytest.mark.unit
STA = bytes.fromhex("0200000000c1")
ESP = bytes.fromhex("3fff00")
WALL = 1_790_000_000.0


def station(mac, *, at, snr=45, counters=True):
    known = 1 if counters else None
    return StationStats(
        mac.hex(":"), "private_ssid", "2.4G", 6, at, 3, at - 15, 130.0, 54.0, snr,
        3499, 3416, 23, 30, known, None, known, known,
    )  # fmt: skip


class Rig:
    def __init__(self):
        self.now, self.wall = 10.0, WALL
        self.binding, self.caps, topology = fixtures()
        self.topology = replace(
            topology,
            clients=AssociatedClients((BssClients(BSSID, (AssociatedClient(STA, 4),)),)),
        )
        self.reports = ReportSource(self.binding, "pod-1", "a" * 64, clock=lambda: self.now)
        self.reports.publish((1, 1), self.caps, self.topology, observed_at=self.now, lifetime=2)
        self.stats = PodStats("emosa/stats/pod", interval=5, clock=lambda: self.wall)
        self.stats.surveys[6] = SurveyStats("2.4G", 6, 40, 5000, self.wall - 3)
        self.stats.stations[STA.hex(":")] = station(STA, at=self.wall - 2)
        self.sent = []
        self.policy = {"metrics": {"interval_seconds": 5, "radios": [
            {"ruid": RUID.hex(), "include_traffic": True, "include_link": True},
        ]}}  # fmt: skip
        self.reporter = PodMetricReporter(
            self.reports,
            self.stats,
            self.sent.append,
            MidSequence(500),
            admitted=lambda: True,
            policy=lambda: self.policy,
            esp_be=ESP,
            freshness=75,
            clock=lambda: self.now,
            wall=lambda: self.wall,
        )

    def report(self):
        return Reassembler().feed(self.sent[-1])


def tlvs(message, kind):
    return [t.value for t in message.tlvs if t.kind == kind]


def test_a_periodic_report_carries_measured_utilization_and_the_stations_signal():
    rig = Rig()
    rig.reporter.periodic(rig.policy, rig.now)
    message = rig.report()
    assert message.message_type == 0x800C and message.mid == 501
    (ap,) = tlvs(message, 0x94)
    # BSSID, utilization 40 % of 255, one station, ESP flags (BE only), declared ESP
    assert ap == BSSID + bytes((utilization(40),)) + struct.pack("!H", 1) + b"\x80" + ESP
    assert utilization(40) == 102
    (link,) = tlvs(message, 0x96)
    age, down, up, value = struct.unpack("!3IB", link[13:])
    assert link[:13] == STA + b"\1" + BSSID
    # SNR 45 over OpenSync's -96 dBm floor: -51 dBm, RCPI 118
    assert (age, down, up, value) == (2000, 130, 54, 118) and rcpi(45) == 118
    assert len(tlvs(message, 0xA2)) == 1
    assert rig.reporter.counts == {"periodic_ap_metric_report_transmitted": 1}


def test_without_a_fresh_survey_there_is_no_report():
    rig = Rig()
    rig.stats.surveys[6] = replace(rig.stats.surveys[6], measured_at=rig.wall - 80)
    with pytest.raises(EmosaError) as error:
        rig.reporter.periodic(rig.policy, rig.now)
    assert error.value.code == Reason.NOT_READY and not rig.sent


def test_a_station_without_a_fresh_report_is_left_out_but_counted():
    rig = Rig()
    rig.stats.stations[STA.hex(":")] = station(STA, at=rig.wall - 90)
    rig.reporter.periodic(rig.policy, rig.now)
    message = rig.report()
    assert struct.unpack("!H", tlvs(message, 0x94)[0][7:9]) == (1,)
    assert not tlvs(message, 0x96) and not tlvs(message, 0xA2)


def test_unmeasured_counters_leave_out_traffic_and_policy_selects_companions():
    rig = Rig()
    rig.stats.stations[STA.hex(":")] = station(STA, at=rig.wall - 2, counters=False)
    rig.reporter.periodic(rig.policy, rig.now)
    assert not tlvs(rig.report(), 0xA2) and tlvs(rig.report(), 0x96)
    rig.policy["metrics"]["radios"][0]["include_link"] = False
    rig.reporter.periodic(rig.policy, rig.now)
    assert not tlvs(rig.report(), 0x96)


def test_a_query_is_answered_for_the_queried_bss():
    rig = Rig()
    query = Reassembler().feed(
        fragment_message(
            rig.binding.local_al, rig.binding.controller_al, 0x800B, 71,
            (Tlv(0x93, b"\1" + BSSID),),
        )[0]
    )  # fmt: skip
    assert rig.reporter.handle(query, rig.now, ingress="fixture", generation=1) == (
        "ap_metric_query_answered"
    )
    message = rig.report()
    assert message.mid == 71 and len(tlvs(message, 0x94)) == 1
    rig.stats.surveys.clear()
    assert rig.reporter.handle(query, rig.now, ingress="fixture", generation=1) == (
        "ap_measurements_unavailable"
    )


def test_the_declared_esp_is_reported_as_configured():
    rig = Rig()
    assert rig.reporter.status()["esp_source"] == "profile"
    with pytest.raises(EmosaError):
        PodMetricReporter(
            rig.reports, rig.stats, rig.sent.append, MidSequence(1), admitted=lambda: True,
            policy=dict, esp_be=b"\1\2", freshness=75,
        )  # fmt: skip


def test_the_pod_survey_is_decoded_from_its_report():
    from emosa.opensync.stats import Report

    report = Report(nodeID="")
    survey = report.survey.add(band=0, survey_type=0, timestamp_ms=int(WALL * 1000))
    # dppline.c: a sample's offset_ms is the report's time minus the sample's
    survey.survey_list.add(channel=6, duration_ms=5000, busy=41, offset_ms=2000)
    stats = PodStats("emosa/stats/pod", interval=5, clock=lambda: WALL)
    assert stats.receive("emosa/stats/pod", report.SerializeToString())
    assert stats.surveys[6].busy_percent == 41 and stats.surveys[6].measured_at == WALL - 2
    assert stats.status()["surveys"]["6"]["busy_percent"] == 41
