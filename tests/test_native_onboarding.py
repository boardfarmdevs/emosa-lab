import asyncio
from dataclasses import replace

import pytest
from test_autoconfiguration import BINDING, RESPONSE, assemble, m2
from test_autoconfiguration import fixed_entropy as fixed_entropy
from test_provisioning_session import frames
from test_wsc_operation_bridge import rig as rig

from emosa.easymesh_payloads import DeviceInventory, InventoryRadio, encode_value
from emosa.simulation.wire_reports import fixtures
from emosa.wire.cmdu import MidSequence, Tlv, fragment_message
from emosa.wire.coordinator import ReportSource
from emosa.wire.onboarding import OnboardingRecovery, OnboardingSession, non_dpp_admission

pytestmark = pytest.mark.unit


def lifecycle(rig):
    bridge, engine, backend, clock = rig
    _, caps, topology = fixtures()
    ruid = bridge.target.ruid
    caps = replace(
        caps,
        profile2=bridge.exchange.profile2,
        radios=(
            replace(
                caps.radios[0],
                basic=bridge.exchange.basic,
                ht=False,
                advanced=bridge.exchange.advanced,
            ),
        ),
    )
    topology = replace(
        topology,
        device=replace(topology.device, al_mac=BINDING.local_al),
        operational=replace(
            topology.operational, radios=(replace(topology.operational.radios[0], ruid=ruid),)
        ),
    )
    source = ReportSource(BINDING, "pod-1", "b" * 64, clock=clock.monotonic)
    source.publish((1, 1), caps, topology, observed_at=clock.monotonic())
    bridge.exchange.capabilities = (
        Tlv(0x85, encode_value(bridge.exchange.basic)),
        *bridge.exchange.capabilities[1:],
    )
    inventory = DeviceInventory(b"owned", b"0.1.0", b"model", (InventoryRadio(ruid, b"model"),))
    sent = []
    session = OnboardingSession(
        source,
        sent.append,
        lambda *_: bridge,
        inventory,
        mids=MidSequence(500),
        clock=clock.monotonic,
    )
    return session, source, sent


async def receive(session, frame):
    return await session.receive(frame, ingress=BINDING.ingress, generation=BINDING.generation)


def response(mid=501, flags=b"\xc0", extra=()):
    return fragment_message(
        BINDING.local_al, BINDING.controller_al, 8, mid, (*RESPONSE, Tlv(0xDD, flags), *extra)
    )[0]


def test_client_telemetry_gap_does_not_restart_provisioned_control_session_or_invent_leaves(rig):
    async def scenario():
        _bridge, engine, backend, clock = rig
        session, source, _sent = lifecycle(rig)
        original = source.current()
        source.publish(
            (1, 2),
            original.capabilities,
            original.topology,
            observed_at=clock.monotonic(),
            lifetime=2,
            telemetry_valid_until=clock.monotonic() + 0.5,
        )
        await session.tick()
        assert await receive(session, response()) == "early_then_m1_sent"
        await receive(session, frames()[0])
        assert len(engine.store.operations()) == 1 and backend.writes == 1
        context = source.current().context_token
        before = dict(session.counts)
        clock.advance(0.6)
        await session.tick()
        assert session.state == "provisioning" and source.current().context_token == context
        assert not source.current().topology.inventory_complete
        assert session.counts.get("client_leave_notification", 0) == before.get(
            "client_leave_notification", 0
        )
        assert session.counts["search_sent"] == 1 and backend.writes == 1
        # This distinction must never hide an actual connection/source loss.
        source.invalidate()
        await session.tick()
        assert session.state == "source_lost"
        session.close()

    asyncio.run(scenario())


