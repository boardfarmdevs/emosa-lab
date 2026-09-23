from dataclasses import replace

import pytest

from emosa.errors import EmosaError
from emosa.simulation.wire_reports import AGENT, CONTROLLER, fixtures, query_frames
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.coordinator import ReportCoordinator, ReportSource

pytestmark = pytest.mark.unit


class Rig:
    def __init__(self):
        self.now = 0.0
        self.binding, self.caps, self.facts = fixtures()
        self.source = ReportSource(self.binding, "pod-1", "a" * 64, clock=lambda: self.now)
        self.sent = []
        self.coordinator = ReportCoordinator(
            self.source, self.sent.append, mids=MidSequence(65534), clock=lambda: self.now
        )
        self.publish()

    def publish(self, revision=(1, 1), **kwargs):
        return self.source.publish(revision, self.caps, self.facts, observed_at=self.now, **kwargs)

    def receive(self, frame, **kwargs):
        return self.coordinator.receive(frame, **({"ingress": "fixture", "generation": 1} | kwargs))

    def ack(self, mid, tlvs=()):
        return self.receive(fragment_message(AGENT, CONTROLLER, 0x8000, mid, tlvs)[0])


def test_early_ack_is_bound_to_live_sent_mid_and_does_not_enable_wsc():
    rig = Rig()
    rig.coordinator.notify_early()
    assert rig.ack(42) == "unmatched_ack"
    assert rig.ack(65535) == "early_acknowledged"
    assert rig.ack(65535) == "unmatched_ack"
    rig.now = 0.3
    rig.coordinator.tick()
    assert len(rig.sent) == 1
    status = rig.coordinator.status()
    assert status["operations_created"] == 0
    assert status["wsc_admission"].startswith("blocked")
    assert not status["controller_onboarding_proven"]


def test_ack_arrival_does_not_trigger_an_unnecessary_retry():
    rig = Rig()
    rig.coordinator.notify_early()
    rig.now = 0.3
    assert rig.ack(65535) == "early_acknowledged"
    assert len(rig.sent) == 1


def test_lost_ack_retries_with_new_mids_and_fixed_deadline():
    rig = Rig()
    rig.publish(lifetime=2)
    rig.coordinator.notify_early()
    for now in (0.25, 0.5, 0.75):
        rig.now = now
        rig.publish()  # Same revision renews source, not the pending transaction.
        rig.coordinator.tick()
    assert [Reassembler().feed(f).mid for f in rig.sent] == [65535, 0, 1]
    rig.now = 1.0
    rig.coordinator.tick()
    assert rig.coordinator.pending is None
    assert rig.coordinator.counts["early_ack_timeout"] == 1
    assert rig.ack(1) == "unmatched_ack"


def test_retry_can_be_acknowledged_by_any_still_live_attempt():
    rig = Rig()
    rig.coordinator.notify_early()
    rig.now = 0.25
    rig.coordinator.tick()
    assert rig.ack(65535) == "early_acknowledged"
    assert len(rig.sent) == 2


@pytest.mark.parametrize("kind", [0xA3, 0xBC, 0xAB, 0xAC])
def test_error_and_unprocessed_security_ack_cannot_clear_notification(kind):
    rig = Rig()
    rig.coordinator.notify_early()
    assert rig.ack(65535, (Tlv(kind, b""),)) != "early_acknowledged"
    assert rig.coordinator.pending is not None


def test_unrelated_unknown_ack_tlv_is_ignored_under_base_reception_rule():
    rig = Rig()
    rig.coordinator.notify_early()
    assert rig.ack(65535, (Tlv(0xFE, b"public"),)) == "early_acknowledged"


@pytest.mark.parametrize("change", ["revision", "invalidate", "expiry", "binding"])
def test_source_changes_cancel_pending_reports_and_reject_old_ack(change):
    rig = Rig()
    rig.coordinator.notify_early()
    if change == "revision":
        rig.publish((1, 2))
    elif change == "invalidate":
        rig.source.invalidate()
        rig.publish()  # Even the same DB revision belongs to a new source epoch.
    elif change == "binding":
        rig.source.binding = replace(rig.binding, generation=2)
    else:
        rig.now = 1
    assert rig.ack(65535) == "unmatched_ack"
    assert rig.coordinator.counts["early_source_invalidated"] == 1
    assert len(rig.sent) == 1


