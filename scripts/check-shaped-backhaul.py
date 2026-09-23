"""Independent common service/loss audit for the owned shaped backhaul.

No EMOSA imports. Whole-interface reconciliation is not peer qualification or
proof of native metric delivery. All capture tolerances remain fixed at 1 ms.
"""

import argparse
import json
import runpy
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAP = runpy.run_path(str(ROOT / "scripts/check-virtual-capacity.py"))
AUDIT, RECEIVE, HEALTH = CAP["AUDIT"], CAP["RECEIVE"], CAP["HEALTH"]
read, lines = CAP["read"], CAP["lines"]


def queue_loss(directory, runtime, windows):
    assert runtime["queue_loss_requested"]
    receiver = read(directory / "receiver.json")
    assert not receiver["errors"] and receiver["stopped_by_signal"]
    assert receiver["label"] == runtime["label"] and receiver["nonce"] == runtime["nonce"]
    assert receiver["source_sha256"] == runtime["source_hashes"]["link-traffic.py"]
    captured = {n: CAP["capture"](directory, n) for n in ("offered", "backhaul")}
    inventories = {n: CAP["workload"](v[0], runtime["nonce"]) for n, v in captured.items()}
    phases, losses = [], 0
    for phase in runtime["phases"]:
        n, sender = phase["phase"], phase["sender"]
        assert not sender["errors"] and sender["source_sha256"] == receiver["source_sha256"]
        assert len(sender["sender_socket_buffers"]) == 8
        offered, delivered = (inventories[k][n] for k in ("offered", "backhaul"))
        assert set(offered) == set(range(sender["packets_sent"]))
        assert set(delivered) <= set(offered)
        assert all(offered[s][1] == delivered[s][1] for s in delivered)
        assert all(len(f) == sender["udp_payload_size"] + 42 for _, f in delivered.values())
        received = [(s, size) for p, s, size, _, _ in receiver["records"] if p == n]
        assert Counter(received) == Counter((s, sender["udp_payload_size"]) for s in delivered)
        before, after = phase["before"][0], phase["after"][0]
        dropped = len(set(offered) - set(delivered))
        assert after["drops"] - before["drops"] == dropped
        assert after["backlog"] == after["qlen"] == 0
        assert (dropped > 0) == (n == 2), "saturation must exercise actual queue loss"
        losses += dropped
        first = min(at for at, _ in delivered.values())
        middle = [
            f for at, f in delivered.values() if first + 1_000_000_000 <= at < first + 3_000_000_000
        ]
        measured = sum(CAP["charged_size"](len(f)) for f in middle) * 8 / 2 / 1_000_000
        expected = {1: 50, 2: 100, 3: 5}[n]
        assert expected * 0.99 <= measured <= expected * 1.01
        phases.append(
            {
                "phase": n,
                "offered": len(offered),
                "received": len(delivered),
                "queue_drops": dropped,
                "measured_service_mbps": measured,
            }
        )
    tx = {runtime["pod_backhaul"]["address"]}
    tx.update(f[6:12].hex(":") for _, f in inventories["backhaul"][1].values())
    # This fixture's controller bridge has the inventoried native AL address.
    # Reject all other sources instead of silently assigning them a direction.
    rx = {"02:00:00:e0:00:01"}
    frames = []
    for at, f in captured["backhaul"][0]:
        mac = f[6:12].hex(":")
        assert mac in tx | rx and not tx & rx
        frames.append((at, "tx" if mac in tx else "rx", len(f)))
    offsets = [p["wall_ns"] - p["started_ns"] for p in runtime["phases"]]
    return (
        frames,
        offsets,
        windows,
        {
            "queue_loss_checks_passed": True,
            "phases": phases,
            "expected_loss_components": {"queue_drops": losses},
            "capture_health": {n: v[1] for n, v in captured.items()},
        },
    )


