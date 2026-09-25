"""Fresh independent hostap M2 → component operation → real owned OVSDB."""

import asyncio
import json
from pathlib import Path

import pytest

from emosa.model import State
from emosa_lab.simulation.wsc_provisioning import CASES, run_case

pytestmark = pytest.mark.ovsdb


@pytest.mark.parametrize("case", CASES)
def test_authenticated_provisioning_with_independent_registrar(case):
    registrar = Path(__file__).resolve().parents[1] / ".cache/wsc-registrar/component-registrar"
    assert registrar.is_file(), "Run python3 scripts/build-wsc-registrar.py first"
    result = asyncio.run(run_case(registrar, case))
    op = result["operation"]
    assert result["passed"] and result["full_controller_onboarding"] is False
    assert op["initiating_interface"] == "wsc-component"
    assert op["attempts"] == 1
    assert op["state"] == (
        State.OWNERSHIP_CONFLICT if case == "identity-race" else State.OBSERVED_APPLIED
    )
    if case in {"lost-reply", "crash-after-commit"}:
        assert op["commit_attribution"] == "unknown"
        assert op["application_evidence"]["attribution"] == "current_condition_only"
    if case == "crash-after-commit":
        assert result["checks"]["writes_before_crash"] == 1
        assert result["checks"]["writes_after_restart"] == 0
        assert result["checks"]["old_m2_against_fresh_m1"] == "INVALID_INPUT"
    public = json.dumps(result)
    assert "OnlySimulationWscKey2026!" not in public
    assert "request_fingerprint" not in public
    assert "secret_ref" not in public
