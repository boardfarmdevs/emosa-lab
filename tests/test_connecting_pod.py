import asyncio

import pytest

from emosa.errors import EmosaError, Reason
from emosa.model import Intent
from emosa.opensync.mapping import OpenSyncBackend, check_results
from emosa.opensync.session import OvsSession
from emosa.secrets import SecretStore
from emosa_lab.simulation.connecting_pod import AL_MAC, SERIAL, poll, run
from emosa_lab.simulation.database import SimDatabase

pytestmark = pytest.mark.ovsdb


def test_connecting_pod_service_lifecycle(tmp_path):
    report = asyncio.run(run(tmp_path / "demo", automated=True))
    assert report["passed"]
    assert report["stages"]["before_connection"]["agents"][0]["inventory"] is None
    assert report["stages"]["configuration_applied"]["commit_evidence"]["attribution"] == "reply"
    assert report["stages"]["disconnected"]["agents"][0]["fresh"] is False
    assert report["stages"]["adapter_restarted"]["agents"][0]["al_mac"] == AL_MAC
    assert report["controller_onboarding_proven"] is False
    assert not (tmp_path / "demo/control.sock").exists()


@pytest.mark.parametrize("identity", ["missing", "wrong"])
def test_unmatched_identity_cannot_register_or_write(tmp_path, identity):
    async def scenario():
        db = await SimDatabase().start()
        vault = SecretStore(tmp_path / "secrets")
        vault.write_simulated("test-key", "simulation-key-only")
        backend = OpenSyncBackend("pod-1", OvsSession(db.endpoint), vault, expected_serial=SERIAL)
        admin = OvsSession(db.endpoint)
        try:
            await db.seed(serial_number=None if identity == "missing" else "wrong")
            with pytest.raises(EmosaError, match="identity"):
                await backend.inventory()
            with pytest.raises(EmosaError, match="identity"):
                await backend.plan(Intent("pod-1", "radio-1", "bss-1", "new", "test-key"))
            assert backend.write_count == 0
        finally:
            await backend.close()
            await admin.close()
            await db.close()

    asyncio.run(scenario())


def test_pinned_schema_rejects_duplicate_identity_rows():
    async def scenario():
        db = await SimDatabase().start()
        admin = OvsSession(db.endpoint)
        try:
            await db.seed(serial_number=SERIAL)
            result = await admin.transact(
                [{"op": "insert", "table": "AWLAN_Node", "row": {"serial_number": SERIAL}}]
            )
            assert any(row.get("error") == "constraint violation" for row in result)
            assert len((await admin.snapshot())["tables"]["AWLAN_Node"]) == 1
        finally:
            await admin.close()
            await db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["replace", "recreate"])
def test_identity_change_between_plan_and_commit_is_guarded(tmp_path, change):
    async def scenario():
        db = await SimDatabase().start()
        vault = SecretStore(tmp_path / "secrets")
        vault.write_simulated("test-key", "simulation-key-only")
        backend = OpenSyncBackend("pod-1", OvsSession(db.endpoint), vault, expected_serial=SERIAL)
        admin = OvsSession(db.endpoint)
        try:
            await db.seed(serial_number=SERIAL)
            initial = await backend.snapshot()
            assert initial.ready

            async def replace_identity():
                if change == "replace":
                    operations = [
                        {
                            "op": "update",
                            "table": "AWLAN_Node",
                            "where": [],
                            "row": {"serial_number": "replacement"},
                        }
                    ]
                else:
                    operations = [
                        {"op": "delete", "table": "AWLAN_Node", "where": []},
                        {
                            "op": "insert",
                            "table": "AWLAN_Node",
                            "row": {"serial_number": SERIAL},
                        },
                    ]
                results = await admin.transact(operations)
                check_results(results, [1] if change == "replace" else [1, None])

            backend.before_transaction = replace_identity
            result = await backend.submit(
                Intent("pod-1", "radio-1", "bss-1", "must-not-apply", "test-key"),
                {"session_generation": initial.generation, "transaction_id": "identity-race"},
            )
            assert result.reason == Reason.PRECONDITION_FAILED
            snap = await admin.snapshot()
            assert (
                next(iter(snap["tables"]["Wifi_VIF_Config"].values()))["ssid"] == "initial-network"
            )
            if change == "replace":
                stale = await poll(backend.snapshot, lambda s: not s.ready)
                assert not stale.observed.fresh
            else:
                # Recreating a row with the expected serial invalidates the pending
                # transaction, but a subsequent fresh read may qualify it again.
                refreshed = await backend.snapshot()
                assert refreshed.ready and refreshed.observed.fresh
        finally:
            await backend.close()
            await admin.close()
            await db.close()

    asyncio.run(scenario())
