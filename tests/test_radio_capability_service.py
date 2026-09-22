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
