import asyncio
import json

import pytest

from emosa.easymesh_payloads import decode_value
from emosa_lab.simulation.topology import run

pytestmark = pytest.mark.ovsdb


def test_complete_topology_two_pods_reconnect_uuid_recreation_and_service_crash(tmp_path):
    result = asyncio.run(run(tmp_path / "topology"))
    assert result["passed"]
    stages = result["stages"]
    assert stages["uuid_recreation"] == {"rows_recreated": 12, "all_uuids_changed": True}
    assert stages["unexpected_interface"]["blockers"] == ["vif_inventory_differs_from_binding"]
    for pod in stages["connected"]:
        payload = decode_value(0x83, bytes.fromhex(pod["operational_bss_value"]["value_hex"]))
        assert [len(r.bsses) for r in payload.radios] == [2, 1]
        assert {b.ssid for r in payload.radios for b in r.bsses} == {
            b"initial-network",
            b"simulation-guest",
            b"simulation-iot",
        }
    assert stages["connected"][0]["binding_sha256"] != stages["connected"][1]["binding_sha256"]
    assert (
        stages["connected"][0]["operational_bss_value"]
        != stages["connected"][1]["operational_bss_value"]
    )
    assert not result["controller_onboarding_proven"] and not result["physical_pod_proven"]
    assert not (tmp_path / "topology/control.sock").exists()
    assert all(
        key not in json.dumps(result) for key in ("initial-simulation-key", "preserved-guest-key")
    )
    assert json.loads((tmp_path / "topology/adapter.json").read_text())["write_mode"] == "read-only"
