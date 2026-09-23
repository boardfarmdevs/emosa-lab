import asyncio
import json
import os
import sqlite3
from dataclasses import replace

import pytest
from test_autoconfiguration import BINDING, exchange, m2
from test_autoconfiguration import fixed_entropy as fixed_entropy

from emosa.backends.mock import ModelBackend
from emosa.clock import ManualClock
from emosa.errors import EmosaError, Reason
from emosa.model import Intent, State
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.store import Store
from emosa.wire.operation_bridge import ComponentTarget, ScopeContext, WscComponentBridge

pytestmark = pytest.mark.unit


@pytest.fixture
def rig(tmp_path, fixed_entropy):
    clock = ManualClock()
    vault, store = SecretStore(tmp_path / "secrets"), Store(tmp_path / "state")
    backend = ModelBackend("pod-1", vault, clock)
    engine = Engine(store, vault, {"pod-1": backend}, clock)
    session = exchange(clock=clock.monotonic)
    session.basic = replace(session.basic, max_bss=1)
    target = ComponentTarget(
        "pod-1", "radio-1", "bss-1", session.basic.ruid, bytes.fromhex("020000001001")
    )

    async def context():
        if not backend.ready:
            raise EmosaError(Reason.NOT_READY, "model source unavailable")
        return ScopeContext(backend.generation, "synthetic-model-v1", "bound-fixture")

    bridge = WscComponentBridge(engine, session, target, context, run_id="wsc-test")
    yield bridge, engine, backend, clock
    bridge.close()
    if engine.store.db is not None:
        engine.store.close()


async def receive(bridge, message=None, **kwargs):
    return await bridge.receive(
        m2() if message is None else message,
        **({"ingress": BINDING.ingress, "generation": BINDING.generation} | kwargs),
    )


def test_authenticated_message_has_atomic_receipt_and_distinct_operation_origin(rig):
    async def scenario():
        bridge, engine, backend, _ = rig
        sent = []
        await bridge.start(sent.append)
        op = await receive(bridge)
        receipt = engine.store.wsc_receipt(op.operation_id)
        assert receipt["exchange_id"] == bridge.exchange.exchange_id
        assert receipt["m1_sha256"] == bridge.exchange.m1_sha256
        assert receipt["ruid"] == bridge.target.ruid.hex()
        assert op.initiating_interface == "wsc-component"
        assert op.state == State.REQUESTED and backend.writes == 0
        assert engine.vault.resolve(op.intent["secret_ref"]) == "public-vector-passphrase"
        assert await receive(bridge, m2(mid=777)) == op
        assert len(engine.store.operations()) == 1
        result = await engine.execute(op.operation_id)
        assert result.state == State.CONFIG_COMMITTED and backend.writes == 1
        assert (await receive(bridge)).operation_id == op.operation_id
        assert backend.writes == 1
        backend.device_step()
        await engine.reconcile("pod-1")
        assert engine.store.get(op.operation_id).state == State.OBSERVED_APPLIED
        public = json.dumps([p.to_dict() for p in engine.store.operations()])
        public += json.dumps(engine.store.events("wsc-test"))
        assert "public-vector-passphrase" not in public
        assert receipt["request_fingerprint"] not in public

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["auth", "ruid", "ingress", "generation", "context", "expired", "scope"]
)
def test_rejected_input_cannot_create_operation_secret_or_write(rig, failure):
    async def scenario():
        bridge, engine, backend, clock = rig
        await bridge.start(lambda frame: None)
        message, kwargs = m2(), {}
        if failure == "auth":
            from emosa.wire.cmdu import Tlv

            message = replace(
                message, tlvs=(message.tlvs[0], Tlv(0x11, message.tlvs[1].value[:-1] + b"\0"))
            )
        elif failure == "ruid":
            from emosa.wire.cmdu import Tlv

            message = replace(
                message, tlvs=(Tlv(0x82, bytes.fromhex("02000000ffff")), message.tlvs[1])
            )
        elif failure in {"ingress", "generation"}:
            kwargs[failure] = "alien" if failure == "ingress" else 999
        elif failure == "context":
            backend.reconnect()
        elif failure == "expired":
            clock.advance(5)
        else:

            async def reject(intent):
                raise EmosaError(Reason.PRECONDITION_FAILED, "scope changed")

            backend.plan = reject
        with pytest.raises(EmosaError):
            await receive(bridge, message, **kwargs)
        assert not engine.store.operations() and backend.writes == 0
        assert not list(engine.vault.directory.glob("wsc-*"))

    asyncio.run(scenario())


def test_database_receipt_failure_rolls_back_operation_and_removes_created_secret(rig):
    async def scenario():
        bridge, engine, backend, _ = rig
        await bridge.start(lambda frame: None)
        engine.store.db.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON wsc_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await receive(bridge)
        assert not engine.store.operations() and not engine.store.events("wsc-test")
        assert not list(engine.vault.directory.glob("wsc-*")) and backend.writes == 0

    asyncio.run(scenario())