def test_neighbor_metrics_handoff_requires_authenticated_live_context_without_config_effect(rig):
    from emosa.wire.link_metrics import LinkBinding, LinkMetrics, RxLink, TxLink

    async def scenario():
        _bridge, engine, backend, clock = rig
        session, source, sent = lifecycle(rig)
        await session.tick()
        await receive(session, response())
        query = fragment_message(
            BINDING.local_al, BINDING.controller_al, 5, 701, (Tlv(8, b"\0\2"),)
        )[0]
        # Discovery admission alone does not activate this measurement handoff.
        assert await receive(session, query) == "unsupported_message_0005"
        await receive(session, frames()[0])
        count = len(sent)
        assert await receive(session, query) == "neighbor_measurement_unavailable"
        assert len(sent) == count
        clock.advance(0.1)
        snapshot = source.current()
        row = snapshot.topology.neighbors1905[0]
        neighbor = row.neighbors[0]
        peer_interface = bytes.fromhex("020000005002")
        session.link_metric_source.publish(
            context_token=snapshot.context_token,
            counter_epoch="owned-test-epoch",
            interval_started=clock.monotonic() - 0.1,
            observed_at=clock.monotonic(),
            inventory=(
                LinkBinding(
                    neighbor.al_mac,
                    row.local_interface,
                    peer_interface,
                    1,
                    neighbor.bridges_present,
                ),
            ),
            metrics=(
                LinkMetrics(
                    BINDING.local_al,
                    neighbor.al_mac,
                    (
                        TxLink(
                            row.local_interface,
                            peer_interface,
                            1,
                            neighbor.bridges_present,
                            2,
                            100,
                            900,
                            90,
                            65535,
                        ),
                    ),
                ),
                LinkMetrics(
                    BINDING.local_al,
                    neighbor.al_mac,
                    (RxLink(row.local_interface, peer_interface, 1, 1, 99, 255),),
                ),
            ),
            inventory_complete=True,
        )
        assert await receive(session, query) == "neighbor_link_metric_response_sent"
        packet = assemble((sent[-1],))
        assert (packet.message_type, packet.mid) == (6, 701)
        assert [t.kind for t in packet.tlvs] == [9, 10]
        assert len(engine.store.operations()) == 1 and backend.writes == 1
        source.invalidate()
        before = len(sent)
        assert await receive(session, query) == "source_unavailable"
        assert len(sent) == before and session.link_metric_source.current() is None
        session.close()

    asyncio.run(scenario())


def test_ap_metric_handoff_requires_authentication_and_live_membership(rig):
    from test_ap_metrics import bundle

    async def scenario():
        bridge, engine, backend, clock = rig
        session, source, sent = lifecycle(rig)
        await session.tick()
        await receive(session, response())
        current = source.current()
        measured = bundle(clock.monotonic(), stations=())
        measured = replace(measured, radio=replace(measured.radio, ruid=bridge.target.ruid))
        session.ap_metric_source.publish(
            context=current.context_token,
            counter_epoch="measured-epoch-1",
            observed_at=clock.monotonic(),
            bundle=measured,
            inventory_complete=True,
        )
        query = fragment_message(
            BINDING.local_al,
            BINDING.controller_al,
            0x800B,
            711,
            (Tlv(0x93, b"\1" + measured.ap.bssid), Tlv(0x82, measured.radio.ruid)),
        )[0]
        before = len(sent)
        await receive(session, query)
        assert len(sent) == before and not engine.store.operations()
        await receive(session, frames()[0])
        assert await receive(session, query) == "ap_metric_query_answered"
        packet = assemble((sent[-1],))
        assert (packet.message_type, packet.mid) == (0x800C, 711)
        assert [t.kind for t in packet.tlvs] == [0x94, 0xC7, 0xC6]
        assert len(engine.store.operations()) == backend.writes == 1
        source.invalidate()
        before = len(sent)
        assert await receive(session, query) == "source_unavailable"
        assert len(sent) == before and session.ap_metric_source.current() is None
        session.close()

    asyncio.run(scenario())


