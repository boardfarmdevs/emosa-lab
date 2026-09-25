import asyncio
from contextlib import asynccontextmanager

import pytest

from emosa.errors import EmosaError, Reason
from emosa.model import Intent
from emosa.opensync.mapping import OpenSyncBackend
from emosa.opensync.session import OvsSession
from emosa.secrets import SecretStore
from emosa_lab.simulation.connecting_pod import SERIAL
from emosa_lab.simulation.database import SimDatabase, SimManager

pytestmark = pytest.mark.ovsdb


@asynccontextmanager
async def fixture(tmp_path, *, sole_radio=True):
    database = await SimDatabase().start()
    vault = SecretStore(tmp_path / "secrets")
    vault.write_simulated("new-key", "OnlySyntheticNewKey2026!")
    backend = OpenSyncBackend(
        "pod-1",
        OvsSession(database.endpoint),
        vault,
        expected_serial=SERIAL,
        mapping_scope="sole-fronthaul-radio",
    )
    admin = OvsSession(database.endpoint)
    manager = SimManager(database.endpoint)
    try:
        await database.seed(serial_number=SERIAL, sole_radio=sole_radio)
        await manager.start()
        yield backend, admin, manager
    finally:
        await manager.close()
        await backend.close()
        await admin.close()
        await database.close()


def intent():
    return Intent("pod-1", "radio-1", "bss-1", "scope-must-be-proven", "new-key")


async def mutate(admin, fault):
    snap = await admin.snapshot()
    tables = snap["tables"]
    vif_id, vif = next(iter(tables["Wifi_VIF_Config"].items()))
    state_id, state = next(iter(tables["Wifi_VIF_State"].items()))
    radio_id = next(iter(tables["Wifi_Radio_Config"]))
    if fault == "backhaul-role":
        ops = [
            {
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": [],
                "row": {"multi_ap": "backhaul_bss"},
            }
        ]
    elif fault == "extra-state-key":
        ops = [
            {
                "op": "mutate",
                "table": "Wifi_VIF_State",
                "where": [],
                "mutations": [
                    ["wpa_psks", "insert", ["map", [["other", "UnapprovedSecret2026!"]]]]
                ],
            }
        ]
    elif fault == "orphan-state":
        ops = [
            {
                "op": "insert",
                "table": "Wifi_VIF_State",
                "row": {**state, "if_name": "orphan-observed-bss"},
            }
        ]
    elif fault == "shared-config":
        ops = [
            {
                "op": "insert",
                "table": "Wifi_Radio_Config",
                "row": {
                    "if_name": "other-radio",
                    "enabled": True,
                    "vif_configs": ["set", [["uuid", vif_id]]],
                    "freq_band": "5G",
                },
            }
        ]
    elif fault in {"extra-bss", "orphan-config"}:
        ops = [
            {
                "op": "insert",
                "table": "Wifi_VIF_Config",
                "uuid-name": "extra",
                "row": {**vif, "if_name": "extra-bss"},
            }
        ]
        if fault == "extra-bss":
            ops.append(
                {
                    "op": "mutate",
                    "table": "Wifi_Radio_Config",
                    "where": [],
                    "mutations": [["vif_configs", "insert", ["set", [["named-uuid", "extra"]]]]],
                }
            )
    elif fault == "replace-equal-row":
        ops = [
            {"op": "delete", "table": "Wifi_VIF_Config", "where": []},
            {"op": "insert", "table": "Wifi_VIF_Config", "uuid-name": "newvif", "row": vif},
            {
                "op": "update",
                "table": "Wifi_Radio_Config",
                "where": [["_uuid", "==", ["uuid", radio_id]]],
                "row": {"vif_configs": ["set", [["named-uuid", "newvif"]]]},
            },
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", state_id]]],
                "row": {"vif_config": ["named-uuid", "newvif"]},
            },
        ]
    else:
        raise AssertionError("unknown test fault")
    results = await admin.transact(ops)
    assert all(isinstance(row, dict) and "error" not in row for row in results)


