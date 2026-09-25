import asyncio
from dataclasses import replace

import pytest

from emosa.model import State
from emosa.wire.cmdu import Tlv, fragment_message
from emosa.wire.provisioning_session import ComponentProvisioningSession
from test_autoconfiguration import BINDING, m2
from test_autoconfiguration import fixed_entropy as fixed_entropy
from test_wsc_operation_bridge import rig as rig

pytestmark = pytest.mark.unit


def frames(message=None, *, fragmented=False):
    message = message or m2()
    return fragment_message(
        message.destination,
        message.source,
        message.message_type,
        message.mid,
        (*message.tlvs, *((Tlv(0xEF, bytes(1024)),) if fragmented else ())),
    )


async def receive(session, frame, **kwargs):
    return await session.receive(
        frame, **({"ingress": BINDING.ingress, "generation": BINDING.generation} | kwargs)
    )


def test_complete_packet_input_drives_operation_and_duplicate_reuses_it(rig):
    async def scenario():
        bridge, engine, backend, clock = rig
        sent = []
        session = ComponentProvisioningSession(bridge, sent.append, clock=clock.monotonic)
        await session.start()
        assert sent
        incoming = frames(fragmented=True)
        assert len(incoming) == 2
        assert (await receive(session, incoming[1]))["status"] == "incomplete"
        assert not engine.store.operations() and backend.writes == 0
        result = await receive(session, incoming[0])
        assert result["status"] == "operation" and result["state"] == State.CONFIG_COMMITTED
        again = await receive(session, frames(m2(mid=999))[0])
        assert again["operation_id"] == result["operation_id"]
        assert backend.writes == 1 and len(engine.store.operations()) == 1
        session.close()
        assert (await receive(session, incoming[0]))["status"] == "closed"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["ingress", "generation", "peer", "kind", "auth"])
def test_packet_rejection_never_creates_an_operation(rig, failure):
    async def scenario():
        bridge, engine, backend, clock = rig
        session = ComponentProvisioningSession(bridge, lambda _: None, clock=clock.monotonic)
        await session.start()
        message, kwargs = m2(), {}
        if failure == "ingress":
            kwargs["ingress"] = "wrong"
        elif failure == "generation":
            kwargs["generation"] = 88
        elif failure == "peer":
            message = replace(message, source=bytes.fromhex("02000000ff01"))
        elif failure == "kind":
            message = replace(message, message_type=2)
        else:
            value = message.tlvs[1].value
            message = replace(
                message, tlvs=(message.tlvs[0], Tlv(0x11, value[:-1] + bytes([value[-1] ^ 1])))
            )
        assert (await receive(session, frames(message)[0], **kwargs))["status"] in {
            "rejected",
            "unsupported_message",
        }
        assert not session.assembly.contexts
        assert not engine.store.operations() and backend.writes == 0
        assert not list(engine.vault.directory.glob("wsc-*"))

    asyncio.run(scenario())


def test_fragment_expiry_and_input_budgets_are_bounded(rig):
    async def scenario():
        bridge, engine, backend, clock = rig
        session = ComponentProvisioningSession(bridge, lambda _: None, clock=clock.monotonic)
        await session.start()
        await receive(session, frames(fragmented=True)[0])
        assert session.assembly.buffered > 0
        clock.advance(5)
        session.tick()
        assert session.assembly.buffered == 0
        for _ in range(40):
            await receive(session, b"invalid")
        assert session.counts["rate_limited"] == 8 and len(session.events) == 32
        assert not engine.store.operations() and backend.writes == 0

    asyncio.run(scenario())


def test_packet_handler_has_no_unbounded_concurrent_waiters(rig):
    async def scenario():
        bridge, engine, backend, clock = rig
        session = ComponentProvisioningSession(bridge, lambda _: None, clock=clock.monotonic)
        await session.start()
        entered, release = asyncio.Event(), asyncio.Event()
        plan = backend.plan

        async def wait(intent):
            entered.set()
            await release.wait()
            return await plan(intent)

        backend.plan = wait
        task = asyncio.create_task(receive(session, frames()[0]))
        await entered.wait()
        assert (await receive(session, frames()[0]))["status"] == "busy"
        release.set()
        await task
        assert backend.writes == 1 and len(engine.store.operations()) == 1

    asyncio.run(scenario())