def test_discovery_early_m1_authenticated_operation_without_semantic_input(rig):
    async def scenario():
        bridge, engine, backend, _ = rig
        session, _, sent = lifecycle(rig)
        assert await receive(session, frames()[0]) == "wsc_before_admission"
        assert not engine.store.operations()
        await session.tick()
        assert await receive(session, response()) == "early_then_m1_sent"
        assert [assemble((f,)).message_type for f in sent] == [7, 0x8043, 9]
        assert await receive(session, frames()[0]) == "wsc_operation"
        assert await receive(session, frames(m2(mid=987))[0]) == "wsc_operation"
        assert len(engine.store.operations()) == backend.writes == 1
        receipt = engine.store.wsc_receipt(engine.store.operations()[0].operation_id)
        assert receipt["m1_sha256"] == bridge.exchange.m1_sha256
        query = fragment_message(BINDING.local_al, BINDING.controller_al, 0x8001, 77, ())[0]
        assert await receive(session, query) == "ap_capability_report_sent"
        report = assemble((sent[-1],))
        assert (report.message_type, report.mid) == (0x8002, 77)
        assert {0xA1, 0x85, 0xD4, 0xB4} <= {t.kind for t in report.tlvs}
        assert not {0xA9, 0xAD, 0xB2, 0xED} & {t.kind for t in report.tlvs}
        session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "flags,extra",
    [
        (b"\x40", ()),
        (b"\x80", ()),
        (b"\xc0", (Tlv(0xA9, b""),)),
        (b"\xc0", (Tlv(0xA9, b"\1\0\0"),)),
        (b"\xc0", (Tlv(0xAB, b"x"),)),
    ],
)
def test_incompatible_response_never_sends_m1_or_creates_operation(rig, flags, extra):
    async def scenario():
        _, engine, backend, _ = rig
        session, _, sent = lifecycle(rig)
        await session.tick()
        await receive(session, response(flags=flags, extra=extra))
        assert len(sent) == 1
        assert not engine.store.operations() and backend.writes == 0

    asyncio.run(scenario())


def test_wrong_mid_then_source_loss_does_not_revive_write_authority(rig):
    async def scenario():
        _, engine, backend, _ = rig
        session, source, sent = lifecycle(rig)
        await session.tick()
        await receive(session, response(mid=555))
        assert session.state == "discovering" and len(sent) == 1
        assert await receive(session, response()) == "early_then_m1_sent"
        source.invalidate()
        await session.tick()
        assert session.state == "source_lost"
        assert await receive(session, frames()[0]) == "inactive_input"
        assert not engine.store.operations() and backend.writes == 0

    asyncio.run(scenario())


def test_final_stats_require_provisioning_and_an_observed_departure(rig):
    from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients
    from emosa.errors import EmosaError
    from emosa.wire.disassociation import FinalSession, TrafficCounters

    async def scenario():
        _, _, _, clock = rig
        session, source, sent = lifecycle(rig)
        original = source.current()
        bssid = original.topology.operational.radios[0].bsses[0].ap_mac
        station = bytes.fromhex("020000009988")
        event = FinalSession(
            original.context_token,
            "measured-session-1",
            bssid,
            station,
            clock.monotonic(),
            3,
            TrafficCounters(100, 200, 3, 4, 0, 0, 1),
        )
        with pytest.raises(EmosaError):
            session.report_final_session(event)
        await session.tick()
        await receive(session, response())
        with pytest.raises(EmosaError):
            session.report_final_session(event)
        await receive(session, frames()[0])
        with pytest.raises(EmosaError):
            session.report_final_session(event)
        joined = replace(
            original.topology,
            clients=AssociatedClients((BssClients(bssid, (AssociatedClient(station, 20),)),)),
            inventory_complete=True,
        )
        source.publish((1, 2), original.capabilities, joined, observed_at=clock.monotonic())
        await session.tick()
        left = replace(joined, clients=AssociatedClients((BssClients(bssid, ()),)))
        source.publish((1, 3), original.capabilities, left, observed_at=clock.monotonic())
        await session.tick()
        assert session.report_final_session(event) == "final_session_report_sent"
        report = assemble((sent[-1],))
        assert report.message_type == 0x8022
        ack = fragment_message(BINDING.local_al, BINDING.controller_al, 0x8000, report.mid, ())[0]
        assert await receive(session, ack) == "final_session_acknowledged"
        source.publish((1, 4), original.capabilities, joined, observed_at=clock.monotonic())
        await session.tick()
        with pytest.raises(EmosaError):
            session.report_final_session(event)
        session.close()
        assert not session.departures and not session.disassociations.pending

    asyncio.run(scenario())


