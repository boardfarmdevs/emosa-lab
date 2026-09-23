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


@pytest.mark.parametrize(
    "case", ["bounded", "no_observation", "fresh_lease", "clock_jump", "operation_changed"]
)
def test_discovery_loss_exemption_requires_observed_loss_and_whole_response_budget(case):
    check = runpy.run_path("scripts/check-native-peer-metrics.py")["queries_during_discovery_loss"]
    payload = {"running": True, "errors": [], "heartbeat_ns": 1_000_000_000}
    sample = {"started_ns": 3_000_000_000, "neighbor_observation": payload}
    absent = {"available": False}
    gap = {
        "started_at": 1.1,
        "restored_at": 6,
        "session_before": {"observed_neighbor": {"available": True}},
        "session_after": {"observed_neighbor": {"available": True}},
        "operation_before": {"count": 1},
        "operation_after": {"count": 1},
        "session_withheld": {
            "observed_neighbor": absent,
            "peer_link_metrics": absent,
            "forwarding_observation": {"sample": sample},
        },
    }
    observations = [
        {
            "observed_ns": 4_000_000_000,
            "observed_neighbor": absent,
            "peer_link_metrics": absent,
            "sample": sample,
        }
    ]
    offsets = [0, 0]
    queries = [{"frame": i, "time": t} for i, t in enumerate((3.9, 4.01, 5.5, 5.9999, 7))]
    if case == "no_observation":
        observations = []
    elif case == "fresh_lease":
        payload["heartbeat_ns"] = 2_000_000_000
    elif case == "clock_jump":
        offsets[1] = 1_000_001
    elif case == "operation_changed":
        gap["operation_after"]["count"] = 2
    if case == "bounded":
        assert check(queries, gap, observations, offsets, 6, 8) == [1, 2]
        # A source could recover before the deadline: the query is still owed.
        assert check(queries, gap, observations, offsets, 6, 6.4) == [1]
        # Resuming the collector is earlier than receiving a complete fresh
        # baseline. The latter's actual read boundary still binds the deadline.
        assert check([{"frame": 7, "time": 6.2}], gap, observations, offsets, 6.8, 8) == [7]
    else:
        with pytest.raises(AssertionError):
            check(queries, gap, observations, offsets, 6, 8)