def test_transient_source_loss_does_not_revive_old_exchange(rig):
    async def scenario():
        bridge, engine, backend, _ = rig
        await bridge.start(lambda frame: None)
        backend.ready = False
        with pytest.raises(EmosaError):
            await receive(bridge)
        backend.ready = True
        with pytest.raises(EmosaError):
            await receive(bridge)
        assert not engine.store.operations() and backend.writes == 0
        assert not list(engine.vault.directory.glob("wsc-*"))

    asyncio.run(scenario())


def test_existing_secret_is_never_overwritten_or_deleted(rig):
    async def scenario():
        bridge, engine, _, _ = rig
        await bridge.start(lambda frame: None)
        ref = "wsc-" + bridge.exchange.exchange_id
        engine.vault.persist_received(ref, "ExistingPrivateCredential")
        with pytest.raises(FileExistsError):
            await receive(bridge)
        assert engine.vault.resolve(ref) == "ExistingPrivateCredential"
        assert not engine.store.operations()

    asyncio.run(scenario())


def test_secret_fsync_failure_cannot_leave_journal_reference(rig, monkeypatch):
    async def scenario():
        bridge, engine, _, _ = rig
        await bridge.start(lambda frame: None)

        def fail(fd):
            raise OSError("injected fsync failure")

        monkeypatch.setattr(os, "fsync", fail)
        with pytest.raises(OSError):
            await receive(bridge)
        assert not engine.store.operations() and not list(engine.vault.directory.glob("wsc-*"))

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["requested", "validated", "submitted", "committed"])
def test_restart_cancels_unsent_and_reconciles_sent_without_replay(rig, phase):
    async def scenario():
        bridge, engine, backend, clock = rig
        await bridge.start(lambda frame: None)
        op = await receive(bridge)
        if phase == "committed":
            await engine.execute(op.operation_id)
        elif phase in {"validated", "submitted"}:
            from emosa.operations import transition

            transition(op, State.VALIDATED)
            if phase == "submitted":
                op.attempts.append(
                    {
                        "attempt_id": "crash-attempt",
                        "transaction_id": "crash-transaction",
                        "session_generation": 1,
                        "prepared_at": clock.utc(),
                    }
                )
                transition(op, State.SUBMITTED)
            engine.store.save(op)
        path = engine.store.directory
        engine.store.close()
        engine.store = Store(path)
        recovered = Engine(engine.store, engine.vault, {"pod-1": backend}, clock)
        recovered.recover()
        result = recovered.store.get(op.operation_id)
        assert (
            result.state
            == (
                {
                    "requested": State.CANCELLED,
                    "validated": State.CANCELLED,
                    "submitted": State.INDETERMINATE,
                    "committed": State.CONFIG_COMMITTED,
                }[phase]
            )
        )
        await recovered.execute(op.operation_id)
        assert backend.writes == (1 if phase == "committed" else 0)
        with pytest.raises(EmosaError):
            await receive(bridge)
        if phase == "committed":
            backend.device_step()
            await recovered.reconcile("pod-1")
            assert recovered.store.get(op.operation_id).state == State.OBSERVED_APPLIED
        assert recovered.store.wsc_receipt(op.operation_id) is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["closed", "expired", "reconnect", "guard_missing"])
def test_staged_request_loses_authority_before_execution(rig, change):
    async def scenario():
        bridge, engine, backend, clock = rig
        await bridge.start(lambda frame: None)
        op = await receive(bridge)
        if change == "closed":
            bridge.close()
        elif change == "expired":
            clock.advance(5)
        elif change == "reconnect":
            backend.reconnect()
        else:
            engine.wsc_guards.clear()
        assert (await engine.execute(op.operation_id)).state == State.REJECTED
        assert backend.writes == 0

    asyncio.run(scenario())


def test_concurrent_input_has_bounded_handoff_backpressure(rig):
    async def scenario():
        bridge, engine, backend, _ = rig
        await bridge.start(lambda frame: None)
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.plan

        async def hold(intent):
            entered.set()
            await release.wait()
            return await original(intent)

        backend.plan = hold
        task = asyncio.create_task(receive(bridge))
        await entered.wait()
        with pytest.raises(EmosaError) as caught:
            await receive(bridge)
        assert caught.value.code == Reason.BUSY
        release.set()
        await task
        assert len(engine.store.operations()) == 1

    asyncio.run(scenario())


def test_public_request_cannot_select_component_or_full_wire_origin(rig):
    _, engine, _, _ = rig
    intent = Intent("pod-1", "radio-1", "bss-1", "unused", "missing-key")
    for origin in ("wsc-component", "easymesh-wire"):
        with pytest.raises(EmosaError) as caught:
            engine.request(
                intent, source="caller", key="key", run_id="test", initiating_interface=origin
            )
        assert caught.value.code == Reason.MISSING_PREREQUISITE
    assert not engine.store.operations()
