"""Independent egress source audit; no EMOSA imports or inferred PHY values."""

import argparse
import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text())


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def source_counters(value):
    obs = value["observation"]
    (link,) = obs["link"]
    tx = link["stats64"]["tx"]
    actions = [a for f in obs["filters"]["egress"] for a in f.get("options", {}).get("actions", [])]
    assert len(actions) <= 1
    drops = 0
    if actions:
        (action,) = actions
        assert action["kind"] == "gact" and action["control_action"]["type"] == "drop"
        assert action["stats"]["drops"] == action["stats"]["packets"]
        drops = action["stats"]["drops"]
    return {
        "successful_packets": tx["packets"],
        "successful_bytes": tx["bytes"],
        "driver_drops": tx["dropped"],
        "action_drops": drops,
    }


def audit_rows(rows, origins):
    previous, previous_windows = {}, {}
    windows, baselines, generations, losses = 0, 0, set(), 0
    duplicates = 0
    workers = set()
    for worker, row, payload in rows:
        workers.add(worker)
        assert row["measurement_source_qualified"] is False
        sample, window = row["sample"], row["window"]
        if sample is None:
            assert window is None and row["available"] is False
            previous.pop(worker, None)
            continue
        origin = origins[sample["started_ns"]]
        assert payload == origin
        assert origin["running"] and not origin["errors"]
        assert sample["counters"] == source_counters(origin)
        assert sample["local_interface"] == origin["mac"]
        assert sample["ifindex"] == origin["ifindex"]
        epoch = sample["epoch"]
        assert epoch[1:7] == [
            origin["collector_epoch"],
            origin["configuration_epoch"],
            origin["boot_id"],
            origin["netns_inode"],
            origin["ifindex"],
            origin["mac"],
        ]
        generations.add((worker, epoch[0]))
        old = previous.get(worker)
        if old == sample:
            assert window == previous_windows[worker]
            duplicates += 1
            continue
        if window:
            assert row["available"] and old is not None and old["epoch"] == epoch
            assert window["first_read_ns"] == [old["started_ns"], old["ended_ns"]]
            assert window["last_read_ns"] == [sample["started_ns"], sample["ended_ns"]]
            assert old["ended_ns"] < sample["started_ns"] < old["started_ns"] + 2_000_000_000
            delta = {k: v - old["counters"][k] for k, v in sample["counters"].items()}
            assert delta == window["deltas"] and min(delta.values()) >= 0
            assert window["egress_losses"] == delta["driver_drops"] + delta["action_drops"]
            losses += window["egress_losses"]
            windows += 1
        else:
            baselines += 1
        previous[worker], previous_windows[worker] = sample, window
    return {
        "checked_windows": windows,
        "baseline_samples": baselines,
        "connection_generations": len(generations),
        "worker_processes": len(workers),
        "duplicate_observations": duplicates,
        "summed_nonoverlapping_egress_losses": losses,
    }


def check(directory, native=False):
    raw = lines(directory / "egress-observations.jsonl")
    final = read(directory / "egress-observer-final.json")
    assert final == raw[-1] and final["running"] is False and not final["errors"]
    assert all(not r["errors"] and r["collector_epoch"] == final["collector_epoch"] for r in raw)
    epochs = [v["configuration_epoch"] for v in raw]
    assert epochs == sorted(epochs)
    origins = {v["observation"]["started_ns"]: v for v in raw if v["observation"]}
    if native:
        binding = runpy.run_path(str(ROOT / "scripts/check-neighbor-binding.py"))["check"](
            directory
        )
        assert (
            final["collector_sha256"]
            == read(directory / "source-hashes.json")["/opt/emosa-radio-manager/egress-observer.py"]
        )
        rows = []
        publishers = {}
        for value in lines(directory / "manager.jsonl"):
            if value.get("publication") != "observed-state" or not value.get("forwarding", {}).get(
                "available"
            ):
                continue
            data = dict(value["forwarding"]["interfaces"]["eth1"]["external_ids"][1]).get(
                "egress_observation"
            )
            if data is not None:
                published = json.loads(data)
                publishers[published["heartbeat_ns"]] = published
        for value in lines(directory / "forwarding-samples.jsonl"):
            row = value["egress_accounting"]
            payload = value["sample"]["egress_observation"] if value["sample"] else None
            if payload:
                assert payload == publishers[payload["heartbeat_ns"]]
            if row["available"]:
                assert (
                    value["available"]
                    and value["observed_ns"] < row["sample"]["started_ns"] + 2_000_000_000
                )
            rows.append((value["worker_pid"], row, payload))
        result = audit_rows(rows, origins)
        assert result["checked_windows"] >= 90 and result["connection_generations"] == 3
        assert result["summed_nonoverlapping_egress_losses"] == 0
        for phase in ("session_before", "session_withheld", "session_after"):
            assert read(directory / "telemetry-gap-check.json")[phase]["egress_accounting"][
                "available"
            ]
        lost = read(directory / "recovery-checks.json")[0]["session_unavailable"]
        assert (
            not lost["egress_accounting"]["available"]
            and lost["egress_accounting"]["sample"] is None
        )
        result.update(
            native_ovsdb_handoff_checks_passed=True,
            independent_binding_checks_passed=binding["native_neighbor_binding_checks_passed"],
        )
    else:
        loss = runpy.run_path(str(ROOT / "scripts/check-backhaul-accounting.py"))["check_loss"](
            directory
        )
        runtime = read(directory / "result.json")
        assert final["collector_sha256"] == runtime["egress_collector_sha256"]
        replay = read(directory / "egress-projection.json")
        by_heartbeat = {v["heartbeat_ns"]: v for v in raw}
        rows = [(1, v, by_heartbeat[v["heartbeat_ns"]]) for v in replay["observations"]]
        result = audit_rows(rows, origins)
        assert result["summed_nonoverlapping_egress_losses"] == loss["tc_action_drops"] == 17
        assert len(set(epochs)) >= 3
        assert any(
            v["running"] and v["observation"] is None and v["configuration_epoch"] > 0 for v in raw
        )
        result.update(
            controlled_loss_source_checks_passed=True, capture_health=loss["capture_health"]
        )
    result.update(
        configuration_epochs=len(set(epochs)),
        measurement_source_qualified=False,
        native_neighbor_metric_delivery_proven=False,
        sustained_operation_proven=False,
        physical_pod_proven=False,
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--native", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.directory, args.native), indent=2))
