from dataclasses import replace

import pytest
from test_report_coordinator import Rig as SourceRig

from emosa.simulation.wire_reports import AGENT, CONTROLLER, query_frames
from emosa.wire.autoconfiguration import parse_response
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.discovery_session import DiscoveryReportSession
from emosa.wire.inspection import packets

pytestmark = pytest.mark.unit

RESPONSE = (
    Tlv(0x0F, b"\0"),
    Tlv(0x10, b"\0"),
    Tlv(0x80, b"\x01\0"),
    Tlv(0xB3, b"\x01"),
    Tlv(0xDD, b"\xc0"),
    Tlv(0xA9, bytes(3)),
)


class Rig(SourceRig):
    def __init__(self):
        super().__init__()
        self.session = DiscoveryReportSession(
            self.source, self.sent.append, mids=MidSequence(65534), clock=lambda: self.now
        )

    def receive(self, frame, **kwargs):
        return self.session.receive(frame, **({"ingress": "fixture", "generation": 1} | kwargs))

    def response(self, mid=65535, tlvs=RESPONSE, **kwargs):
        return self.receive(fragment_message(AGENT, CONTROLLER, 8, mid, tlvs)[0], **kwargs)

    def discover(self):
        self.session.tick()
        assert self.response() == "controller_correlated"


def test_search_response_then_topology_without_early_or_m1_or_operation():
    rig = Rig()
    assert rig.receive(query_frames(80)[0]) == "message_not_admitted"
    assert not rig.sent
    rig.discover()
    assert rig.receive(query_frames(80)[0]) == "topology_response_sent"
    assert [(Reassembler().feed(f).message_type, Reassembler().feed(f).mid) for f in rig.sent] == [
        (7, 65535),
        (3, 80),
    ]
    assert (
        rig.receive(fragment_message(AGENT, CONTROLLER, 9, 123, ())[0]) == "wsc_admission_blocked"
    )
    status = rig.session.status()
    assert status["operations_created"] == 0 and status["automatic_early_report"].startswith(
        "blocked"
    )
    assert status["selected_response_issues"] == [] and not status["controller_onboarding_proven"]


def test_native_response_missing_security_and_kib_is_rejected():
    from pathlib import Path

    native = list(packets(Path("doc/evidence/peer-baseline/samples/wired/ethernet.pcap")))
    response = Reassembler().feed(native[1][2])
    assert response.message_type == 8
    rig = Rig()
    rig.session.tick()
    assert rig.response(tlvs=response.tlvs) == "discovery_incompatible"
    assert set(rig.session.status()["selected_response_issues"]) == {
        "kib_mib_support_absent",
        "security_capability_absent",
    }
    assert rig.receive(query_frames(80)[0]) == "message_not_admitted"
    assert len(rig.sent) == 1


@pytest.mark.parametrize(
    "kind,value,issue",
    [
        (0xDD, None, "controller_capability_absent"),
        (0xDD, b"\x40", "kib_mib_support_absent"),
        (0xDD, b"\x80", "early_ap_capability_bit_absent_for_non_dpp_search"),
        (0xA9, None, "security_capability_absent"),
        (0xA9, b"", "security_capability_length_invalid"),
        (0xA9, bytes(2), "security_capability_length_invalid"),
        (0xA9, bytes(4), "security_capability_length_invalid"),
        (0xA9, b"\1\0\0", "security_capability_reserved_algorithm"),
        (0xA9, b"\0\1\0", "security_capability_reserved_algorithm"),
        (0xA9, b"\0\0\1", "security_capability_reserved_algorithm"),
    ],
)
def test_missing_malformed_and_reserved_capabilities_never_admit_reports(kind, value, issue):
    rig = Rig()
    rig.session.tick()
    fields = tuple(t for t in RESPONSE if t.kind != kind)
    if value is not None:
        fields += (Tlv(kind, value),)
    assert rig.response(tlvs=fields) == "discovery_incompatible"
    assert issue in rig.session.status()["selected_response_issues"]
    rig.now = 0.3
    rig.session.tick()
    assert len(rig.sent) == 1 and rig.session.reports is None


def test_reserved_future_controller_bytes_do_not_claim_profile_qualification():
    rig = Rig()
    rig.session.tick()
    values = tuple(Tlv(0xDD, b"\xff\xff") if t.kind == 0xDD else t for t in RESPONSE)
    assert rig.response(tlvs=values) == "controller_correlated"
    assert "profile_qualification" in rig.session.advertisement.pending_requirements


def test_lost_responses_retry_three_times_new_mids_with_fixed_budget():
    rig = Rig()
    rig.session.tick()
    for now in (0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5):
        rig.now = now
        rig.publish()
        rig.session.tick()
    assert [Reassembler().feed(f).mid for f in rig.sent] == [65535, 0, 1]
    assert rig.session.state == "discovery_timeout"
    assert rig.response(mid=1) == "message_not_admitted"
    rig.now = 5.5
    rig.publish()
    rig.session.tick()
    assert len(rig.sent) == 3
    rig.session.restart()
    rig.session.tick()
    assert Reassembler().feed(rig.sent[-1]).mid == 2


