"""Independently audit raw pod forwarding observations, OVSDB handoff and capture.

Standard library only. This does not qualify per-neighbor counters, capacity,
availability, represented topology, or successful IEEE 1905 metric delivery.
"""

import argparse
import json
import runpy
import struct
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = {"eth1", "eth2", "wlan0"}
COUNTERS = {d + "_" + k for d in ("rx", "tx") for k in ("packets", "bytes", "errors", "dropped")}


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def check(directory):
    def read(name):
        return json.loads((directory / name).read_text())

    result = read("result.json")
    assert result["forwarding_observation_requested"] and not result["cleanup_errors"]
    health = runpy.run_path(str(ROOT / "scripts/check-capture-health.py"))["check_file"](
        directory / "forwarding.pcap", directory / "forwarding-capture.log", 1
    )
    links = read("neighbor-link-observations.json")
    pod = next(r for r in links["containers"]["em-baseline-agent"] if r["ifname"] == "eth1")
    peer = next(r for r in links["containers"]["em-baseline-controller"] if r["ifname"] == "eth1")
    (adapter,) = links["adapter_control_interface"]
    assert len({pod["address"], peer["address"], adapter["address"]}) == 3
    root = {r["ifindex"]: r for r in links["vm_links"]}
    assert root[pod["link_index"]]["master"] == "em-base-bh"
    assert root[pod["link_index"]]["link_index"] == pod["ifindex"]

    published = {}
    for event in read_lines(directory / "manager.jsonl"):
        value = event.get("forwarding", {})
        if event.get("publication") != "observed-state" or not value.get("available"):
            continue
        metadata = dict(value["bridge_external_ids"][1])
        assert metadata["complete"] == "true"
        assert metadata["emosa_profile"] == "owned-linux-bridge-observation-v1"
        start = int(metadata["started_ns"])
        assert start not in published
        published[start] = value
    assert len(published) >= 100

    previous, workers, generations = {}, set(), set()
    accepted = windows = withdrawn = 0
    total = {name: {key: 0 for key in COUNTERS} for name in NAMES}
    for event in read_lines(directory / "forwarding-samples.jsonl"):
        assert event["counter_scope"] == "whole-interface-not-per-neighbor"
        assert event["measurement_source_qualified"] is False
        worker = event["worker_pid"]
        workers.add(worker)
        if not event["available"]:
            assert event["sample"] is None and event["window"] is None
            previous.pop(worker, None)
            withdrawn += 1
            continue
        sample = event["sample"]
        start, end = sample["started_ns"], sample["ended_ns"]
        assert 0 < start <= end <= event["observed_ns"] < start + 2_000_000_000
        assert end - start <= 1_000_000_000
        value = published[start]
        metadata = dict(value["bridge_external_ids"][1])
        assert end == int(metadata["ended_ns"])
        assert set(sample["interfaces"]) == set(value["interfaces"]) == NAMES
        for name in NAMES:
            actual, origin = sample["interfaces"][name], value["interfaces"][name]
            assert actual["mac"] == origin["mac_in_use"]
            assert actual["ifindex"] == origin["ifindex"]
            assert actual["mtu"] == origin["mtu"]
            assert actual["peer_ifindex"] == int(dict(origin["external_ids"][1])["peer_ifindex"])
            assert actual["counters"] == dict(origin["statistics"][1])
            assert set(actual["counters"]) == COUNTERS
            assert all(type(v) is int and 0 <= v < 2**63 for v in actual["counters"].values())
        assert sample["interfaces"]["eth1"]["mac"] == pod["address"] != adapter["address"]
        assert sample["interfaces"]["eth1"]["ifindex"] == pod["ifindex"]
        assert sample["interfaces"]["eth1"]["peer_ifindex"] == pod["link_index"]
        generations.add((worker, sample["generation"]))
        old = previous.get(worker)
        window = event["window"]
        if window is not None:
            assert old is not None and old["epoch"] == sample["epoch"]
            assert old["generation"] == sample["generation"]
            assert window["first_read_ns"] == [old["started_ns"], old["ended_ns"]]
            assert window["last_read_ns"] == [start, end]
            assert old["ended_ns"] < start < old["started_ns"] + 2_000_000_000
            for name in NAMES:
                delta = {
                    key: sample["interfaces"][name]["counters"][key]
                    - old["interfaces"][name]["counters"][key]
                    for key in COUNTERS
                }
                assert delta == window["deltas"][name] and all(v >= 0 for v in delta.values())
                for key, count in delta.items():
                    total[name][key] += count
            windows += 1
        elif old and old["epoch"] == sample["epoch"]:
            assert event["reason"] in {"measurement_gap", "counter_reset"}
        previous[worker] = sample
        accepted += 1
    assert accepted >= 100 and windows >= 90 and len(workers) == 2 and len(generations) == 3
    assert withdrawn >= 1  # Actual OVSDB interruption must revoke observations.
    assert all(total[name][d + "_packets"] > 0 for name in NAMES for d in ("rx", "tx"))
    gap = read("telemetry-gap-check.json")
    for phase in ("session_before", "session_withheld", "session_after"):
        value = gap[phase]["forwarding_observation"]
        assert value["available"] and value["window"] is not None
        assert value["measurement_source_qualified"] is False

    # Inspect the independent capture at the pod's VM veth peer. The interface
    # MAC in native Topology Discovery, not the Ethernet source AL, identifies
    # the controller's sending interface (IEEE 1905.1-2013 §8.1).
    data = (directory / "forwarding.pcap").read_bytes()
    offset, number = 24, 0
    discoveries, traffic = [], Counter()
    while offset < len(data):
        _, _, size, _ = struct.unpack_from("<IIII", data, offset)
        frame = data[offset + 16 : offset + 16 + size]
        offset += 16 + size
        number += 1
        assert len(frame) >= 14
        if frame[12:14] == b"\x89\x3a" and frame[14:18] == b"\0\0\0\0":
            assert frame[21] == 0x80
            fields, pos = {}, 22
            while pos + 3 <= len(frame):
                kind, length = struct.unpack_from("!BH", frame, pos)
                pos += 3
                assert pos + length <= len(frame)
                if kind == 0:
                    assert length == 0
                    break
                assert kind not in fields
                fields[kind] = frame[pos : pos + length]
                pos += length
            if fields.get(1) == bytes.fromhex("020000e00001"):
                assert fields[2] == bytes.fromhex(peer["address"].replace(":", ""))
                discoveries.append(number)
        if frame[12:14] == b"\x08\x00" and len(frame) >= 34 and frame[23] == 1:
            source, destination = frame[26:30], frame[30:34]
            for last in (20, 21):
                client, gateway = bytes([192, 0, 2, last]), bytes([192, 0, 2, 1])
                if (source, destination) == (client, gateway):
                    traffic[f"client_{last}_to_gateway"] += 1
                    assert frame[6:12] != bytes.fromhex(pod["address"].replace(":", ""))
                elif (source, destination) == (gateway, client):
                    traffic[f"gateway_to_client_{last}"] += 1
    assert discoveries, "no actual peer interface advertisement observed at pod backhaul"
    assert len(traffic) == 4 and min(traffic.values()) >= 10
    return {
        "raw_forwarding_observation_checks_passed": True,
        "capture_health": health,
        "successful_manager_publications": len(published),
        "accepted_samples": accepted,
        "checked_counter_windows": windows,
        "worker_processes": len(workers),
        "connection_generations": len(generations),
        "withdrawals": withdrawn,
        "raw_forwarding_available_during_telemetry_gap": True,
        "pod_backhaul_mac": pod["address"],
        "controller_advertised_interface_mac": peer["address"],
        "controller_discovery_frames": discoveries,
        "transit_icmp_frames": dict(traffic),
        "nonoverlapping_raw_counter_deltas": total,
        "scope": (
            "Observed interface identities and raw interval transport; "
            "counters not yet reconciled per neighbor"
        ),
        "virtual_to_pod_interface_mapping_qualified": False,
        "measurement_source_qualified": False,
        "native_neighbor_metric_delivery_proven": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), indent=2, sort_keys=True))
