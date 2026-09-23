"""Independent packet/byte interval audit and controlled pre-driver loss review.

No EMOSA imports. Counter reconciliation does not qualify loss, capacity or a
complete neighbor-metric publisher. Only the retained isolated veth lab applies.
"""

import argparse
import json
import runpy
import struct
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMING_ALLOWANCE_NS = 1_000_000


def read(path):
    return json.loads(path.read_text())


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def packets(path, linktype=1):
    data = path.read_bytes()
    assert len(data) >= 24 and data[:4] == b"\xd4\xc3\xb2\xa1"
    assert struct.unpack_from("<I", data, 20)[0] == linktype
    result, pos = [], 24
    while pos < len(data):
        assert pos + 16 <= len(data)
        sec, us, captured, original = struct.unpack_from("<IIII", data, pos)
        assert us < 1_000_000 and 14 <= captured == original <= 65535
        pos += 16
        assert pos + captured <= len(data)
        result.append((sec * 1_000_000_000 + us * 1000, data[pos : pos + captured]))
        pos += captured
    assert result
    return result


def reconcile(frames, first_read, last_read, offsets, deltas):
    """Bound counts around uncertain counter-read instants; never widen on failure."""
    lo, hi = min(offsets), max(offsets)
    assert hi - lo < 1_000_000, "clock offset varied beyond the selected audit budget"
    a, b = first_read
    c, d = last_read
    assert 0 < a <= b < c <= d and c - a < 2_000_000_000
    narrow = (b + hi + TIMING_ALLOWANCE_NS, c + lo - TIMING_ALLOWANCE_NS)
    wide = (a + lo - TIMING_ALLOWANCE_NS, d + hi + TIMING_ALLOWANCE_NS)
    assert narrow[0] < narrow[1]
    result = {}
    for direction in ("tx", "rx"):
        minimum = [
            size for at, dr, size in frames if dr == direction and narrow[0] < at < narrow[1]
        ]
        maximum = [size for at, dr, size in frames if dr == direction and wide[0] < at < wide[1]]
        for field, lower, upper in (
            ("packets", len(minimum), len(maximum)),
            ("bytes", sum(minimum), sum(maximum)),
        ):
            key = direction + "_" + field
            assert lower <= deltas[key] <= upper, (key, lower, deltas[key], upper)
            result[key] = {"minimum": lower, "observed": deltas[key], "maximum": upper}
    return result