def test_sole_radio_allows_guarded_change_and_observed_application(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (backend, admin, manager):
            report = await backend.radio_scope()
            assert report["topology_candidate"] and report["synthetic_mapping_candidate"]
            assert not report["physical_qualification"] and not report["wire_admission"]
            snap = await backend.snapshot()
            result = await backend.submit(
                intent(), {"session_generation": snap.generation, "transaction_id": "sole-radio"}
            )
            assert result.status == "committed"
            await manager.command("apply")
            observed = await backend.snapshot()
            assert observed.observed.values["ssid"] == intent().ssid
            assert not (await backend.radio_scope())["blockers"]
            assert "OnlySyntheticNewKey2026!" not in str(await backend.radio_scope())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "extra-bss",
        "orphan-config",
        "orphan-state",
        "shared-config",
        "backhaul-role",
        "extra-state-key",
    ],
)
def test_unqualified_scope_rejected_before_submission(tmp_path, fault):
    async def scenario():
        async with fixture(tmp_path) as (backend, admin, _):
            await mutate(admin, fault)
            report = await backend.radio_scope()
            assert report["blockers"] and not report["synthetic_mapping_candidate"]
            with pytest.raises(EmosaError) as error:
                await backend.plan(intent())
            assert error.value.code == Reason.PRECONDITION_FAILED
            assert backend.write_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "extra-bss",
        "orphan-config",
        "orphan-state",
        "shared-config",
        "backhaul-role",
        "extra-state-key",
        "replace-equal-row",
    ],
)
def test_scope_race_aborts_entire_transaction(tmp_path, fault):
    async def scenario():
        async with fixture(tmp_path) as (backend, admin, _):
            initial = await backend.snapshot()
            backend.before_transaction = lambda: mutate(admin, fault)
            result = await backend.submit(
                intent(), {"session_generation": initial.generation, "transaction_id": "scope-race"}
            )
            assert result.reason == Reason.PRECONDITION_FAILED
            tables = (await admin.snapshot())["tables"]
            for row in tables["Wifi_VIF_Config"].values():
                assert row["ssid"] == "initial-network"
                assert dict(row["wpa_psks"][1])["key"] == "initial-simulation-key"

    asyncio.run(scenario())


def test_legacy_multi_key_fixture_does_not_qualify_as_sole_credential(tmp_path):
    async def scenario():
        async with fixture(tmp_path, sole_radio=False) as (backend, _, __):
            report = await backend.radio_scope()
            assert "credential_layout_not_single_wpa2_psk" in report["blockers"]
            assert "ordinary_ap_role_not_explicit" in report["blockers"]
            assert "preserved-guest-key" not in str(report)
            with pytest.raises(EmosaError):
                await backend.plan(intent())

    asyncio.run(scenario())


def test_separate_radio_is_allowed_and_untouched(tmp_path):
    async def scenario():
        async with fixture(tmp_path) as (backend, admin, _):
            tables = (await admin.snapshot())["tables"]
            vif = next(iter(tables["Wifi_VIF_Config"].values()))
            state = next(iter(tables["Wifi_VIF_State"].values()))
            results = await admin.transact(
                [
                    {
                        "op": "insert",
                        "table": "Wifi_VIF_Config",
                        "uuid-name": "other_vif",
                        "row": {**vif, "if_name": "other-ap", "ssid": "other-radio-unchanged"},
                    },
                    {
                        "op": "insert",
                        "table": "Wifi_Radio_Config",
                        "uuid-name": "other_radio",
                        "row": {
                            "if_name": "other-radio",
                            "freq_band": "5G",
                            "enabled": True,
                            "vif_configs": ["set", [["named-uuid", "other_vif"]]],
                        },
                    },
                    {
                        "op": "insert",
                        "table": "Wifi_VIF_State",
                        "uuid-name": "other_state",
                        "row": {
                            **state,
                            "if_name": "other-ap",
                            "ssid": "other-radio-unchanged",
                            "vif_config": ["named-uuid", "other_vif"],
                        },
                    },
                    {
                        "op": "insert",
                        "table": "Wifi_Radio_State",
                        "row": {
                            "if_name": "other-radio",
                            "freq_band": "5G",
                            "radio_config": ["named-uuid", "other_radio"],
                            "vif_states": ["set", [["named-uuid", "other_state"]]],
                        },
                    },
                ]
            )
            assert all("error" not in r for r in results)
            assert (await backend.radio_scope())["synthetic_mapping_candidate"]
            snapshot = await backend.snapshot()
            result = await backend.submit(
                intent(), {"session_generation": snapshot.generation, "transaction_id": "two-radio"}
            )
            assert result.status == "committed"
            rows = (await admin.snapshot())["tables"]["Wifi_VIF_Config"]
            assert next(r for r in rows.values() if r["if_name"] == "other-ap") == {
                **vif,
                "if_name": "other-ap",
                "ssid": "other-radio-unchanged",
            }

    asyncio.run(scenario())