def test_recovery_requires_fresh_discovery_and_rejects_previous_m2(rig, monkeypatch):
    from test_autoconfiguration import exchange

    from emosa import wsc_messages
    from emosa.wire.operation_bridge import WscComponentBridge

    async def scenario():
        old_bridge, engine, backend, clock = rig
        template, source, sent = lifecycle(rig)
        initial = source.current()
        mids = MidSequence(500)
        bridges = [old_bridge]

        def bridge_factory(*_):
            if len(bridges) == 1 and old_bridge.exchange.state != "closed":
                return old_bridge
            fresh = exchange(clock=clock.monotonic)
            fresh.basic = old_bridge.exchange.basic
            fresh.capabilities = old_bridge.exchange.capabilities
            bridge = WscComponentBridge(
                engine, fresh, old_bridge.target, old_bridge.current_context, run_id="recovery"
            )
            # The old bridge context was wrapped by its admitted session. A new
            # bridge must use an independent, current backend context instead.
            from emosa.wire.operation_bridge import ScopeContext

            async def context():
                return ScopeContext(backend.generation, "synthetic-model-v1", "bound-fixture")

            bridge.current_context = context
            bridges.append(bridge)
            return bridge

        recovery = OnboardingRecovery(
            source,
            lambda: OnboardingSession(
                source,
                sent.append,
                bridge_factory,
                template.inventory,
                mids=mids,
                clock=clock.monotonic,
            ),
            clock=clock.monotonic,
        )
        await recovery.tick()
        await receive(recovery, response())
        old_hash = old_bridge.exchange.m1_sha256
        source.invalidate()
        await recovery.tick()
        assert old_bridge.exchange.state == "closed" and recovery.state == "recovering"
        assert await receive(recovery, frames()[0]) == "recovery_waiting_for_fresh_source"
        clock.advance(1)
        await recovery.tick()
        assert recovery.starts == 1  # Time alone cannot restore the source.
        backend.reconnect()
        source.publish(
            (2, 1), initial.capabilities, initial.topology, observed_at=clock.monotonic()
        )
        monkeypatch.setattr(wsc_messages.secrets, "token_bytes", lambda n: b"\x55" * n)
        await recovery.tick()
        new_mid = assemble((sent[-1],)).mid
        assert recovery.starts == 2 and new_mid != 501
        await receive(recovery, response())  # Delayed old discovery response.
        assert recovery.state == "discovering"
        assert await receive(recovery, frames()[0]) == "wsc_before_admission"
        assert await receive(recovery, response(mid=new_mid)) == "early_then_m1_sent"
        assert bridges[-1].exchange.m1_sha256 != old_hash
        await receive(recovery, frames()[0])  # Valid only for the old M1.
        assert not engine.store.operations() and backend.writes == 0
        recovery.close()
        assert bridges[-1].exchange.state == "closed"

    asyncio.run(scenario())