def test_topology_queries_echo_mid_once_and_new_mid_retries_are_independent():
    rig = Rig()
    for expected in ("topology_response_sent", "duplicate_query"):
        assert rig.receive(query_frames(80)[0]) == expected
    assert rig.receive(query_frames(81)[0]) == "topology_response_sent"
    assert [Reassembler().feed(f).mid for f in rig.sent] == [80, 81]


@pytest.mark.parametrize("change", [{"ingress": "alien"}, {"generation": 2}])
def test_binding_mismatch_cannot_allocate_reassembly_or_consume_query(change):
    rig = Rig()
    assert rig.receive(query_frames(1)[0], **change) == "input_or_source_rejected"
    assert not rig.coordinator.assembly.contexts
    assert not rig.coordinator._queries
    assert rig.receive(query_frames(1)[0]) == "topology_response_sent"


def test_alien_source_is_rejected_before_reassembly():
    rig = Rig()
    frame = fragment_message(AGENT, bytes.fromhex("020000009999"), 2, 1, [Tlv(0xB3, b"\1")])[0]
    assert rig.receive(frame) == "input_or_source_rejected"
    assert not rig.coordinator.assembly.contexts and not rig.sent


def test_source_unavailable_never_reports_cached_topology():
    rig = Rig()
    rig.source.invalidate()
    assert rig.receive(query_frames(80)[0]) == "input_or_source_rejected"
    assert not rig.sent
    rig.publish()
    assert rig.receive(query_frames(80)[0]) == "duplicate_query"
    assert rig.receive(query_frames(81)[0]) == "topology_response_sent"


def test_query_rate_budget_is_bounded_and_refills_without_replaying_old_query():
    rig = Rig()
    for mid in range(8):
        assert rig.receive(query_frames(mid)[0]) == "topology_response_sent"
    assert rig.receive(query_frames(8)[0]) == "query_budget_exhausted"
    rig.now = 0.25
    assert rig.receive(query_frames(8)[0]) == "topology_response_sent"
    assert len(rig.sent) == 9


def test_no_transmit_after_close_and_pending_exchange_is_dropped():
    rig = Rig()
    rig.coordinator.notify_early()
    rig.coordinator.close()
    assert rig.receive(query_frames(5)[0]) == "closed_input"
    rig.coordinator.tick()
    with pytest.raises(EmosaError):
        rig.coordinator.notify_early()
    assert len(rig.sent) == 1 and not rig.coordinator.pending


def test_wsc_and_ap_capability_query_are_visible_unsupported_inputs_not_operation_triggers():
    rig = Rig()
    for kind in (9, 0x8001):
        frame = fragment_message(AGENT, CONTROLLER, kind, 1, ())[0]
        assert rig.receive(frame) == "unsupported_message"
    assert not rig.sent and rig.coordinator.status()["operations_created"] == 0


def test_sending_failure_does_not_leave_an_ack_eligible_notification():
    rig = Rig()

    def fail(frame):
        rig.sent.append(frame)
        raise OSError("fixture transport stopped")

    rig.coordinator.send_frame = fail
    with pytest.raises(OSError):
        rig.coordinator.notify_early()
    assert rig.ack(65535) == "unmatched_ack"
    assert rig.coordinator.counts["early_send_failed"] == 1


def test_slow_source_callback_does_not_lose_original_query_receive_time():
    rig = Rig()
    original = rig.source.current

    def slow():
        rig.now += 1
        return original()

    rig.source.current = slow
    assert rig.receive(query_frames(1)[0]) == "input_or_source_rejected"
    assert not rig.sent


def test_publishing_same_revision_with_changed_facts_invalidates_existing_lease():
    rig = Rig()
    rig.caps = replace(rig.caps, inventory_complete=False)
    with pytest.raises(EmosaError):
        rig.publish()
    assert rig.source.current() is None


