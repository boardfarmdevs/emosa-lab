"""Unassociated station measurements from the pod's probe requests (spec §3.9)."""

import struct
from dataclasses import replace
from pathlib import Path

import pytest

from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients
from emosa.errors import EmosaError
from emosa.opensync.stats import PodStats, ProbeSample
from emosa.wire.channel import OperatingRadio
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.coordinator import ReportSource
from emosa.wire.unassociated import UnassociatedCoordinator, decode_query
from emosa_lab.simulation.wire_reports import BSSID, RUID, fixtures

pytestmark = pytest.mark.unit
RECORDED = Path("tests/fixtures/opensync/pod-6.6.1-hwsim-bs-probe.hex")
TOPIC = "emosa/stats/MVXPOD023F87E628DD"
HEARD = bytes.fromhex("020000001400")  # probed pod-1 in the recording
ASSOCIATED = bytes.fromhex("0200000000c1")
SILENT = bytes.fromhex("0200000000c2")
WALL = 1790387600.0  # 13 s after the recorded report


def recorded_stats():
    """One band-steering report captured from pod-1 (rdk-emosa, 2026-09-26)."""
    stats = PodStats(TOPIC, interval=5, clock=lambda: WALL)
    for line in RECORDED.read_text().splitlines():
        topic, data = line.split()
        assert stats.receive(topic, bytes.fromhex(data))
    return stats


class Rig:
    def __init__(self, *, probes="recorded", radios=None):
        self.now, self.wall = 10.0, WALL
        self.binding, caps, topology = fixtures()
        topology = replace(
            topology,
            clients=AssociatedClients((BssClients(BSSID, (AssociatedClient(ASSOCIATED, 4),)),)),
        )
        self.reports = ReportSource(self.binding, "pod-1", "a" * 64, clock=lambda: self.now)
        self.reports.publish(
            (1, 1),
            caps,
            topology,
            observed_at=self.now,
            lifetime=2,
            operating_radios=(OperatingRadio(RUID, 81, 6, 17),) if radios is None else radios,
        )
        self.sent = []
        self.coordinator = UnassociatedCoordinator(
            self.reports,
            self.sent.append,
            MidSequence(700),
            probes=recorded_stats() if probes == "recorded" else probes,
            clock=lambda: self.now,
            wall=lambda: self.wall,
        )

    def query(self, op_class, *channels, mid=90):
        body = bytearray((op_class, len(channels)))
        for channel, stations in channels:
            body += bytes((channel, len(stations))) + b"".join(stations)
        return Reassembler().feed(
            fragment_message(
                self.binding.local_al,
                self.binding.controller_al,
                0x800F,
                mid,
                (Tlv(0x97, bytes(body)),),
            )[0]
        )

    def messages(self):
        return [Reassembler().feed(frame) for frame in self.sent]


def errors(ack):
    return {t.value[1:]: t.value[0] for t in ack.tlvs if t.kind == 0xA3}


def test_the_recorded_band_steering_report_gives_the_last_probe():
    probe = recorded_stats().probe("02:00:00:00:14:00")
    # six probes in the report; the newest was 5604 ms before the report's time
    assert probe == ProbeSample("2.4G", "home-ap-24", 28, (1790387592823 - 5604) / 1000)
    assert recorded_stats().status()["probed_stations"] == 1


def test_a_heard_station_is_measured_and_the_others_refused_in_the_ack():
    rig = Rig()
    result = rig.coordinator.handle(rig.query(81, (6, (HEARD, ASSOCIATED, SILENT))), rig.now)
    assert result == "unassociated_metrics_response_sent"
    ack, response = rig.messages()
    assert ack.message_type == 0x8000 and ack.mid == 90
    assert errors(ack) == {ASSOCIATED: 0x01, SILENT: 0x02}
    assert response.message_type == 0x8010 and response.mid == 701
    ((kind, value),) = [(t.kind, t.value) for t in response.tlvs if t.kind != 0]
    assert kind == 0x98 and value[:2] == bytes((81, 1))
    mac, channel, age, rcpi = value[2:8], value[8], *struct.unpack("!IB", value[9:14])
    # SNR 28 over the -96 dBm floor: -68 dBm, RCPI 84; heard 12.781 s before now
    assert (mac, channel, rcpi) == (HEARD, 6, 84) and age == 12781
    assert len(value) == 14


def test_another_channel_or_class_is_not_heard():
    rig = Rig()
    assert rig.coordinator.handle(rig.query(115, (36, (HEARD,))), rig.now) == (
        "unassociated_query_refused"
    )
    rig.coordinator.handle(rig.query(81, (1, (HEARD,)), (6, (SILENT,)), mid=91), rig.now)
    (first,), (second,) = rig.messages()[:1], rig.messages()[1:]
    assert errors(first) == {HEARD: 0x02}
    assert errors(second) == {HEARD: 0x02, SILENT: 0x02}  # the Ack is the whole answer


def test_an_old_probe_is_not_a_measurement():
    rig = Rig()
    rig.wall = WALL + 120
    rig.coordinator.handle(rig.query(81, (6, (HEARD,))), rig.now)
    (ack,) = rig.messages()
    assert errors(ack) == {HEARD: 0x02}


def test_without_telemetry_every_station_is_refused():
    rig = Rig(probes=None)
    rig.coordinator.handle(rig.query(81, (6, (HEARD, ASSOCIATED))), rig.now)
    (ack,) = rig.messages()
    assert errors(ack) == {HEARD: 0x02, ASSOCIATED: 0x01}
    assert rig.coordinator.status()["measurement_source"] is None


def test_without_a_known_operating_channel_nothing_is_heard():
    rig = Rig(radios=())
    rig.coordinator.handle(rig.query(81, (6, (HEARD,))), rig.now)
    (ack,) = rig.messages()
    assert errors(ack) == {HEARD: 0x02}


def test_a_duplicate_station_is_answered_once():
    op_class, channels = decode_query(
        (Tlv(0x97, bytes((81, 2, 6, 1)) + HEARD + bytes((1, 1)) + HEARD),)
    )
    assert (op_class, channels) == (81, ((6, (HEARD,)), (1, ())))


@pytest.mark.parametrize(
    "body",
    [
        b"",
        bytes((81, 1, 6, 2)) + HEARD,  # truncated station list
        bytes((81, 1, 6, 1)) + HEARD + b"\0",  # trailing octet
        bytes((81, 1, 6, 1)) + b"\x01" + HEARD[1:],  # group address
        bytes((81, 1, 6, 0)),  # no station
    ],
)
def test_a_malformed_query_is_rejected(body):
    with pytest.raises(EmosaError):
        decode_query((Tlv(0x97, body),))


def test_a_late_query_is_not_acknowledged():
    rig = Rig()
    with pytest.raises(EmosaError):
        rig.coordinator.handle(rig.query(81, (6, (HEARD,))), rig.now - 1)
    assert not rig.sent