def test_recovery_backoff_and_incompatible_admission_are_bounded(rig):
    async def scenario():
        _, _, _, clock = rig
        template, source, sent = lifecycle(rig)
        initial = source.current()
        recovery = OnboardingRecovery(
            source,
            lambda: OnboardingSession(
                source,
                sent.append,
                template.factory,
                template.inventory,
                mids=MidSequence(500),
                clock=clock.monotonic,
            ),
            clock=clock.monotonic,
        )
        for attempt in range(40):
            source.publish(
                (1, attempt * 2 + 2),
                initial.capabilities,
                initial.topology,
                observed_at=clock.monotonic(),
                lifetime=2,
            )
            await recovery.tick()
            clock.advance(6)  # No controller response before discovery deadline.
            source.publish(
                (1, attempt * 2 + 3),
                initial.capabilities,
                initial.topology,
                observed_at=clock.monotonic(),
            )
            now = clock.monotonic()
            await recovery.tick()
            delay = recovery.next_start - now
            assert 1 <= delay <= 30 and len(recovery.history) <= 32
            await recovery.tick()
            assert recovery.session is None
            clock.advance(delay)
        source.publish(
            (1, 100), initial.capabilities, initial.topology, observed_at=clock.monotonic()
        )
        await recovery.tick()
        await receive(recovery, response(flags=b"\x40"))
        assert recovery.state == "incompatible"
        starts = recovery.starts
        clock.advance(1)
        await recovery.tick()
        assert recovery.starts == starts
        recovery.close()

    asyncio.run(scenario())


def test_partial_early_send_failure_never_starts_m1(rig):
    async def scenario():
        _, engine, _, _ = rig
        session, _, sent = lifecycle(rig)
        await session.tick()

        def fail(frame):
            raise OSError("injected transport failure")

        session.send_frame = fail
        await receive(session, response())
        assert session.state == "failed" and len(sent) == 1
        assert not engine.store.operations()

    asyncio.run(scenario())


def test_unsupported_policy_is_not_acknowledged_as_applied(rig):
    async def scenario():
        _, engine, backend, _ = rig
        session, _, sent = lifecycle(rig)
        await session.tick()
        await receive(session, response())
        count = len(sent)
        query = fragment_message(
            BINDING.local_al, BINDING.controller_al, 0x8003, 77, (Tlv(0x8A, b"unsupported"),)
        )[0]
        assert await receive(session, query) == "unsupported_message_8003"
        assert len(sent) == count and not engine.store.operations() and backend.writes == 0

    asyncio.run(scenario())


def test_missing_security_is_contextual_not_a_change_to_general_diagnostics():
    from emosa.wire.autoconfiguration import parse_response

    ad = parse_response(assemble((response(),)))
    assert ad.selected_response_issues == ("security_capability_absent",)
    assert non_dpp_admission(ad) == ()


def test_station_join_age_update_unknown_and_leave_notifications(rig):
    from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients

    async def scenario():
        session, source, sent = lifecycle(rig)
        await session.tick()
        await receive(session, response())
        snap = source.current()
        bssid = snap.topology.clients.bsses[0].bssid
        mac = bytes.fromhex("020000000200")

        def publish(revision, clients, complete=True):
            source.publish(
                (1, revision),
                snap.capabilities,
                replace(
                    snap.topology,
                    inventory_complete=complete,
                    clients=AssociatedClients((BssClients(bssid, clients),)),
                ),
                observed_at=snap.stamp.observed_at,
            )

        publish(2, (AssociatedClient(mac, 123),))
        await session.tick()
        notification = assemble((sent[-1],))
        assert notification.message_type == 1 and notification.relay
        assert notification.tlvs == (Tlv(1, BINDING.local_al), Tlv(0x92, mac + bssid + b"\x80"))
        count = len(sent)
        publish(3, (AssociatedClient(mac, 124),))
        await session.tick()
        assert len(sent) == count  # Duration advancing is not another association.
        publish(4, (), False)
        await session.tick()
        assert len(sent) == count  # Unknown is not a disassociation.
        publish(5, ())
        await session.tick()
        notification = assemble((sent[-1],))
        assert notification.tlvs[-1] == Tlv(0x92, mac + bssid + b"\0")
        assert session.counts["disassociation_statistics_unavailable"] == 1
        session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("associated,reason", [(True, 3), (False, 2)])