@pytest.mark.parametrize("revision", [(0, 10), (1, 0), (1,), (True, 1), [1, 2], (-1, 2)])
def test_old_or_malformed_source_revision_withdraws_facts(revision):
    rig = Rig()
    with pytest.raises(EmosaError):
        rig.publish(revision)
    assert rig.source.current() is None


@pytest.mark.parametrize("observed_at,lifetime", [(1, 1), (-1, 1), (0, 0), (0, 3), (0, True)])
def test_publication_cannot_refresh_an_expired_or_invalid_observation(observed_at, lifetime):
    rig = Rig()
    with pytest.raises(EmosaError):
        rig.source.publish((1, 2), rig.caps, rig.facts, observed_at=observed_at, lifetime=lifetime)
    assert rig.source.current() is None


def test_radio_capability_and_topology_set_must_agree_before_publication():
    rig = Rig()
    rig.caps = replace(rig.caps, radios=())
    with pytest.raises(EmosaError):
        rig.publish((1, 2))
    assert rig.source.current() is None


def test_report_source_is_per_instance_and_event_retention_is_bounded():
    first, second = Rig(), Rig()
    assert first.source.current().stamp.token != second.source.current().stamp.token
    for mid in range(200):
        first.ack(mid)
    assert len(first.coordinator.events) == 64
    assert first.coordinator.counts["unmatched_ack"] == 200


def test_source_changes_while_sending_do_not_leak_a_second_fragment():
    from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients
    from emosa.simulation.wire_reports import BSSID

    rig = Rig()
    clients = tuple(
        AssociatedClient(bytes.fromhex("020003") + i.to_bytes(3, "big"), i) for i in range(180)
    )
    rig.facts = replace(rig.facts, clients=AssociatedClients((BssClients(BSSID, clients),)))
    rig.publish((1, 2))

    def changed(frame):
        rig.sent.append(frame)
        rig.source.invalidate()

    rig.coordinator.send_frame = changed
    assert rig.receive(query_frames(1)[0]) == "input_or_source_rejected"
    assert len(rig.sent) == 1 and rig.coordinator.counts["topology_send_failed"] == 1
    assert "topology_response_sent" not in rig.coordinator.counts


def test_incomplete_query_expires_without_dispatch_and_wrong_generation_cannot_finish_it():
    rig = Rig()
    frames = fragment_message(
        AGENT, CONTROLLER, 2, 10, [Tlv(0xFE, b"x" * 1400), Tlv(0xB3, b"\1"), Tlv(0xFE, b"y" * 100)]
    )
    assert len(frames) == 2
    assert rig.receive(frames[0]) == "incomplete"
    assert rig.receive(frames[1], generation=2) == "input_or_source_rejected"
    rig.now = 5
    rig.coordinator.tick()
    assert not rig.coordinator.assembly.contexts and not rig.sent


def test_new_observation_does_not_extend_already_prepared_report_lease():
    from emosa.wire.reports import topology_response

    rig = Rig()
    original = rig.source.current()
    prepared = topology_response(
        Reassembler().feed(query_frames(1)[0]),
        rig.binding,
        original.topology,
        original.stamp,
        ingress="fixture",
        generation=1,
        received_at=0,
        clock=lambda: rig.now,
    )
    rig.now = 0.75
    rig.publish()
    rig.now = 1
    with pytest.raises(EmosaError):
        prepared.send(rig.sent.append, rig.coordinator._stamp, clock=lambda: rig.now)
    assert not rig.sent


@pytest.mark.parametrize("field,value", [("inputs_digest", "b" * 64), ("pod_id", "other-pod")])
def test_source_identity_or_inputs_change_requires_new_source_context(field, value):
    rig = Rig()
    rig.coordinator.notify_early()
    setattr(rig.source, field, value)
    assert rig.source.current() is None
    assert rig.ack(65535) == "unmatched_ack"
    with pytest.raises(EmosaError):
        rig.publish((1, 2))


