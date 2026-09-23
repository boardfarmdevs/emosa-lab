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
from emosa.wire.onboarding import OnboardingSession, non_dpp_admission

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
