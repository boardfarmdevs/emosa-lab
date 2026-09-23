"""Independent common-window receive/transmit accounting and ingress-loss audit.

No EMOSA imports. Complete link metrics and physical-pod qualification are not
established by selected software-path loss measurements.
"""

import argparse
import json
import runpy
import struct
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = runpy.run_path(str(ROOT / "scripts/check-backhaul-accounting.py"))


def read(path):
    return json.loads(path.read_text())


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def cooked_frames(path, ifindex):
    """SLL2 records retain packet direction; restore the observed Ethernet size."""
    result = []
    for at, frame in AUDIT["packets"](path, 276):
        assert len(frame) >= 20
        protocol, reserved, index, hardware, kind, length = struct.unpack_from("!HHIHBB", frame)
        assert reserved == 0 and index == ifindex and hardware == 1 and length == 6
        assert kind in (0, 1, 2, 3, 4) and protocol in (0x800, 0x806, 0x86DD, 0x893A, 0x88CC)
        # VM peer outgoing is pod incoming. SLL2 replaces a 14-byte Ethernet
        # header with its 20-byte header. VLAN/other media are not admitted.
        result.append((at, "rx" if kind == 4 else "tx", len(frame) - 20 + 14))
    return result


def counters(payload):
    observation = payload["observation"]
    (link,) = observation["link"]
    result = {}
    for direction, hook, drop_name in (("tx", "egress", "driver"), ("rx", "ingress", "interface")):
        raw = link["stats64"][direction]
        assert raw["errors"] == 0
        result.update({direction + "_" + key: raw[key] for key in ("packets", "bytes")})
        result[direction + "_" + drop_name + "_drops"] = raw["dropped"]
        actions = [
            a for f in observation["filters"][hook] for a in f.get("options", {}).get("actions", [])
        ]
        assert len(actions) <= 1
        result[direction + "_action_drops"] = 0
        if actions:
            (action,) = actions
            assert action["kind"] == "gact" and action["control_action"]["type"] == "drop"
            assert action["stats"]["packets"] == action["stats"]["drops"]
            result[direction + "_action_drops"] = action["stats"]["drops"]
    return result


def audit_rows(rows, origins):
    previous, previous_windows = {}, {}
    windows, generations, workers = [], set(), set()
    losses = {"transmit_losses": 0, "receive_losses": 0}
    baselines = duplicates = 0
    for worker, status, payload in rows:
        workers.add(worker)
        assert not status["measurement_source_qualified"] and not status["capacity_qualified"]
        sample, window = status["sample"], status["window"]
        if sample is None:
            assert window is None and not status["available"]
            previous.pop(worker, None)
            continue
        origin = origins[sample["started_ns"]]
        assert payload == origin and origin["running"] and not origin["errors"]
        assert sample["counters"] == counters(origin)
        assert sample["local_interface"] == origin["mac"] and sample["ifindex"] == origin["ifindex"]
        assert sample["epoch"][1:7] == [
            origin[k]
            for k in (
                "collector_epoch",
                "configuration_epoch",
                "boot_id",
                "netns_inode",
                "ifindex",
                "mac",
            )
        ]
        generations.add((worker, sample["epoch"][0]))
        old = previous.get(worker)
        if sample == old:
            assert window == previous_windows[worker]
            duplicates += 1
            continue
        if window is None:
            baselines += 1
        else:
            assert status["available"] and old is not None and old["epoch"] == sample["epoch"]
            assert window["first_read_ns"] == [old["started_ns"], old["ended_ns"]]
            assert window["last_read_ns"] == [sample["started_ns"], sample["ended_ns"]]
            assert old["ended_ns"] < sample["started_ns"] < old["started_ns"] + 2_000_000_000
            delta = {k: v - old["counters"][k] for k, v in sample["counters"].items()}
            assert min(delta.values()) >= 0 and window["deltas"] == delta
            assert window["transmit_losses"] == delta["tx_driver_drops"] + delta["tx_action_drops"]
            assert (
                window["receive_losses"] == delta["rx_interface_drops"] + delta["rx_action_drops"]
            )
            for key in losses:
                losses[key] += window[key]
            windows.append(window)
        previous[worker], previous_windows[worker] = sample, window
    return {
        "checked_common_windows": len(windows),
        "baseline_samples": baselines,
        "connection_generations": len(generations),
        "worker_processes": len(workers),
        "duplicate_observations": duplicates,
        **{"summed_nonoverlapping_" + k: v for k, v in losses.items()},
    }, windows


