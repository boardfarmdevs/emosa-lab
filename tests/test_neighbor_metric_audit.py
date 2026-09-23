import copy
import runpy

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("case", ["bounded", "clock_jump", "authority_live", "missing_loss"])
def test_metric_audit_only_accounts_queries_strictly_inside_observed_loss(case):
    check = runpy.run_path("scripts/check-neighbor-metrics.py")["queries_during_source_loss"]
    events = [
        {"received_wall_ns": 100_000_000_000, "received_monotonic_ns": 10_000_000_000},
        {"received_wall_ns": 101_000_000_000, "received_monotonic_ns": 11_000_000_000},
    ]
    lost = {
        "state": "recovering",
        "report_source": {"available": False},
        "recovery": {"history": [{"event": "source_lost", "at": 12}]},
    }
    fault = {
        "kind": "pod_connection_loss",
        "started_at": 101,
        "restored_at": 106,
        "recovered_at": 107,
        "session_unavailable": copy.deepcopy(lost),
    }
    queries = [{"frame": i, "time": t} for i, t in enumerate((101, 102.01, 104, 105.99, 107))]
    if case == "clock_jump":
        events[1]["received_wall_ns"] += 30_000_000
    elif case == "authority_live":
        fault["session_unavailable"]["report_source"]["available"] = True
    elif case == "missing_loss":
        fault["session_unavailable"]["recovery"]["history"] = []
    if case == "bounded":
        assert check(queries, [fault], events) == [2]
    else:
        with pytest.raises(AssertionError):
            check(queries, [fault], events)
