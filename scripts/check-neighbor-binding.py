"""Independent native pod-side discovery, topology-identity and observation-loss audit.

No adapter imports. The retained Ethernet media values remain simulator fixtures;
this does not qualify per-link measurements, a physical PHY or full acceptance.
"""

import argparse
import json
import re
import runpy
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(directory):
    def read(name):
        return json.loads((directory / name).read_text())

    base = runpy.run_path(str(ROOT / "scripts/check-native-onboarding.py"))
    forwarding = runpy.run_path(str(ROOT / "scripts/check-forwarding-observations.py"))["check"](
        directory
    )
    result = read("result.json")
    assert result["neighbor_binding_requested"] and result["neighbor_gap_check_completed"]
    final = read("neighbor-observer-final.json")
    assert final["running"] is False and final["errors"] == []
    assert final["interface"] == "eth1" and final["run_label"] == result["label"]
    assert (
        final["collector_sha256"]
        == read("source-hashes.json")["/opt/emosa-radio-manager/neighbor-observer.py"]
    )
    samples = [
        json.loads(line)
        for line in (directory / "forwarding-samples.jsonl").read_text().splitlines()
    ]
    bounded, observations = 0, {}
    for row in samples:
        neighbor = row["observed_neighbor"]
        if not neighbor["available"]:
            continue
        bounded += 1
        sample, binding = row["sample"], neighbor["binding"]
        port, payload = sample["interfaces"]["eth1"], sample["neighbor_observation"]
        assert binding["generation"] == sample["generation"]
        assert binding["capture_epoch"] == payload["capture_epoch"] == final["capture_epoch"]
        assert binding["local_interface"] == port["mac"] == final["mac"]
        assert binding["local_ifindex"] == port["ifindex"] == payload["ifindex"]
        assert binding["neighbor_al"] == "02:00:00:e0:00:01"
        assert binding["neighbor_interface"] == forwarding["controller_advertised_interface_mac"]
        assert binding["bridges_present"] is True
        assert payload["running"] and not payload["errors"]
        received, now = binding["discovery_received_ns"], row["observed_ns"]
        assert 0 <= now - received < 65_000_000_000
        assert (
            now
            < binding["valid_until_ns"]
            <= min(
                sample["started_ns"] + 2_000_000_000,
                payload["heartbeat_ns"] + 2_000_000_000,
                received + 65_000_000_000,
            )
        )
        (matching,) = [f for f in payload["frames"] if f["received_ns"] == received]
        data = bytes.fromhex(matching["frame_hex"])
        assert data[:6] == bytes.fromhex("0180c2000013") and data[12:14] == b"\x89\x3a"
        assert (
            data[14] == 0 and data[16:18] == b"\0\0" and data[20] == 0 and data[21] & 0xC0 == 0x80
        )
        fields = base["tlvs"](data[22:])
        assert [v.hex(":") for t, v in fields if t == 1] == [binding["neighbor_al"]]
        assert [v.hex(":") for t, v in fields if t == 2] == [binding["neighbor_interface"]]
        observations[received] = data
    assert bounded >= 100 and len(observations) >= 4

    # Convert the pod monotonic timestamps using independently collected kernel
    # station-event time pairs, and match every Discovery to the backhaul pcap.
    events = [
        json.loads(line) for line in (directory / "station-events.jsonl").read_text().splitlines()
    ]
    offsets = [e["received_wall_ns"] - e["received_monotonic_ns"] for e in events]
    assert max(offsets) - min(offsets) < 20_000_000
    offset = sorted(offsets)[len(offsets) // 2]
    data = (directory / "forwarding.pcap").read_bytes()
    packets, pos, number = [], 24, 0
    while pos < len(data):
        sec, us, length, _ = struct.unpack_from("<IIII", data, pos)
        number += 1
        packets.append(
            (number, sec * 1_000_000_000 + us * 1000, data[pos + 16 : pos + 16 + length])
        )
        pos += 16 + length
    matched = []
    for received, frame in observations.items():
        matches = [
            n
            for n, at, payload in packets
            if payload == frame and abs(at - (received + offset)) < 20_000_000
        ]
        assert len(matches) == 1, "pod discovery does not match independent backhaul capture/time"
        matched.extend(matches)

    pod = samples[next(i for i, s in enumerate(samples) if s["observed_neighbor"]["available"])][
        "sample"
    ]
    identities = [
        bytes.fromhex(pod["interfaces"][name]["mac"].replace(":", ""))
        for name in ("eth1", "eth2", "wlan0")
    ]
    topology_frames = []
    for packet in base["packets"](directory / "ethernet.pcap"):
        if packet["source"] != base["AGENT"] or packet["kind"] != 3:
            continue
        device = base["value"](packet, 3)
        assert device[:6].hex() == base["AGENT"] and device[6] == 3
        expected = bytes.fromhex(base["AGENT"]) + b"\x03"
        expected += identities[0] + b"\x00\x01\x00" + identities[1] + b"\x00\x01\x00"
        expected += identities[2] + b"\x01\x03\x0a" + identities[2] + b"\0\0\x06\0"
        assert device == expected
        assert base["value"](packet, 4) == b"\x01\x03" + b"".join(identities)
        assert (
            base["value"](packet, 7) == identities[0] + bytes.fromhex(base["CONTROLLER"]) + b"\x80"
        )
        topology_frames.append(packet["frame"])
    assert len(topology_frames) >= 3
    controller_inventories = 0
    for path in sorted(directory.glob("active-inventory-*.json")):
        rows = {k: v for obj in json.loads(path.read_text()) for k, v in obj.items()}
        (device,) = [
            k
            for k, v in rows.items()
            if re.fullmatch(r"Device\.WiFi\.DataElements\.Network\.Device\.\d+\.", k)
            and v.get("ID") == "02:00:00:00:30:01"
        ]
        assert rows[device]["InterfaceNumberOfEntries"] == 3
        ports = {
            k: v
            for k, v in rows.items()
            if re.fullmatch(re.escape(device) + r"Interface\.\d+\.", k)
        }
        assert {v["MACAddress"] for v in ports.values()} == {m.hex(":") for m in identities}
        (backhaul,) = [k for k, v in ports.items() if v["MACAddress"] == identities[0].hex(":")]
        neighbors = [
            v for k, v in rows.items() if re.fullmatch(re.escape(backhaul) + r"Neighbor\.\d+\.", k)
        ]
        assert neighbors == [{"IsIEEE1905": True, "ID": "02:00:00:e0:00:01"}]
        controller_inventories += 1
    assert controller_inventories >= 5

    gap = read("neighbor-gap-check.json")
    assert 4 <= gap["restored_at"] - gap["started_at"] < 15
    before, during, after = (
        gap[n] for n in ("session_before", "session_withheld", "session_after")
    )
    assert before["observed_neighbor"]["available"] and after["observed_neighbor"]["available"]
    assert (
        not during["observed_neighbor"]["available"]
        and not during["report_source"]["inventory_complete"]
    )
    assert after["report_source"]["inventory_complete"]
    for value in (during, after):
        assert value["report_source"]["available"] and value["forwarding_observation"]["available"]
        assert value["report_source"]["context_token"] == before["report_source"]["context_token"]
        assert value["report_source"]["operating_radio_count"] == 1
        assert value["counts"].get("search_sent") == before["counts"].get("search_sent")
        assert value["counts"].get("client_leave_notification", 0) == 0
    assert gap["operation_before"] == gap["operation_after"]
    assert gap["clients_withheld"]["clients"] == "passed"
    probe = read("clients-neighbor-withheld.json")
    for name, interface in (("em-baseline-wired", "eth1"), ("em-baseline-wifi", "wlan0")):
        value = probe["observations"][name]
        assert value["interface"] == interface and base["zero_packet_loss"](value["ping"])
        assert value["application"]["nonce"] == probe["nonce"]
        assert value["routes"] and all(r.get("dev") == interface for r in value["routes"])
    assert probe["observations"]["em-baseline-wifi"]["supplicant"]["wpa_state"] == "COMPLETED"
    assert not any(
        packet["kind"] == 1 and any(t == 0x92 and v[-1] == 0 for t, v in packet["tlvs"])
        for packet in base["packets"](directory / "ethernet.pcap")
        if gap["started_at"] <= packet["time"] <= gap["completed_at"]
    )
    return {
        "native_neighbor_binding_checks_passed": True,
        "raw_forwarding": forwarding,
        "bound_observation_samples": bounded,
        "independently_matched_discovery_frames": matched,
        "correct_topology_frames": topology_frames,
        "native_controller_interface_inventories_checked": controller_inventories,
        "observer_pause_seconds": gap["restored_at"] - gap["started_at"],
        "topology_withdrawn_without_control_or_radio_loss": True,
        "new_onboarding_operations_during_observer_pause": 0,
        "new_config_writes_during_observer_pause": 0,
        "owned_interface_identity_mapping_observed": True,
        "media_values_are_simulation_fixtures": True,
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
