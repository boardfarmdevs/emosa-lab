"""Independent packet, framing and bounded service-work review. No EMOSA imports.

The simulated service estimate is distinct from complete neighbor reporting and
physical PHY qualification. Compressed evidence is expanded only in a temporary
directory, with a fixed size budget.
"""

import argparse
import gzip
import json
import runpy
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = runpy.run_path(str(ROOT / "scripts/check-backhaul-accounting.py"))
HEALTH = runpy.run_path(str(ROOT / "scripts/check-capture-health.py"))["check_file"]
RECEIVE = runpy.run_path(str(ROOT / "scripts/check-receive-accounting.py"))


def read(path):
    return json.loads(path.read_text())


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def capture(directory, name):
    path = directory / (name + ".pcap")
    with tempfile.TemporaryDirectory(prefix="emosa-capacity-") as temporary:
        if not path.exists():
            packed = directory / (name + ".pcap.gz")
            path = Path(temporary) / (name + ".pcap")
            count = 0
            with gzip.open(packed, "rb") as source, path.open("xb") as target:
                while block := source.read(1048576):
                    count += len(block)
                    assert count <= 200 * 1024 * 1024, "capture expansion budget"
                    target.write(block)
        health = HEALTH(path, directory / (name + "-capture.log"), 1)
        return AUDIT["packets"](path), health


def rows(directory, native, *, shaped=False):
    raw = lines(directory / "egress-observations.jsonl")
    final = read(directory / "egress-observer-final.json")
    assert raw[-1] == final and not final["running"] and not final["errors"]
    assert all(not v["errors"] and v["collector_epoch"] == final["collector_epoch"] for v in raw)
    epochs = [v["configuration_epoch"] for v in raw]
    assert epochs == sorted(epochs)
    origins = {v["observation"]["started_ns"]: v for v in raw if v["observation"]}
    if native:
        values = [
            (
                v["worker_pid"],
                v["shaped_backhaul" if shaped else "virtual_capacity"],
                v["sample"]["egress_observation"] if v["sample"] else None,
            )
            for v in lines(directory / "forwarding-samples.jsonl")
        ]
    else:
        by_heartbeat = {v["heartbeat_ns"]: v for v in raw}
        values = [
            (1, v, by_heartbeat[v["heartbeat_ns"]])
            for v in read(
                directory
                / ("shaped-projection.json" if shaped else "virtual-capacity-projection.json")
            )["observations"]
        ]
    previous, previous_status = {}, {}
    workers, generations, windows = set(), set(), []
    baselines = duplicates = 0
    totals = Counter()
    for worker, status, payload in values:
        workers.add(worker)
        assert not status["measurement_source_qualified"] and not status["capacity_qualified"]
        assert not status["native_neighbor_metric_delivery_proven"]
        sample, window, estimate = status["sample"], status["window"], status["service_estimate"]
        if sample is None:
            assert window is None and estimate is None and not status["available"]
            previous.pop(worker, None)
            continue
        origin = origins[sample["started_ns"]]
        assert payload == origin and origin["running"] and not origin["errors"]
        (q,) = [q for q in origin["observation"]["qdiscs"] if not shaped or q.get("root")]
        assert q["kind"] == "tbf" and q["handle"] == "4e00:" and q["root"] is True
        assert q["options"] == {
            "rate": 12500000,
            "burst": "64Kb/1",
            "mpu": 0,
            "lat": 36700,
            "linklayer": "ethernet",
        }
        assert q["stab"] == {
            "linklayer": "ethernet",
            "overhead": 24,
            "mpu": 84,
            "mtu": 2048,
            "tsize": 2048,
        }
        expected = {
            "service_bytes": q["bytes"],
            "service_packets": q["packets"],
            "queue_drops": q["drops"],
            "token_waits": q["overlimits"],
        }
        if shaped:
            expected.update(RECEIVE["counters"](origin))
        assert sample["counters"] == expected
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
        assert sample["local_interface"] == origin["mac"] and sample["ifindex"] == origin["ifindex"]
        generations.add((worker, sample["epoch"][0]))
        old = previous.get(worker)
        if old == sample:
            assert previous_status[worker] == status
            duplicates += 1
            continue
        if window is None:
            assert estimate is None
            baselines += 1
        else:
            assert old is not None and old["epoch"] == sample["epoch"] and status["available"]
            assert window["first_read_ns"] == [old["started_ns"], old["ended_ns"]]
            assert window["last_read_ns"] == [sample["started_ns"], sample["ended_ns"]]
            assert window["deltas"] == {
                k: v - old["counters"][k] for k, v in sample["counters"].items()
            }
            assert min(window["deltas"].values()) >= 0
            if shaped:
                delta = window["deltas"]
                assert (
                    window["transmit_losses"]
                    == delta["tx_driver_drops"] + delta["tx_action_drops"] + delta["queue_drops"]
                )
                assert (
                    window["receive_losses"]
                    == delta["rx_interface_drops"] + delta["rx_action_drops"]
                )
                totals.update(
                    {
                        k: delta[k]
                        for k in (
                            "tx_driver_drops",
                            "tx_action_drops",
                            "queue_drops",
                            "rx_interface_drops",
                            "rx_action_drops",
                        )
                    }
                )
            a, b = window["first_read_ns"]
            c, d = window["last_read_ns"]
            assert 0 < a <= b < c <= d and c - a < 2_000_000_000
            work = window["deltas"]["service_bytes"] * 80
            minimum, maximum = c - b, d - a
            assert estimate["service_work_ns"] == work
            assert estimate["interval_ns_bounds"] == [minimum, maximum]
            assert estimate["available_percent"] == max(
                0, 100 * (1 - work / ((minimum + maximum) / 2))
            )
            assert estimate["available_percent_bounds"] == [
                max(0, 100 * (1 - work / t)) for t in (minimum, maximum)
            ]
            assert estimate["mtu_payload_capacity_mbps"] == 100 * 1500 / 1538
            assert (
                estimate["nominal_service_mbps"] == 100 and estimate["burst_credit_bytes"] == 65536
            )
            windows.append({**window, "estimate": estimate})
        previous[worker], previous_status[worker] = sample, status
    return {
        "service_windows": len(windows),
        "baseline_samples": baselines,
        "worker_processes": len(workers),
        "connection_generations": len(generations),
        "duplicate_observations": duplicates,
        "configuration_epochs": len(set(epochs)),
        **({"loss_components": dict(totals)} if shaped else {}),
    }, windows


