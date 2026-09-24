import asyncio
import json

import pytest

from emosa_lab.simulation.connecting_fleet import run

pytestmark = pytest.mark.ovsdb


def test_two_pods_one_service_isolation_and_crash_recovery(tmp_path):
    report = asyncio.run(run(tmp_path / "fleet"))
    assert report["passed"]
    stages = report["stages"]
    assert stages["separate_database_keys_verified"]
    assert stages["withheld"]["operation_id"] == stages["recovered_operation"]["operation_id"]
    assert len(stages["recovered_operation"]["attempts"]) == 1
    assert stages["wrong_identity_operation"]["attempts"] == []
    histories = stages["history"]
    # Histories are scoped to run IDs and retain distinct operation IDs after SIGKILL.
    operation_sets = [
        {event["operation_id"] for event in h["events"] if event["operation_id"]} for h in histories
    ]
    assert all(operation_sets) and operation_sets[0].isdisjoint(operation_sets[1])
    crashes = [x for x in report["service_lifecycle"] if x.get("requested_crash")]
    assert len(crashes) == 1 and crashes[0]["returncode"] == -9
    assert report["controller_onboarding_proven"] is False
    assert not (tmp_path / "fleet/control.sock").exists()
    serialized = json.dumps(report)
    for path in (tmp_path / "fleet/secrets").iterdir():
        if path.is_file() and path.name.startswith("pod-"):
            assert path.read_text().strip() not in serialized