def test_client_capability_unavailable_is_explicit_specification_error(rig, associated, reason):
    from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients

    async def scenario():
        session, source, sent = lifecycle(rig)
        await session.tick()
        await receive(session, response())
        snap = source.current()
        bssid = snap.topology.clients.bsses[0].bssid
        mac = bytes.fromhex("020000000200")
        source.publish(
            (1, 2),
            snap.capabilities,
            replace(
                snap.topology,
                clients=AssociatedClients(
                    (BssClients(bssid, (AssociatedClient(mac, 5),) if associated else ()),)
                ),
            ),
            observed_at=snap.stamp.observed_at,
        )
        info = Tlv(0x90, bssid + mac)
        query = fragment_message(BINDING.local_al, BINDING.controller_al, 0x8009, 712, (info,))[0]
        assert await receive(session, query) == "client_capability_unavailable_report"
        report = assemble((sent[-1],))
        assert (report.message_type, report.mid) == (0x800A, 712)
        assert report.tlvs == (info, Tlv(0x91, b"\x01"), Tlv(0xA3, bytes([reason]) + mac))
        session.close()

    asyncio.run(scenario())


def test_bound_policy_receipt_does_not_claim_reporting_or_create_config_operation(rig, tmp_path):
    from emosa.wire.reporting_policy import ReportingPolicyStore

    async def scenario():
        bridge, engine, backend, _ = rig
        session, source, sent = lifecycle(rig)
        store = ReportingPolicyStore(tmp_path / "policy.sqlite", boot_id="boot-1")
        session.reporting_policy_store = store
        try:
            await session.tick()
            await receive(session, response())
            query = fragment_message(
                BINDING.local_al,
                BINDING.controller_al,
                0x8003,
                77,
                (Tlv(0x8A, b"\x3c\x01" + bridge.target.ruid + b"\0\0\0\xe0"),),
            )[0]
            assert await receive(session, query) == "policy_receipt_ack_sent"
            ack = assemble((sent[-1],))
            assert (ack.message_type, ack.mid) == (0x8000, 77)
            assert not session.status()["reporting_policy"]["required_reporting_proven"]
            assert not engine.store.operations() and backend.writes == 0
            source.invalidate()
            await session.tick()
            assert session.reporting_policy.closed
            before = len(sent)
            await receive(session, query)
            assert len(sent) == before
        finally:
            session.close()
            store.close()

    asyncio.run(scenario())


def test_clients_are_announced_again_once_the_controller_knows_the_bss(rig):
    from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients

    async def scenario():
        session, source, sent = lifecycle(rig)
        await session.tick()
        await receive(session, response())
        snap = source.current()
        bssid = snap.topology.clients.bsses[0].bssid
        mac = bytes.fromhex("020000000200")
        source.publish(
            (1, 2),
            snap.capabilities,
            replace(
                snap.topology,
                clients=AssociatedClients((BssClients(bssid, (AssociatedClient(mac, 5),)),)),
            ),
            observed_at=snap.stamp.observed_at,
        )
        await session.tick()  # the early join, possibly before the controller knows the BSS
        count = len(sent)
        # M2 created the operation; the controller has not asked for topology yet.
        session.state, session.topology_mark = "provisioning", 0
        session.reports.counts["topology_response_sent"] = 0
        await session.tick()
        assert len(sent) == count
        session.reports.counts["topology_response_sent"] = 1
        await session.tick()
        notification = assemble((sent[-1],))
        assert notification.tlvs[-1] == Tlv(0x92, mac + bssid + b"\x80")
        assert session.counts["clients_reannounced"] == 1
        count = len(sent)
        session.reports.counts["topology_response_sent"] = 2
        await session.tick()
        assert len(sent) == count  # once per configuration, not on every query
        session.close()

    asyncio.run(scenario())