def test_telemetry_expiry_withdraws_clients_and_operating_data_without_losing_control_context():
    from emosa.wire.channel import OperatingRadio

    rig = Rig()
    radio = rig.caps.radios[0].basic.ruid
    live = rig.publish(
        (1, 2),
        lifetime=2,
        telemetry_valid_until=0.5,
        operating_radios=(OperatingRadio(radio, 81, 6, 20),),
    )
    assert live.stamp.valid_until == 0.5 and live.topology.inventory_complete
    rig.now = 0.5
    withdrawn = rig.source.current()
    assert withdrawn.context_token == live.context_token
    assert withdrawn.capabilities == live.capabilities
    assert not withdrawn.topology.inventory_complete and not withdrawn.operating_radios
    assert all(not b.clients for b in withdrawn.topology.clients.bsses)
    assert withdrawn.stamp.token != live.stamp.token
    with pytest.raises(EmosaError):
        live.stamp.check(withdrawn.stamp, rig.now)
    # Refreshing an independently current OVSDB snapshot does not resurrect
    # telemetry and does not require rediscovery. Its own lapse still does.
    renewed = rig.publish((1, 3), lifetime=2, telemetry_valid_until=0.5)
    assert renewed.context_token == live.context_token and not renewed.topology.inventory_complete
    rig.now = 2.5
    assert rig.source.current() is None
    recovered = rig.publish((1, 4), lifetime=2, telemetry_valid_until=3)
    assert recovered.context_token != live.context_token


def test_lapsed_telemetry_cannot_send_a_prepared_topology_or_fabricate_an_empty_inventory():
    from emosa.wire.reports import topology_response

    rig = Rig()
    snapshot = rig.publish((1, 2), lifetime=2, telemetry_valid_until=0.2)
    prepared = topology_response(
        Reassembler().feed(query_frames(1)[0]),
        rig.binding,
        snapshot.topology,
        snapshot.stamp,
        ingress="fixture",
        generation=1,
        received_at=0,
        clock=lambda: rig.now,
    )
    rig.now = 0.3
    with pytest.raises(EmosaError):
        prepared.send(rig.sent.append, rig.coordinator._stamp, clock=lambda: rig.now)
    rig.receive(query_frames(2)[0])
    assert not rig.sent and not rig.source.current().topology.inventory_complete


@pytest.mark.parametrize("expiry", [True, "1", float("nan"), float("inf")])
def test_invalid_telemetry_deadline_cannot_preserve_source_authority(expiry):
    rig = Rig()
    with pytest.raises(EmosaError):
        rig.publish((1, 2), telemetry_valid_until=expiry)
    assert rig.source.current() is None


def test_topology_deadline_withdraws_topology_without_expiring_radio_or_control_context():
    from emosa.wire.channel import OperatingRadio
    from emosa.wire.reports import topology_response

    rig = Rig()
    radio = rig.caps.radios[0].basic.ruid
    live = rig.publish(
        (1, 2),
        lifetime=2,
        topology_valid_until=0.2,
        telemetry_valid_until=1,
        operating_radios=(OperatingRadio(radio, 81, 6, 20),),
    )
    prepared = topology_response(
        Reassembler().feed(query_frames(1)[0]),
        rig.binding,
        live.topology,
        live.stamp,
        ingress="fixture",
        generation=1,
        received_at=0,
        clock=lambda: rig.now,
    )
    rig.now = 0.3
    withdrawn = rig.source.current()
    assert withdrawn.context_token == live.context_token
    assert not withdrawn.topology.inventory_complete
    assert withdrawn.operating_radios == live.operating_radios
    with pytest.raises(EmosaError):
        prepared.send(rig.sent.append, rig.coordinator._stamp, clock=lambda: rig.now)
    assert not rig.sent
    renewed = rig.publish((1, 3), lifetime=2, topology_valid_until=0.2, telemetry_valid_until=1)
    assert renewed.context_token == live.context_token and not renewed.topology.inventory_complete


@pytest.mark.parametrize("expiry", [True, "1", float("nan"), float("inf")])
def test_invalid_topology_deadline_revokes_authority(expiry):
    rig = Rig()
    with pytest.raises(EmosaError):
        rig.publish((1, 2), topology_valid_until=expiry)
    assert rig.source.current() is None
