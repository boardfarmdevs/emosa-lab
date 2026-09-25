import asyncio
import socket
import time

import pytest

from emosa.errors import EmosaError, Reason
from emosa.model import Intent, State
from emosa.opensync.mapping import OpenSyncBackend, check_results, where_uuid
from emosa.opensync.session import OvsSession
from emosa.reconcile import Engine
from emosa.secrets import SecretStore
from emosa.store import Store
from emosa_lab.simulation.database import SimDatabase, SimManager

pytestmark = pytest.mark.ovsdb


async def eventually(function, predicate, timeout=8):
    end = time.monotonic() + timeout
    while True:
        result = await function()
        if predicate(result):
            return result
        if time.monotonic() >= end:
            raise AssertionError("condition did not converge within test deadline")
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("none", State.OBSERVED_APPLIED),
        ("lost-reply", State.OBSERVED_APPLIED),
        ("withhold", State.TIMED_OUT),
        ("guard-conflict", State.OWNERSHIP_CONFLICT),
        ("compete", State.OWNERSHIP_CONFLICT),
        ("partial", State.TIMED_OUT),
    ],
)
def test_real_ovsdb_component_boundary(tmp_path, fault, expected):
    async def scenario():
        db = await SimDatabase().start()
        vault = SecretStore(tmp_path / "secrets")
        vault.write_simulated("test-key", "simulated-new-key")
        store = Store(tmp_path / "state")
        backend = OpenSyncBackend("pod-1", OvsSession(db.endpoint), vault)
        manager = SimManager(db.endpoint)
        try:
            await db.seed()
            await manager.start()
            engine = Engine(store, vault, {"pod-1": backend})
            intent = Intent("pod-1", "radio-1", "bss-1", "new-ssid", "test-key")
            op = engine.request(
                intent,
                source="component-test",
                key="one",
                run_id="ovsdb-run",
                deadline=0.15 if fault in {"withhold", "partial"} else 10,
            )
            before = await backend.session.snapshot()
            vif_id, initial = next(iter(before["tables"]["Wifi_VIF_Config"].items()))
            backend.drop_next_reply = fault == "lost-reply"
            if fault == "guard-conflict":
                backend.before_transaction = lambda: manager.command("compete")
            result = await engine.execute(op.operation_id)
            assert result.state != State.OBSERVED_APPLIED
            if fault == "lost-reply":
                assert result.state == State.INDETERMINATE
            if fault in {"none", "lost-reply"}:
                await manager.command("apply")
            elif fault == "compete":
                await manager.command("compete")
            elif fault == "partial":
                await manager.command("partial")

            async def refresh():
                await engine.reconcile("pod-1")
                return store.get(op.operation_id)

            final = await eventually(refresh, lambda p: p.state == expected)
            assert final.state == expected
            if fault == "lost-reply":
                assert final.commit_evidence["attribution"] == "unknown"
                assert final.application_evidence["attribution"] == "current_condition_only"
                assert backend.write_count == 1
            after = await backend.session.snapshot()
            current = after["tables"]["Wifi_VIF_Config"][vif_id]
            assert current["bridge"] == initial["bridge"]
            assert current["wpa_oftags"] == initial["wpa_oftags"]
            assert dict(current["wpa_psks"][1])["guest"] == "preserved-guest-key"
            if fault == "guard-conflict":
                assert dict(current["wpa_psks"][1])["key"] == "initial-simulation-key"
            assert "simulated-new-key" not in str([p.to_dict() for p in store.operations()])
            assert "simulated-new-key" not in str(store.events("ovsdb-run"))
        finally:
            await manager.close()
            await backend.close()
            store.close()
            await db.close()

    asyncio.run(scenario())