def action_loss(directory, runtime, windows):
    assert runtime["virtual_link_requested"]
    direction = runtime["loss_direction"]
    assert direction in ("ingress", "egress")
    captures = {
        n: HEALTH(directory / (n + ".pcap"), directory / (n + "-capture.log"), kind)
        for n, kind in (("ingress", 1), ("backhaul", 1), ("receive", 276))
    }
    client, peer = (
        AUDIT["echo_inventory"](directory / (n + ".pcap")) for n in ("ingress", "backhaul")
    )
    expected_client, expected_peer = Counter(), Counter()
    for identifier in (31001, 31002, 31003):
        for seq in range(1, 18):
            expected_client[(identifier, 8, seq)] += 1
            if identifier != 31002:
                expected_client[(identifier, 0, seq)] += 1
            if identifier != 31002 or direction == "ingress":
                for kind in (0, 8):
                    expected_peer[(identifier, kind, seq)] += 1
    assert client == expected_client and peer == expected_peer
    assert [p["returncode"] for p in runtime["phases"]] == [0, 1, 0]
    frames = RECEIVE["cooked_frames"](
        directory / "receive.pcap", runtime["backhaul_peer"]["ifindex"]
    )
    cuts = [p[s] for p in runtime["phases"] for s in ("before", "after")]
    offsets = [v["wall_ns"] - v["monotonic_ns"] for v in cuts]
    selected = [
        w
        for w in windows
        if cuts[0]["monotonic_ns"]
        < w["first_read_ns"][0]
        < w["last_read_ns"][1]
        < cuts[-1]["monotonic_ns"]
    ]
    assert len(selected) >= 6
    return (
        frames,
        offsets,
        selected,
        {
            "controlled_action_loss_checks_passed": True,
            "direction": direction,
            "expected_loss_components": {
                "rx_action_drops" if direction == "ingress" else "tx_action_drops": 17
            },
            "capture_health": captures,
        },
    )


def check(directory, native=False):
    runtime = read(directory / "result.json")
    assert not runtime["cleanup_errors"]
    result, windows = CAP["rows"](directory, native, shaped=True)
    assert windows
    if native:
        service = CAP["check"](directory, True)
        assert result["connection_generations"] == 3 and result["worker_processes"] == 2
        assert result["service_windows"] >= 90
        gap = read(directory / "telemetry-gap-check.json")
        assert all(
            gap[p]["shaped_backhaul"]["available"]
            for p in ("session_before", "session_withheld", "session_after")
        )
        lost = read(directory / "recovery-checks.json")[0]["session_unavailable"]["shaped_backhaul"]
        assert not lost["available"] and lost["sample"] is None and lost["service_estimate"] is None
        base = AUDIT["check_native"](directory)
        raw, health = CAP["capture"](directory, "forwarding")
        tx, rx = set(base["tx_source_macs"]), set(base["rx_source_macs"])
        frames = [(at, "tx" if f[6:12].hex(":") in tx else "rx", len(f)) for at, f in raw]
        assert all(f[6:12].hex(":") in tx | rx for _, f in raw)
        offsets = [
            v["received_wall_ns"] - v["received_monotonic_ns"]
            for v in lines(directory / "station-events.jsonl")
        ]
        selected = windows
        detail = {
            "native_ovsdb_handoff_checks_passed": service["native_ovsdb_handoff_checks_passed"],
            "capture_health": {"forwarding": health},
            "expected_loss_components": {},
        }
    else:
        assert runtime["status"] == "observed_pending_independent_review"
        assert runtime["traffic_control_restored"] and not runtime["physical_pod_changed"]
        runner = queue_loss if runtime.get("queue_loss_requested") else action_loss
        frames, offsets, selected, detail = runner(directory, runtime, windows)
    expected = detail.pop("expected_loss_components")
    assert {k: v for k, v in result["loss_components"].items() if v} == expected
    result.update(detail)
    result.update(
        packet_byte_window_bounds=[
            AUDIT["reconcile"](frames, w["first_read_ns"], w["last_read_ns"], offsets, w["deltas"])
            for w in selected
        ],
        service_window_bounds=CAP["reconcile"](
            [(at, size) for at, dr, size in frames if dr == "tx"], selected, offsets
        ),
        clock_offset_spread_ns=max(offsets) - min(offsets),
        fixed_timing_allowance_ns=1_000_000,
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
