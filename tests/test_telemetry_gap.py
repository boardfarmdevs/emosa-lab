"""The native freshness regression must reject stale facts and changed authority."""

import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "doc/evidence/telemetry-freshness/native-telemetry-gap-01"
audit = runpy.run_path(str(ROOT / "scripts/check-telemetry-gap.py"))
pytestmark = pytest.mark.unit


def test_retained_native_gap_withdraws_telemetry_without_false_discovery():
    result = audit["check"](EVIDENCE)
    assert result["telemetry_gap_checks_passed"]
    assert result["new_onboarding_operations"] == result["new_config_writes"] == 0
    assert not result["sustained_operation_proven"]


@pytest.mark.parametrize("change", ["stale_inventory", "lost_authority", "changed_operation"])
def test_gap_acceptance_rejects_the_original_failure_and_stale_data(tmp_path, change):
    for path in EVIDENCE.iterdir():
        if path.name != "telemetry-gap-check.json":
            (tmp_path / path.name).symlink_to(path)
    gap = json.loads((EVIDENCE / "telemetry-gap-check.json").read_text())
    if change == "stale_inventory":
        gap["session_withheld"]["report_source"]["inventory_complete"] = True
    elif change == "lost_authority":
        gap["session_withheld"]["report_source"]["available"] = False
    else:
        gap["operation_after"]["operation_count"] += 1
    (tmp_path / "telemetry-gap-check.json").write_text(json.dumps(gap))
    with pytest.raises(AssertionError):
        audit["check"](tmp_path)