def check(directory, native=False):
    raw = lines(directory / "egress-observations.jsonl")
    final = read(directory / "egress-observer-final.json")
    assert raw[-1] == final and not final["running"] and not final["errors"]
    assert all(not v["errors"] and v["collector_epoch"] == final["collector_epoch"] for v in raw)
    epochs = [v["configuration_epoch"] for v in raw]
    assert epochs == sorted(epochs)
    origins = {v["observation"]["started_ns"]: v for v in raw if v["observation"]}
    runtime = read(directory / "result.json")
    if native:
        handoff = runpy.run_path(str(ROOT / "scripts/check-egress-accounting.py"))["check"](
            directory, True
        )
        values = lines(directory / "forwarding-samples.jsonl")
        rows = [
            (
                v["worker_pid"],
                v["backhaul_accounting"],
                v["sample"]["egress_observation"] if v["sample"] else None,
            )
            for v in values
        ]
        result, _ = audit_rows(rows, origins)
        assert result["checked_common_windows"] >= 90 and result["connection_generations"] == 3
        assert (
            result["summed_nonoverlapping_transmit_losses"]
            == result["summed_nonoverlapping_receive_losses"]
            == 0
        )
        gap = read(directory / "telemetry-gap-check.json")
        assert all(
            gap[p]["backhaul_accounting"]["available"]
            for p in ("session_before", "session_withheld", "session_after")
        )
        lost = read(directory / "recovery-checks.json")[0]["session_unavailable"][
            "backhaul_accounting"
        ]
        assert not lost["available"] and lost["sample"] is None
        result["native_ovsdb_handoff_checks_passed"] = handoff["native_ovsdb_handoff_checks_passed"]
        result["raw_forwarding_packet_reconciliation"] = {
            k: v for k, v in AUDIT["check_native"](directory).items() if k != "window_bounds"
        }
    else:
        assert (
            runtime["status"] == "observed_pending_independent_review"
            and runtime["loss_direction"] == "ingress"
        )
        assert runtime["traffic_control_restored"] and not runtime["cleanup_errors"]
        assert final["collector_sha256"] == runtime["egress_collector_sha256"]
        health = runpy.run_path(str(ROOT / "scripts/check-capture-health.py"))["check_file"]
        captures = {
            name: health(directory / (name + ".pcap"), directory / (name + "-capture.log"), kind)
            for name, kind in (("ingress", 1), ("backhaul", 1), ("receive", 276))
        }
        client, backhaul = (
            AUDIT["echo_inventory"](directory / (n + ".pcap")) for n in ("ingress", "backhaul")
        )
        expected_client, expected_backhaul = Counter(), Counter()
        for identifier in (31001, 31002, 31003):
            for seq in range(1, 18):
                expected_client[(identifier, 8, seq)] += 1
                if identifier != 31002:
                    expected_client[(identifier, 0, seq)] += 1
                for kind in (0, 8):
                    expected_backhaul[(identifier, kind, seq)] += 1
        assert client == expected_client and backhaul == expected_backhaul
        by_heartbeat = {v["heartbeat_ns"]: v for v in raw}
        projection = read(directory / "backhaul-projection.json")
        rows = [(1, v, by_heartbeat[v["heartbeat_ns"]]) for v in projection["observations"]]
        result, windows = audit_rows(rows, origins)
        assert result["summed_nonoverlapping_transmit_losses"] == 0
        assert result["summed_nonoverlapping_receive_losses"] == 17
        (drop,) = [p for p in runtime["phases"] if p["phase"] == "drop"]
        a, b = (
            next(v for v in drop[s]["links"] if v["ifname"] == "eth1")["stats64"]["rx"]
            for s in ("before", "after")
        )
        assert b["errors"] == a["errors"] and b["dropped"] == a["dropped"]
        (rule,) = [r for r in drop["after"]["filters"] if "options" in r]
        config = rule["options"]
        assert config["keys"]["src_ip"] == "192.0.2.1" and config["keys"]["dst_ip"] == "192.0.2.20"
        (action,) = config["actions"]
        assert action["stats"]["drops"] == action["stats"]["packets"] == 17
        # A capture of all frames retains unrelated ARP instead of attributing
        # extra interface counts to the selected dropped ICMP flow.
        frames = cooked_frames(directory / "receive.pcap", runtime["backhaul_peer"]["ifindex"])
        cuts = [p[s] for p in runtime["phases"] for s in ("before", "after")]
        offsets = [v["wall_ns"] - v["monotonic_ns"] for v in cuts]
        bounds = []
        for w in windows:
            if (
                not cuts[0]["monotonic_ns"]
                < w["first_read_ns"][0]
                < w["last_read_ns"][1]
                < cuts[-1]["monotonic_ns"]
            ):
                continue
            bounds.append(
                AUDIT["reconcile"](
                    frames, w["first_read_ns"], w["last_read_ns"], offsets, w["deltas"]
                )
            )
        assert len(bounds) >= 6
        result.update(
            controlled_ingress_loss_checks_passed=True,
            tc_action_drops=17,
            ordinary_rx_drop_delta=0,
            capture_health=captures,
            packet_byte_window_bounds=bounds,
            clock_offset_spread_ns=max(offsets) - min(offsets),
            fixed_timing_allowance_ns=AUDIT["TIMING_ALLOWANCE_NS"],
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
