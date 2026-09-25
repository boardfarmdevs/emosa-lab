import asyncio

import pytest

from emosa_lab.simulation.reliability import run


@pytest.mark.ovsdb
def test_tls_fleet_recovers_without_losing_isolation_or_original_outcomes(tmp_path):
    report = asyncio.run(run(tmp_path / "fleet", pods=2, cycles=1))
    assert report["passed"] and report["cleanup_passed"]
    assert len(report["cycles"]) == 1
    assert len(report["checks"]) == 5
    assert report["operation_latency_seconds"]["samples"] >= 5
    assert report["resource_samples"]
    assert not report["controller_onboarding_proven"]
