import asyncio
import json
from dataclasses import replace

import pytest

from emosa.backends.mock import ModelBackend
from emosa.clock import ManualClock
from emosa.errors import EmosaError, Reason
from emosa.model import Intent, State
from emosa.operations import transition
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.store import Store

pytestmark = pytest.mark.unit


@pytest.fixture
def rig(tmp_path):
    clock = ManualClock()
    vault = SecretStore(tmp_path / "secrets")
    vault.write_simulated("test-key", "a-test-credential")
    store = Store(tmp_path / "state")
    backend = ModelBackend("pod-1", vault, clock)
    engine = Engine(store, vault, {"pod-1": backend}, clock)
    intent = Intent("pod-1", "radio-1", "bss-1", "test-network", "test-key")
    yield engine, backend, intent, clock
    store.close()


def requested(engine, intent, key="key-1", deadline=30):
    return engine.request(intent, source="test", key=key, run_id="run-1", deadline=deadline)


def test_commit_is_not_application_and_partial_state_is_not_success(rig):
    async def scenario():
        engine, backend, intent, _ = rig
        op = requested(engine, intent)
        await engine.execute(op.operation_id)
        await engine.reconcile("pod-1")
        assert engine.store.get(op.operation_id).state == State.CONFIG_COMMITTED
        backend.device_step(partial=True)
        await engine.reconcile("pod-1")
        assert engine.store.get(op.operation_id).state == State.CONFIG_COMMITTED
        backend.device_step()
        await engine.reconcile("pod-1")
        assert engine.store.get(op.operation_id).state == State.OBSERVED_APPLIED
        noop = requested(engine, intent, key="key-2")
        await engine.execute(noop.operation_id)
        assert engine.store.get(noop.operation_id).changed is False
        assert backend.writes == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault,expected", [("reject", State.FAILED), ("guard-conflict", State.OWNERSHIP_CONFLICT)]
)
def test_rejected_config(rig, fault, expected):
    async def scenario():
        engine, backend, intent, _ = rig
        backend.fault = fault
        op = requested(engine, intent)
        assert (await engine.execute(op.operation_id)).state == expected
        assert backend.writes == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("lost", [False, True])
def test_deadline_and_late_evidence_preserve_timing_verdict(rig, lost):
    async def scenario():
        engine, backend, intent, clock = rig
        backend.fault = "lost-reply" if lost else "none"
        op = requested(engine, intent)
        await engine.execute(op.operation_id)
        clock.advance(31)
        await engine.reconcile("pod-1")
        timed = engine.store.get(op.operation_id)
        assert timed.state == (State.INDETERMINATE if lost else State.TIMED_OUT)
        assert timed.deadline_elapsed
        backend.device_step()
        backend.reconnect()
        await engine.reconcile("pod-1")
        late = engine.store.get(op.operation_id)
        assert late.late_resolution == "applied_after_deadline"
        assert late.deadline_elapsed
        assert late.commit_evidence["attribution"] == ("unknown" if lost else "reply")
        if not lost:
            assert late.state == State.TIMED_OUT

    asyncio.run(scenario())


def test_unknown_outcome_lost_with_a_pod_restart_stops_blocking_after_the_deadline(rig):
    async def scenario():
        engine, backend, intent, clock = rig
        backend.fault = "lost-reply"
        op = requested(engine, intent)
        await engine.execute(op.operation_id)
        # The pod restarts: the unconfirmed write is gone from its config.
        backend.config["ssid"] = "initial-network"
        backend.pending = None
        backend.reconnect()
        snapshot = backend.snapshot

        async def no_state_row():  # the pod has no VIF State row: not fresh
            snap = await snapshot()
            snap.observed.fresh = False
            return snap

        backend.snapshot = no_state_row
        backend.fault = "none"
        await engine.reconcile("pod-1")
        assert engine.store.get(op.operation_id).state == State.INDETERMINATE
        blocked = requested(engine, intent, key="key-2")
        assert blocked.state == State.REJECTED and blocked.reason == Reason.BUSY
        clock.advance(31)
        await engine.reconcile("pod-1")
        timed = engine.store.get(op.operation_id)
        assert timed.state == State.TIMED_OUT and timed.reason == Reason.APPLY_TIMEOUT
        retry = requested(engine, intent, key="key-3")
        assert (await engine.execute(retry.operation_id)).state == State.CONFIG_COMMITTED

    asyncio.run(scenario())


def test_idempotency_busy_and_scope(rig):
    engine, _, intent, _ = rig
    op = requested(engine, intent)
    assert requested(engine, intent).operation_id == op.operation_id
    with pytest.raises(EmosaError, match="different intent"):
        requested(engine, replace(intent, ssid="different"))
    assert requested(engine, intent, "second").reason == Reason.BUSY
    assert engine.request(intent, source="other", key="key-1", run_id="run-1").reason == Reason.BUSY


