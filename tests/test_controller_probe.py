from dataclasses import replace
from pathlib import Path

import pytest

from emosa.wire.cmdu import Reassembler, Tlv, fragment_message
from emosa_lab.simulation.wire_reports import AGENT, CONTROLLER, fixtures
from emosa_lab.wire.controller_probe import ControllerProbe
from emosa_lab.wire.inspection import packets

pytestmark = pytest.mark.unit


class Rig:
    def __init__(self):
        self.now, self.sent = 0.0, []
        self.binding = fixtures()[0]
        self.probe = ControllerProbe(self.binding, self.sent.append, clock=lambda: self.now)

    def receive(self, frame, **kwargs):
        return self.probe.receive(frame, **({"ingress": "fixture", "generation": 1} | kwargs))

    def response(self, *, mid=65535, source=CONTROLLER):
        native = list(packets(Path("doc/evidence/peer-baseline/samples/wired/ethernet.pcap")))
        tlvs = Reassembler().feed(native[1][2]).tlvs
        return fragment_message(AGENT, source, 8, mid, tlvs)[0]


def test_native_profile_one_response_correlates_but_capability_gaps_block_admission():
    rig = Rig()
    rig.probe.tick()
    sent = Reassembler().feed(rig.sent[0])
    assert sent.message_type == 7 and sent.mid == 65535
    assert next(t.value for t in sent.tlvs if t.kind == 0xB3) == b"\1"
    assert rig.receive(rig.response()) == "response_correlated"
    status = rig.probe.status()
    assert status["response_profile"] == 1 and status["profile_correlated"]
    assert set(status["selected_response_issues"]) == {
        "kib_mib_support_absent",
        "security_capability_absent",
    }
    assert not status["wsc_started"] and status["operations_created"] == 0
    assert not status["profile_qualified"] and not status["controller_onboarding_proven"]
    rig.probe.tick()
    assert len(rig.sent) == 1
    assert rig.receive(rig.response()) == "inactive_input"


def test_unsolicited_wrong_mid_source_generation_and_wsc_do_not_advance_probe():
    rig = Rig()
    assert rig.receive(rig.response()) == "input_rejected"
    rig.probe.tick()
    assert rig.receive(rig.response(mid=17)) == "input_rejected"
    assert rig.receive(rig.response(source=bytes.fromhex("02000000ff01"))) == "input_rejected"
    assert rig.receive(rig.response(), generation=2) == "input_rejected"
    assert rig.receive(rig.response(), ingress="another") == "input_rejected"
    assert rig.receive(fragment_message(AGENT, CONTROLLER, 9, 17, ())[0]) == "unsupported_message"
    assert rig.probe.state == "searching" and rig.probe.status()["response_mid"] is None


def test_lost_response_is_three_searches_with_mid_rollover_then_timeout():
    rig = Rig()
    for moment in (0, 0.5, 1, 2, 3, 4, 5):
        rig.now = moment
        rig.probe.tick()
    assert [Reassembler().feed(f).mid for f in rig.sent] == [65535, 0, 1]
    assert rig.probe.state == "timeout"
    assert rig.receive(rig.response(mid=1)) == "inactive_input"
    assert rig.probe.status()["selected_response_issues"] is None


def test_expired_response_without_tick_cannot_be_observed():
    rig = Rig()
    rig.probe.tick()
    rig.now = 5
    assert rig.receive(rig.response()) == "input_rejected"
    assert not rig.probe.status()["profile_correlated"]


@pytest.mark.parametrize("late", (False, True))
def test_failed_or_late_send_withdraws_response_authority(late):
    rig = Rig()

    def send(frame):
        if late:
            rig.now = 6
        else:
            raise OSError("fixture send failure")

    rig.probe.send_frame = send
    rig.probe.tick()
    assert rig.probe.state == "send_failed"
    assert rig.receive(rig.response()) == "inactive_input"


def test_out_of_order_fragmented_response_preserves_selected_capabilities():
    rig = Rig()
    rig.probe.tick()
    response = Reassembler().feed(rig.response())
    tlvs = response.tlvs + (Tlv(0xF0, bytes(900)), Tlv(0xF1, bytes(900)))
    frames = fragment_message(AGENT, CONTROLLER, 8, 65535, tlvs)
    assert len(frames) == 2
    assert rig.receive(frames[1]) == "incomplete"
    assert not rig.probe.status()["profile_correlated"]
    assert rig.receive(frames[0]) == "response_correlated"


def test_input_rate_is_bounded_and_interface_source_can_differ_from_controller_al():
    rig = Rig()
    interface_mac = bytes.fromhex("02000000ff01")
    rig.binding = replace(rig.binding, source_macs=(interface_mac,))
    rig.probe = ControllerProbe(rig.binding, rig.sent.append, clock=lambda: rig.now)
    rig.probe.tick()
    for _ in range(32):
        assert rig.receive(b"malformed") == "input_rejected"
    assert rig.receive(rig.response(source=interface_mac)) == "rate_limited"
    rig.now = 1
    assert rig.receive(rig.response(source=interface_mac)) == "response_correlated"


def test_close_stops_all_transmissions():
    rig = Rig()
    rig.probe.close()
    rig.probe.tick()
    assert rig.probe.state == "closed" and not rig.sent
