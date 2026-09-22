import asyncio
import json

import pytest

from emosa.easymesh_payloads import decode_value
from emosa.simulation.radio_capabilities import run

pytestmark = pytest.mark.ovsdb


def test_two_pod_service_revalidates_capability_inputs_and_reconnects(tmp_path):
    result = asyncio.run(run(tmp_path / "radio-capabilities"))
    assert result["passed"]
    assert result["observed_tables_unchanged_after_restoring_fixture_faults"]
    for pod in result["stages"]["connected"]:
        values = [decode_value(0x85, bytes.fromhex(r["value"]["value_hex"])) for r in pod["radios"]]
        assert [v.max_bss for v in values] == [4, 2]
        assert [[o.operating_class for o in v.operating_classes] for v in values] == [
            [115],
            [81, 83, 84],
        ]
    serialized = json.dumps(result)
    assert "initial-simulation-key" not in serialized and str(tmp_path) not in serialized
    assert not result["controller_onboarding_proven"] and not result["physical_pod_proven"]
    assert not (tmp_path / "radio-capabilities/control.sock").exists()


def test_two_pod_technology_and_inventory_survive_reconnect_and_process_restart(tmp_path):
    result = asyncio.run(run(tmp_path / "extensions", with_extensions=True))
    assert result["passed"] and result["observed_tables_unchanged_after_restoring_fixture_faults"]
    for pod in result["stages"]["connected"]:
        ext = pod["extensions"]
        assert [v["type"] for v in ext["technology"]["values"]] == ["0x86", "0x87", "0x86"]
        inventory = decode_value(
            0xD4, bytes.fromhex(ext["device_inventory"]["values"][0]["value_hex"])
        )
        assert len(inventory.radios) == 2 and inventory.serial_number
    for stage in ("reconnected", "restarted"):
        pod = result["stages"][stage]
        if isinstance(pod, list):
            pod = pod[0]
        assert pod["extensions"] == result["stages"]["connected"][0]["extensions"]


def test_two_pod_wifi6_roles_withdraw_and_recover_without_complete_he_report(tmp_path):
    result = asyncio.run(run(tmp_path / "wifi6", with_wifi6=True))
    assert result["passed"] and result["observed_tables_unchanged_after_restoring_fixture_faults"]
    assert result["selected_wifi6_inputs"]
    for pod in result["stages"]["connected"]:
        ext = pod["extensions"]
        assert pod["ready"] and ext["wifi6"]["ready"]
        assert not ext["technology"]["ready"] and not ext["technology"]["values"]
        values = [decode_value(0xAA, bytes.fromhex(v["value_hex"])) for v in ext["wifi6"]["values"]]
        assert [[r.role for r in v.roles] for v in values] == [[0], [0, 1]]
        assert not pod["complete_ap_capability_report"]
    first, second = result["stages"]["connected"]
    assert first["extensions"]["wifi6"]["values"] != second["extensions"]["wifi6"]["values"]
    for stage in ("reconnected", "restarted"):
        pod = result["stages"][stage]
        if isinstance(pod, list):
            pod = pod[0]
        assert pod["extensions"] == first["extensions"]
    assert not (tmp_path / "wifi6/control.sock").exists()