def test_conflict_stops_new_writes(rig):
    async def scenario():
        engine, backend, intent, _ = rig
        op = requested(engine, intent)
        await engine.execute(op.operation_id)
        backend.competing_writer()
        await engine.reconcile("pod-1")
        assert engine.store.get(op.operation_id).state == State.OWNERSHIP_CONFLICT
        assert requested(engine, intent, "new-key").reason == Reason.OWNERSHIP_CONFLICT
        assert backend.writes == 1

    asyncio.run(scenario())


def test_restart_after_durable_submission_never_blindly_replays(rig):
    async def scenario():
        engine, backend, intent, _ = rig
        op = requested(engine, intent)
        transition(op, State.VALIDATED)
        op.attempts.append(
            {
                "attempt_id": "crash-attempt",
                "transaction_id": "crash-attempt",
                "session_generation": 1,
                "prepared_at": engine.clock.utc(),
            }
        )
        transition(op, State.SUBMITTED)
        engine.store.save(op)
        # External result may have committed before the process died.
        backend.config.update(intent.target(engine.vault))
        backend.observed.update(backend.config)
        old_store = engine.store
        directory = old_store.directory
        old_store.close()
        engine.store = Store(directory)
        engine.recover()
        assert engine.store.get(op.operation_id).state == State.INDETERMINATE
        backend.reconnect()
        await engine.reconcile("pod-1")
        resolved = engine.store.get(op.operation_id)
        assert resolved.state == State.OBSERVED_APPLIED
        assert resolved.commit_evidence["attribution"] == "unknown"
        assert backend.writes == 0
        engine.store.close()

    asyncio.run(scenario())


def test_missing_secret_keeps_read_only_recovery(rig):
    async def scenario():
        engine, backend, intent, _ = rig
        backend.fault = "lost-reply"
        op = requested(engine, intent)
        await engine.execute(op.operation_id)
        (engine.vault.directory / "test-key").unlink()
        backend.reconnect()
        await engine.reconcile("pod-1")
        assert engine.store.get(op.operation_id).blocked_for_resubmission
        assert backend.writes == 1

    asyncio.run(scenario())


def test_transition_guards_and_caller_wait(rig):
    async def scenario():
        engine, _, intent, _ = rig
        op = requested(engine, intent)
        with pytest.raises(EmosaError):
            transition(op, State.OBSERVED_APPLIED)
        with pytest.raises(EmosaError) as exc:
            await engine.wait(op.operation_id, 0)
        assert exc.value.details["operation_id"] == op.operation_id
        assert engine.store.get(op.operation_id).state == State.REQUESTED
        engine.cancel(op.operation_id)
        assert (await engine.execute(op.operation_id)).state == State.CANCELLED

    asyncio.run(scenario())


def test_secret_and_resource_validation(rig):
    engine, _, intent, _ = rig
    with pytest.raises(EmosaError):
        requested(engine, replace(intent, ssid="🙂" * 9))
    with pytest.raises(EmosaError):
        requested(engine, replace(intent, secret_ref="../escape"))
    with pytest.raises(EmosaError):
        requested(engine, replace(intent, security_mode="wpa3-sae"))
    requested(engine, intent)
    assert "a-test-credential" not in json.dumps([p.to_dict() for p in engine.store.operations()])


def test_writer_lock_and_event_exhaustion(rig):
    engine, _, intent, _ = rig
    with pytest.raises(EmosaError, match="active writer"):
        Store(engine.store.directory)
    engine.store.max_events = 2
    requested(engine, intent)
    for i in range(5):
        engine.store.event("run-1", "diagnostic", {"i": i, "psk": "must-never-leak"})
    events = engine.store.events("run-1")
    assert len(events) == 2 and events[0]["sequence"] == 5
    assert "must-never-leak" not in json.dumps(events)
    engine.store.max_operations = 1
    with pytest.raises(EmosaError, match="budget exhausted"):
        requested(engine, intent, "other")


def test_32_model_pods_isolate_delayed_application(rig):
    async def scenario():
        engine, _, intent, _ = rig
        engine.backends = {
            f"pod-{index}": ModelBackend(f"pod-{index}", engine.vault, engine.clock)
            for index in range(32)
        }
        operations = [
            requested(engine, replace(intent, pod_id=pod_id)) for pod_id in engine.backends
        ]
        await asyncio.gather(*(engine.execute(op.operation_id) for op in operations))
        for pod_id, backend in engine.backends.items():
            if pod_id != "pod-0":
                backend.device_step()
        await asyncio.gather(*(engine.reconcile(pod_id) for pod_id in engine.backends))
        assert engine.store.get(operations[0].operation_id).state == State.CONFIG_COMMITTED
        assert all(
            engine.store.get(op.operation_id).state == State.OBSERVED_APPLIED
            for op in operations[1:]
        )

    asyncio.run(scenario())
