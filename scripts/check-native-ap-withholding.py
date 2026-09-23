"""Independent native AP reporting schedule/withholding regression, not delivery.

The real controller keeps every inclusion flag enabled. No qualified AP source
is installed in this experiment: any AP report or successful-send count fails.
"""

import argparse
import json
import runpy
from pathlib import Path

POLICY = runpy.run_path(str(Path(__file__).with_name("check-native-policy.py")))["check"]


def check(directory):
    policy = POLICY(directory)

    def read(name):
        return json.loads((directory / name).read_text())

    sessions = [row["session"] for row in read("active-samples.json")]
    for fault in read("recovery-checks.json"):
        sessions.extend(fault[name] for name in ("session_before", "session_after"))
    sessions.append(read("native-session.json"))
    periods = set()
    origins = []
    for session in sessions:
        assert session["state"] == "provisioning"
        assert session["ap_metrics"] == {"counts": {}, "measurement_available": False}
        saved = session["reporting_policy"]["received_policy"]
        assert saved.get("reports_transmitted", 0) == 0
        count = saved["periods_due_without_report"]
        origins.append(saved["next_due"] - count * 60)
        if count:
            assert saved["latest_report_attempt"] == {
                "due": saved["last_unfulfilled_due"],
                "status": "unavailable_or_send_incomplete",
                "reason": "NOT_READY",
            }
            assert saved["next_due"] - saved["last_unfulfilled_due"] == 60
            periods.add(count)
    assert max(origins) - min(origins) < 1e-6
    assert {1, 2, 3} <= periods
    return {
        "native_ap_withholding_checks_passed": True,
        "policy_receipt": policy,
        "checked_session_snapshots": len(sessions),
        "observed_unfulfilled_period_counts": sorted(periods),
        "original_schedule_preserved": True,
        "all_requested_inclusion_flags_retained": True,
        "qualified_ap_measurements": False,
        "native_ap_report_delivery_proven": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), indent=2))