def check_native(directory):
    base = runpy.run_path(str(ROOT / "scripts/check-neighbor-binding.py"))["check"](directory)
    captured = packets(directory / "forwarding.pcap")
    links = read(directory / "neighbor-link-observations.json")
    pod = next(v for v in links["containers"]["em-baseline-agent"] if v["ifname"] == "eth1")
    controller = next(
        v for v in links["containers"]["em-baseline-controller"] if v["ifname"] == "eth1"
    )
    probe = read(directory / "clients-active-0000.json")["observations"]
    assert probe["em-baseline-wired"]["routes"][0]["prefsrc"] == "192.0.2.20"
    # Classic Ethernet pcap has no direction bit. This closed lab accepts only
    # the inventoried pod, peer and adapter sources plus the two observed clients.
    wired = {
        frame[6:12].hex(":")
        for _, frame in captured
        if len(frame) >= 34
        and frame[12:14] == b"\x08\x00"
        and frame[26:30] == bytes((192, 0, 2, 20))
    }
    assert len(wired) == 1
    wifi = probe["em-baseline-wifi"]["supplicant"]["address"]
    transmitters = {pod["address"], wifi, *wired}
    # A bridge can originate IPv6 control traffic through this port. Admit only
    # its independently inventoried local identity, and only if captured; an
    # arbitrary additional source still fails direction classification.
    bridge = next(v for v in links["containers"]["em-baseline-agent"] if v["ifname"] == "br-lan")
    if any(frame[6:12].hex(":") == bridge["address"] for _, frame in captured):
        transmitters.add(bridge["address"])
    receivers = {
        controller["address"],
        "02:00:00:e0:00:01",
        links["adapter_control_interface"][0]["address"],
    }
    assert not transmitters & receivers
    frames, totals = [], Counter()
    for at, frame in captured:
        source = frame[6:12].hex(":")
        assert source in transmitters | receivers, "unclassified source; direction unproven"
        direction = "tx" if source in transmitters else "rx"
        frames.append((at, direction, len(frame)))
        totals[direction + "_packets"] += 1
        totals[direction + "_bytes"] += len(frame)
    events = lines(directory / "station-events.jsonl")
    offsets = [v["received_wall_ns"] - v["received_monotonic_ns"] for v in events]
    windows, seen, exact = [], set(), 0
    for observation in lines(directory / "forwarding-samples.jsonl"):
        window = observation["window"]
        if window is None:
            continue
        key = (observation["worker_pid"], observation["sample"]["started_ns"])
        if key in seen:
            continue
        seen.add(key)
        check = reconcile(
            frames,
            window["first_read_ns"],
            window["last_read_ns"],
            offsets,
            window["deltas"]["eth1"],
        )
        for direction in ("rx", "tx"):
            value = check[direction + "_packets"]
            exact += value["minimum"] == value["observed"] == value["maximum"]
        windows.append({"worker_pid": key[0], "started_ns": key[1], "bounds": check})
    assert len(windows) == base["raw_forwarding"]["checked_counter_windows"] >= 90
    assert exact >= len(windows) * 2 * 0.8, "timing bounds too broad for useful reconciliation"
    return {
        "packet_byte_reconciliation_passed": True,
        "scope": "owned veth interface packet/byte deltas, including transit and multicast",
        "counter_windows": len(windows),
        "exact_direction_packet_windows": exact,
        "clock_offset_spread_ns": max(offsets) - min(offsets),
        "fixed_timing_allowance_ns": TIMING_ALLOWANCE_NS,
        "classified_capture_totals": dict(totals),
        "tx_source_macs": sorted(transmitters),
        "rx_source_macs": sorted(receivers),
        "window_bounds": windows,
        "loss_counters_qualified": False,
        "capacity_qualified": False,
        "measurement_source_qualified": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


def echo_inventory(path):
    result = Counter()
    for _, frame in packets(path):
        assert frame[12:14] == b"\x08\x00" and frame[23] == 1
        offset = 14 + (frame[14] & 15) * 4
        assert offset >= 34 and len(frame) >= offset + 8
        kind, code, _, identifier, sequence = struct.unpack_from("!BBHHH", frame, offset)
        assert kind in (0, 8) and code == 0 and identifier in (31001, 31002, 31003)
        result[(identifier, kind, sequence)] += 1
    return result


def check_loss(directory):
    result = read(directory / "result.json")
    assert result["status"] == "observed_pending_independent_review"
    assert not result["cleanup_errors"] and result["traffic_control_restored"]
    assert result["physical_pod_changed"] is False
    health = runpy.run_path(str(ROOT / "scripts/check-capture-health.py"))["check_file"]
    captures = {
        name: health(directory / (name + ".pcap"), directory / (name + "-capture.log"), 1)
        for name in ("ingress", "backhaul")
    }
    ingress, backhaul = (echo_inventory(directory / (n + ".pcap")) for n in ("ingress", "backhaul"))
    expected_ingress, expected_backhaul = Counter(), Counter()
    for identifier in (31001, 31002, 31003):
        for seq in range(1, 18):
            expected_ingress[(identifier, 8, seq)] += 1
            if identifier != 31002:
                expected_ingress[(identifier, 0, seq)] += 1
                expected_backhaul[(identifier, 8, seq)] += 1
                expected_backhaul[(identifier, 0, seq)] += 1
    assert ingress == expected_ingress and backhaul == expected_backhaul
    (drop,) = [v for v in result["phases"] if v["phase"] == "drop"]
    before = next(v for v in drop["before"]["links"] if v["ifname"] == "eth1")["stats64"]
    after = next(v for v in drop["after"]["links"] if v["ifname"] == "eth1")["stats64"]
    delta = {
        d + "_" + k: after[d][k] - before[d][k]
        for d in ("rx", "tx")
        for k in ("packets", "bytes", "errors", "dropped")
    }
    filters = drop["after"]["filters"]
    entries = [v for v in filters if "options" in v]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["pref"] == 49190 and entry["kind"] == "flower"
    keys = entry["options"]["keys"]
    assert keys["src_ip"] == "192.0.2.20" and keys["dst_ip"] == "192.0.2.1"
    (action,) = entry["options"]["actions"]
    assert action["kind"] == "gact" and action["control_action"]["type"] == "drop"
    assert action["stats"]["packets"] == action["stats"]["drops"] == 17
    (initial_filter,) = [v for v in drop["before"]["filters"] if "options" in v]
    (initial_action,) = initial_filter["options"]["actions"]
    assert initial_action["index"] == action["index"]
    assert initial_action["stats"]["packets"] == initial_action["stats"]["drops"] == 0
    assert delta["tx_errors"] == delta["tx_dropped"] == 0
    assert drop["returncode"] == 1
    assert [v["returncode"] for v in result["phases"]] == [0, 1, 0]
    return {
        "controlled_loss_accounting_checks_passed": True,
        "capture_health": captures,
        "intentionally_dropped_echo_requests": 17,
        "tc_action_drops": 17,
        "rtnetlink_backhaul_deltas_during_drop": delta,
        "rtnetlink_tx_errors_and_drops_miss_this_loss": True,
        "traffic_control_restored": True,
        "reported_veth_speed_mbps": result["reported_veth_speed_mbps"],
        "reported_speed_is_measured_capacity": False,
        "measurement_source_qualified": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--loss-probe", action="store_true")
    args = parser.parse_args()
    print(json.dumps((check_loss if args.loss_probe else check_native)(args.directory), indent=2))