@pytest.mark.parametrize(
    "mid,change", [(42, {}), (65535, {"ingress": "alien"}), (65535, {"generation": 2})]
)
def test_wrong_mid_and_binding_cannot_correlate_controller(mid, change):
    rig = Rig()
    rig.session.tick()
    assert rig.response(mid, **change) == "input_rejected"
    assert rig.session.reports is None
    assert not rig.session.assembly.contexts


def test_late_response_does_not_trigger_send_or_extend_deadline():
    rig = Rig()
    rig.session.tick()
    for now in (0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5):
        rig.now = now
        rig.publish()
    assert rig.response() == "input_rejected"
    assert len(rig.sent) == 1 and rig.session.reports is None


@pytest.mark.parametrize(
    "change", ["disconnect", "generation", "lease_gap", "capability", "binding"]
)
def test_context_loss_removes_report_access_and_requires_fresh_search(change):
    rig = Rig()
    rig.discover()
    if change == "disconnect":
        rig.source.invalidate()
        rig.publish()
    elif change == "generation":
        rig.publish((2, 0))
    elif change == "lease_gap":
        rig.now = 1
        rig.publish()  # No polling in the lease gap must still lose correlation.
    elif change == "binding":
        rig.source.binding = replace(rig.binding, generation=2)
    else:
        radio = rig.caps.radios[0]
        rig.caps = replace(rig.caps, radios=(replace(radio, ht=False),))
        rig.publish((1, 2))
    assert rig.receive(query_frames(80)[0]) == "message_not_admitted"
    assert rig.session.reports is None and len(rig.sent) == 1
    assert rig.response() == "message_not_admitted"
    rig.now = 1
    if change != "binding":
        rig.publish((2, 1))
        rig.session.tick()
        assert Reassembler().feed(rig.sent[-1]).mid == 0
        assert rig.response() == "input_rejected"  # Old Search MID cannot return.
        assert rig.response(0) == "controller_correlated"


def test_ordinary_revision_preserves_discovery_but_queries_use_current_facts():
    rig = Rig()
    rig.discover()
    rig.publish((1, 2))
    assert rig.receive(query_frames(80)[0]) == "topology_response_sent"
    assert rig.session.counts["search_sent"] == 1


def test_delayed_publication_cannot_bridge_an_expired_lease():
    rig = Rig()
    rig.discover()
    rig.now = 1.1
    # Observation began before expiry, but its publication arrived after the
    # old lease ended. Fresh facts cannot restore the old discovery context.
    rig.source.publish((1, 2), rig.caps, rig.facts, observed_at=0.9)
    assert rig.receive(query_frames(80)[0]) == "message_not_admitted"
    assert rig.session.reports is None and len(rig.sent) == 1


@pytest.mark.parametrize("failure", ["exception", "expired", "invalidated"])
def test_failed_or_late_search_send_is_not_response_eligible(failure):
    rig = Rig()

    def send(frame):
        rig.sent.append(frame)
        if failure == "exception":
            raise OSError("fixture failure")
        if failure == "expired":
            rig.now = 1
        else:
            rig.source.invalidate()

    rig.session.send_frame = send
    rig.session.tick()
    assert rig.session.state == "search_failed"
    assert rig.response() == "message_not_admitted"
    assert rig.session.reports is None


def test_flapping_source_and_explicit_restarts_cannot_burst_searches():
    rig = Rig()
    rig.session.tick()
    for _ in range(100):
        rig.source.invalidate()
        rig.publish()
        rig.session.restart()
        rig.session.tick()
    assert len(rig.sent) == 1 and len(rig.session.events) == 64


def test_unsupported_radio_scope_cannot_advertise_discovery():
    rig = Rig()
    rig.caps = replace(rig.caps, inventory_complete=False)
    rig.publish((1, 2))
    rig.session.tick()
    assert rig.session.state == "source_incompatible" and not rig.sent


def test_closed_session_cannot_be_restarted_by_source_or_packets():
    rig = Rig()
    rig.discover()
    rig.session.close()
    rig.publish((2, 0))
    rig.session.restart()
    rig.session.tick()
    assert rig.response() == "closed_input"
    assert rig.session.status()["state"] == "closed" and len(rig.sent) == 1


def test_response_review_is_diagnostic_and_does_not_claim_security_implementation():
    value = parse_response(
        Reassembler().feed(fragment_message(AGENT, CONTROLLER, 8, 0, RESPONSE)[0])
    )
    assert not value.selected_response_issues
    assert "security_capability_applicability" in value.pending_requirements
