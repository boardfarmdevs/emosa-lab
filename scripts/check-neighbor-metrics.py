"""Independent neighbor-metric wire vectors and optional native unavailable-source audit.

No EMOSA imports. Wire correctness and explicit native refusal do not establish
a qualified measurement publisher or successful native metric delivery.
"""

import argparse
import hashlib
import json
import runpy
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/protocol/neighbor-metrics"


def check_vectors(directory, tool="tshark"):
    capture = directory / "synthetic-neighbor-metrics.pcap"
    data = subprocess.check_output(
        [tool, "-n", "-r", str(capture), "-T", "pdml"], stderr=subprocess.DEVNULL, timeout=30
    )
    packets = ET.fromstring(data).findall("packet")
    assert len(packets) == 10
    tx = {
        "bridgeFlag": [1, 0],
        "packetErrors": [3, 2**32 - 1],
        "transmittedPackets": [513, 2**32 - 2],
        "macThroughputCapacity": [940, 65],
        "linkAvailability": [97, 0],
        "phyRate": [65535, 65535],
    }
    rx = {"packetErrors": [2, 7], "packets_received": [1027, 8193], "rssi": [255, 42]}
    for index, packet in enumerate(packets):

        def values(name, packet=packet):
            return [f.attrib["value"] for f in packet.findall(f".//field[@name='ieee1905.{name}']")]

        def numbers(name):
            return [int(v, 16) for v in values(name)]

        assert not packet.findall(".//proto[@name='_ws.malformed']")
        case = index // 2
        assert numbers("message_type") == [5 if index % 2 == 0 else 6]
        assert numbers("message_id") == [400 + case]
        if index % 2 == 0:
            assert numbers("tlv_type") == [8, 0]
            assert numbers("tlv_length") == [2 if case < 3 else 8, 0]
            assert numbers("link_metric_query_type") == [0 if case < 3 else 1]
            assert numbers("link_metrics_requested") == [case if case < 3 else 2]
            continue
        kinds = ([9], [10], [9, 10], [9, 10], [12])[case]
        assert numbers("tlv_type") == [*kinds, 0]
        if case == 4:
            assert numbers("link_metric.result_code") == [0]
            continue
        assert values("responder_al_mac_addr") == ["020000004001"] * len(kinds)
        assert values("receiving_al_mac_addr") == ["020000005001", "020000005003"] * len(kinds)
        assert values("neighbor_al_mac_addr") == [
            "020000004002",
            "020000005002",
            "020000005004",
        ] * len(kinds)
        assert numbers("dev_info.media_type") == [1, 0x103] * len(kinds)
        for name in set(tx) | set(rx):
            expected = (tx.get(name, []) if 9 in kinds else []) + (
                rx.get(name, []) if 10 in kinds else []
            )
            assert numbers(name) == expected, (index, name)
    return {
        "wire_vector_checks_passed": True,
        "frames": len(packets),
        "scope": "synthetic query/response layout, not link measurements",
        "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        "dissector": subprocess.check_output([tool, "--version"], text=True).splitlines()[0],
        "measurement_source_qualified": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


def queries_during_source_loss(queries, recoveries, events):
    """Classify only frames strictly inside independently timed source-loss windows."""
    offsets = [e["received_wall_ns"] - e["received_monotonic_ns"] for e in events]
    assert offsets and max(offsets) - min(offsets) < 20_000_000
    offset = sorted(offsets)[len(offsets) // 2] / 1e9
    frames = set()
    for fault in recoveries:
        if fault["kind"] != "pod_connection_loss":
            continue
        lost = fault["session_unavailable"]
        assert lost["state"] == "recovering" and not lost["report_source"]["available"]
        losses = [e for e in lost["recovery"]["history"] if e["event"] == "source_lost"]
        assert losses
        start = losses[-1]["at"] + offset
        end = fault["restored_at"]
        assert fault["started_at"] <= start < end <= fault["recovered_at"]
        # Leave boundary races unexplained instead of excusing a live query.
        frames.update(q["frame"] for q in queries if start + 0.02 < q["time"] < end - 0.02)
    return sorted(frames)


def check_native(directory):
    base = runpy.run_path(str(ROOT / "scripts/check-native-onboarding.py"))
    health = runpy.run_path(str(ROOT / "scripts/check-capture-health.py"))["check"](directory)

    def read(name):
        return json.loads((directory / name).read_text())

    assert not read("result.json")["cleanup_errors"]
    packets = base["packets"](directory / "ethernet.pcap")
    queries = [p for p in packets if p["source"] == base["CONTROLLER"] and p["kind"] == 5]
    assert len(queries) >= 3 and all(p["tlvs"] == [(8, b"\0\2")] for p in queries)
    assert not any(p["source"] == base["AGENT"] and p["kind"] == 6 for p in packets), (
        "unqualified metrics emitted"
    )
    recoveries = read("recovery-checks.json")
    snapshots = [f["session_before"] for f in recoveries] + [read("native-session.json")]
    for s in snapshots:
        assert not s["neighbor_link_metrics"]["measurement_available"]
        assert s["counts"].get("neighbor_measurement_unavailable", 0) >= 1
    handled = sum(
        s["neighbor_link_metrics"]["counts"]["measurement_unavailable"] for s in snapshots
    )
    outage_frames = []
    if handled != len(queries):
        events = [
            json.loads(line)
            for line in (directory / "station-events.jsonl").read_text().splitlines()
        ]
        outage_frames = queries_during_source_loss(queries, recoveries, events)
    assert handled + len(outage_frames) == len(queries), (
        "captured queries and unavailable-source decisions/outage windows differ"
    )
    links = read("neighbor-link-observations.json")
    assert not links["measurement_source_qualified"]
    (adapter,) = links["adapter_control_interface"]
    assert adapter["ifname"] == "probe0" and adapter["address"] == "02:00:00:00:30:01"
    pod = next(r for r in links["containers"]["em-baseline-agent"] if r["ifname"] == "eth1")
    controller = next(
        r for r in links["containers"]["em-baseline-controller"] if r["ifname"] == "eth1"
    )
    assert pod["address"] != adapter["address"]
    root = {r["ifindex"]: r for r in links["vm_links"]}
    for row in (pod, controller, adapter):
        assert root[row["link_index"]]["master"] == "em-base-bh"
    bridge = next(r for r in root.values() if r["ifname"] == "em-base-bh")
    assert bridge["linkinfo"]["info_kind"] == "bridge"
    return {
        "native_unavailable_source_checks_passed": True,
        "capture_health": health,
        "native_query_frames": [p["frame"] for p in queries],
        "queries_handled_without_measurement": handled,
        "queries_during_observed_source_loss": outage_frames,
        "fabricated_metric_or_invalid_neighbor_responses": 0,
        "proxy_control_interface_distinct_from_pod_backhaul": True,
        "shared_vm_bridge_observed": True,
        "virtual_to_pod_interface_mapping_qualified": False,
        "native_neighbor_metric_delivery_proven": False,
        "measurement_source_qualified": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=FIXTURE)
    parser.add_argument("--native-directory", type=Path)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    print(
        json.dumps(
            check_native(args.native_directory)
            if args.native_directory
            else check_vectors(args.directory, args.tshark),
            indent=2,
        )
    )