def charged_size(frame_size):
    # Exact iproute2 6.1.0 tc_calc_size_table with mtu=tsize=2048:
    # cell_log=1, cell_align=-1, size_log=0. Odd sizes round UP by one
    # modeled octet; a 2048-entry table does not imply byte precision.
    return max(84, ((frame_size + 24 + 1) // 2) * 2)


def reconcile(frames, windows, offsets):
    assert max(offsets) - min(offsets) < 1_000_000
    results = []
    for window in windows:
        a, b = window["first_read_ns"]
        c, d = window["last_read_ns"]
        narrow = (b + max(offsets) + 1_000_000, c + min(offsets) - 1_000_000)
        wide = (a + min(offsets) - 1_000_000, d + max(offsets) + 1_000_000)
        minimum = [charged_size(size) for at, size in frames if narrow[0] < at < narrow[1]]
        maximum = [charged_size(size) for at, size in frames if wide[0] < at < wide[1]]
        bounds = {}
        for key, lower, upper in (
            ("service_packets", len(minimum), len(maximum)),
            ("service_bytes", sum(minimum), sum(maximum)),
        ):
            actual = window["deltas"][key]
            assert lower <= actual <= upper, (key, lower, actual, upper)
            bounds[key] = {"minimum": lower, "observed": actual, "maximum": upper}
        results.append(bounds)
    return results


def workload(frames, nonce):
    result = {n: {} for n in (1, 2, 3)}
    for at, frame in frames:
        assert len(frame) <= 1514, "GSO/jumbo frame outside calibration"
        if frame[12:14] != b"\x08\x00" or frame[23] != 17 or frame[42:46] != b"EMVL":
            continue
        assert frame[14] == 0x45 and not (int.from_bytes(frame[20:22], "big") & 0x3FFF)
        assert frame[26:34] == bytes((192, 0, 2, 20, 192, 0, 2, 1))
        assert int.from_bytes(frame[36:38], "big") == 49191
        assert frame[46:54] == bytes.fromhex(nonce)
        phase, seq = frame[54], int.from_bytes(frame[55:58], "big")
        assert phase in result and seq not in result[phase]
        result[phase][seq] = (at, frame)
    return result


def check(directory, native=False):
    runtime = read(directory / "result.json")
    assert not runtime["cleanup_errors"] and not runtime["sustained_operation_proven"]
    result, windows = rows(directory, native)
    assert windows, "no service windows: detailed framing/source qualification is missing"
    if native:
        assert runtime["virtual_link_requested"] and runtime["virtual_link_restoration"]["restored"]
        assert result["service_windows"] >= 90 and result["connection_generations"] == 3
        assert result["worker_processes"] == 2
        base = AUDIT["check_native"](directory)
        raw, health = capture(directory, "forwarding")
        assert all(len(f) <= 1514 and f[12:14] not in (b"\x81\x00", b"\x88\xa8") for _, f in raw)
        tx, rx = set(base["tx_source_macs"]), set(base["rx_source_macs"])
        assert all(f[6:12].hex(":") in tx | rx for _, f in raw)
        frames = [(at, len(f)) for at, f in raw if f[6:12].hex(":") in tx]
        offsets = [
            v["received_wall_ns"] - v["received_monotonic_ns"]
            for v in lines(directory / "station-events.jsonl")
        ]
        gap = read(directory / "telemetry-gap-check.json")
        assert all(
            gap[p]["virtual_capacity"]["available"]
            for p in ("session_before", "session_withheld", "session_after")
        )
        lost = read(directory / "recovery-checks.json")[0]["session_unavailable"][
            "virtual_capacity"
        ]
        assert lost["service_estimate"] is None and not lost["available"]
        result["capture_health"] = {"forwarding": health}
        result["native_ovsdb_handoff_checks_passed"] = True
    else:
        assert runtime["status"] == "observed_pending_independent_review"
        assert runtime["traffic_control_restored"]
        receiver = read(directory / "receiver.json")
        assert not receiver["errors"] and receiver["stopped_by_signal"]
        assert receiver["label"] == runtime["label"] and receiver["nonce"] == runtime["nonce"]
        assert receiver["source_sha256"] == runtime["source_hashes"]["link-traffic.py"]
        captured = {n: capture(directory, n) for n in ("offered", "backhaul")}
        inventories = {n: workload(v[0], runtime["nonce"]) for n, v in captured.items()}
        tx = {runtime["pod_backhaul"]["address"]}
        tx.update(f[6:12].hex(":") for _, f in inventories["backhaul"][1].values())
        phases = []
        for phase in runtime["phases"]:
            n, sender = phase["phase"], phase["sender"]
            count = sender["packets_sent"]
            assert sender["source_sha256"] == receiver["source_sha256"] and not sender["errors"]
            offered, delivered = (inventories[k][n] for k in ("offered", "backhaul"))
            assert set(offered) == set(delivered) == set(range(count))
            assert all(offered[s][1] == delivered[s][1] for s in offered)
            received = [(seq, length) for p, seq, length, _, _ in receiver["records"] if p == n]
            assert Counter(received) == Counter(
                (s, sender["udp_payload_size"]) for s in range(count)
            )
            assert all(len(f) == sender["udp_payload_size"] + 42 for _, f in delivered.values())
            first = min(at for at, _ in delivered.values())
            middle = [
                f
                for at, f in delivered.values()
                if first + 1_000_000_000 <= at < first + 3_000_000_000
            ]
            measured = sum(charged_size(len(f)) for f in middle) * 8 / 2 / 1_000_000
            expected = {1: 50, 2: 100, 3: 5}[n]
            assert expected * 0.99 <= measured <= expected * 1.01
            selected = [
                w
                for w in windows
                if sender["started_ns"] + 500_000_000 < w["first_read_ns"][0]
                and w["last_read_ns"][1] < sender["ended_ns"] - 500_000_000
            ]
            assert len(selected) >= 3
            available = [w["estimate"]["available_percent"] for w in selected]
            assert all(abs(v - (100 - expected)) <= 3 for v in available)
            before, after = phase["before"][0], phase["after"][0]
            assert before["drops"] == after["drops"] == 0
            if n == 2:
                assert after["overlimits"] > before["overlimits"]
            phases.append(
                {
                    "phase": n,
                    "sent_and_received": count,
                    "requested_wire_mbps": sender["target_wire_bps"] / 1e6,
                    "observed_middle_two_seconds_mbps": measured,
                    "steady_service_windows": len(selected),
                    "available_percent_range": [min(available), max(available)],
                }
            )
        frames = [(at, len(f)) for at, f in captured["backhaul"][0] if f[6:12].hex(":") in tx]
        offsets = [p["wall_ns"] - p["started_ns"] for p in runtime["phases"]]
        # Captures cover all collector windows, including idle intervals.
        result.update(
            calibration_checks_passed=True,
            phases=phases,
            capture_health={n: v[1] for n, v in captured.items()},
        )
    result.update(
        service_window_bounds=reconcile(frames, windows, offsets),
        clock_offset_spread_ns=max(offsets) - min(offsets),
        fixed_timing_allowance_ns=1_000_000,
        service_estimate_checked=True,
        complete_neighbor_source_qualified=False,
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