def test_monitor_partial_delete_references_and_reconnect(tmp_path):
    async def scenario():
        db = await SimDatabase().start()
        session, writer = OvsSession(db.endpoint), OvsSession(db.endpoint)
        try:
            await db.seed()
            first = await session.snapshot()
            vif_id, initial = next(iter(first["tables"]["Wifi_VIF_Config"].items()))
            await writer.transact(
                [
                    {
                        "op": "update",
                        "table": "Wifi_VIF_Config",
                        "where": where_uuid(vif_id),
                        "row": {"ssid": "changed"},
                    }
                ]
            )
            updated = await eventually(
                session.snapshot,
                lambda x: x["tables"]["Wifi_VIF_Config"][vif_id]["ssid"] == "changed",
            )
            assert updated["tables"]["Wifi_VIF_Config"][vif_id]["bridge"] == initial["bridge"]
            state_id = next(iter(updated["tables"]["Wifi_VIF_State"]))
            await writer.transact(
                [{"op": "delete", "table": "Wifi_VIF_State", "where": where_uuid(state_id)}]
            )
            await eventually(
                session.snapshot, lambda x: state_id not in x["tables"]["Wifi_VIF_State"]
            )
            await db.stop()
            with pytest.raises((EmosaError, ConnectionError, TimeoutError)):
                await session.snapshot()
            assert not session.ready
            await db.start()
            recovered = await session.snapshot()
            assert recovered["generation"] > first["generation"]
            assert recovered["tables"]["Wifi_VIF_Config"][vif_id]["ssid"] == "changed"
        finally:
            await session.close()
            await writer.close()
            await db.close()

    asyncio.run(scenario())


def test_pod_initiated_listening_manager(tmp_path):
    async def scenario():
        # A pod database initiates TCP; the Python listener remains its management client.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        db = await SimDatabase().start()
        admin = OvsSession(db.endpoint)
        listener = OvsSession(f"ptcp:{port}:127.0.0.1", timeout=8)
        try:
            await db.seed()
            connecting = asyncio.create_task(listener.snapshot())
            # ovsdb-server unixctl uses upstream JSON-RPC, independent of the pod DB session.
            import ovs.jsonrpc
            import ovs.stream

            def set_remote():
                error, stream = ovs.stream.Stream.open_block(
                    ovs.stream.Stream.open("unix:" + str(db.directory / "control.sock")), 5000
                )
                assert error == 0
                rpc = ovs.jsonrpc.Connection(stream)
                try:
                    error, reply = rpc.transact_block(
                        ovs.jsonrpc.Message.create_request(
                            "ovsdb-server/add-remote", [f"tcp:127.0.0.1:{port}"]
                        )
                    )
                    assert not error and reply.error is None
                finally:
                    rpc.close()

            await asyncio.to_thread(set_remote)
            first = await connecting
            assert first["ready"] and first["tables"]["Wifi_VIF_Config"]
            rows = await listener.transact(
                [{"op": "select", "table": "Wifi_VIF_Config", "where": [], "columns": ["if_name"]}]
            )
            assert rows == [{"rows": [{"if_name": "lab-ap"}]}]
        finally:
            await admin.close()
            await listener.close()
            await db.close()

    asyncio.run(scenario())


def test_four_independent_ovsdb_pods(tmp_path):
    async def one(number):
        db = await SimDatabase().start()
        session = OvsSession(db.endpoint)
        try:
            await db.seed()
            first = await session.snapshot()
            vif_id = next(iter(first["tables"]["Wifi_VIF_Config"]))
            await session.transact(
                [
                    {
                        "op": "update",
                        "table": "Wifi_VIF_Config",
                        "where": where_uuid(vif_id),
                        "row": {"ssid": f"pod-{number}"},
                    }
                ]
            )
            snap = await eventually(
                session.snapshot,
                lambda s: s["tables"]["Wifi_VIF_Config"][vif_id]["ssid"] == f"pod-{number}",
            )
            return snap["tables"]["Wifi_VIF_Config"][vif_id]["ssid"]
        finally:
            await session.close()
            await db.close()

    async def scenario():
        assert await asyncio.gather(*(one(i) for i in range(4))) == [f"pod-{i}" for i in range(4)]

    asyncio.run(scenario())


@pytest.mark.unit
def test_transaction_result_validation():
    for results in ([{}], [{"count": 0}], [None], [], [{"error": "timed out"}]):
        with pytest.raises(EmosaError):
            check_results(results, [1])
    check_results([{}, {"count": 1}], [None, 1])


def test_snapshot_budget_exhaustion_is_not_ready(tmp_path):
    async def scenario():
        db = await SimDatabase().start()
        session = OvsSession(db.endpoint, row_limit=1)
        try:
            await db.seed()
            with pytest.raises(EmosaError) as exc:
                await session.snapshot()
            assert exc.value.code == Reason.NOT_READY
            assert not session.ready
        finally:
            await session.close()
            await db.close()

    asyncio.run(scenario())
