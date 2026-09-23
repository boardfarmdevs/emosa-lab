"""Independent active-client pilot review, never a complete sustained verdict.

Standard library and tshark only; no EMOSA imports. Reuses the separate bounded
onboarding verifier, then checks captured events, detailed native inventory and
independent per-phase traffic against retained run samples.
"""

import argparse
import json
import re
import runpy
from pathlib import Path

REFERENCE = runpy.run_path(str(Path(__file__).with_name("check-native-onboarding.py")))
STA, BSSID, AL = "02:00:00:00:02:00", "02:00:00:ec:02:00", "02:00:00:00:30:01"


def read(path):
    return json.loads(path.read_text())


def stations(path):
    rows = {k: v for obj in read(path) for k, v in obj.items()}
    device = [
        k
        for k, v in rows.items()
        if re.fullmatch(r"Device\.WiFi\.DataElements\.Network\.Device\.\d+\.", k)
        and isinstance(v, dict)
        and v.get("ID") == AL
    ]
    assert len(device) == 1
    bss = [k for k, v in rows.items() if k.startswith(device[0]) and v.get("BSSID") == BSSID]
    assert len(bss) == 1
    return [
        v for k, v in rows.items() if k.startswith(bss[0] + "STA.") and v.get("MACAddress") == STA
    ]


def check(directory, tshark, *, recovery=False):
    onboarding = REFERENCE["check"](directory, tshark, initial_only=recovery)
    result = read(directory / "result.json")
    assert result["active_seconds_requested"] >= 90
    samples = read(directory / "active-samples.json")
    assert len(samples) >= 5 and samples[-1]["elapsed"] >= 90
    assert len({s["adapter_pid"] for s in samples}) == (2 if recovery else 1)
    assert all(s["session"]["state"] == "provisioning" for s in samples)
    assert all(s["session"]["telemetry"]["accepted"] > 0 for s in samples)
    rss, descriptors = [], []
    connected_observations = 0
    for index, sample in enumerate(samples):
        status = dict(
            line.split(":", 1) for line in sample["process_status"].splitlines() if ":" in line
        )
        assert int(status["Pid"]) == sample["adapter_pid"] and "Z" not in status["State"]
        rss.append(int(status["VmRSS"].split()[0]))
        descriptors.append(sample["open_descriptors"])
        actual = stations(directory / f"active-inventory-{index:04d}.json")
        assert bool(actual) == sample["controller_station_present"]
        connected_observations += bool(actual)
        probes = read(directory / f"clients-active-{index:04d}.json")
        for name, interface in (("em-baseline-wired", "eth1"), ("em-baseline-wifi", "wlan0")):
            value = probes["observations"][name]
            assert value["interface"] == interface and REFERENCE["zero_packet_loss"](value["ping"])
            assert value["application"]["nonce"] == probes["nonce"]
            assert value["routes"] and all(r.get("dev") == interface for r in value["routes"])
        wifi = probes["observations"]["em-baseline-wifi"]["supplicant"]
        assert wifi["bssid"] == BSSID and wifi["wpa_state"] == "COMPLETED"
    assert connected_observations >= 3
    detached = sorted(directory.glob("detached-inventory-*.json"))
    assert len(detached) >= 2 and all(not stations(p) for p in detached)
    rows = REFERENCE["packets"](directory / "ethernet.pcap")
    events = [
        (p, v)
        for p in rows
        if p["source"] == AL.replace(":", "") and p["kind"] == 1
        for kind, v in p["tlvs"]
        if kind == 0x92
    ]
    assert all(
        v[:12] == bytes.fromhex((STA + BSSID).replace(":", ""))
        and len(v) == 13
        and v[12] in (0, 128)
        for _, v in events
    )
    joined = [p for p, v in events if v[12] == 128]
    left = [p for p, v in events if v[12] == 0]
    assert len(joined) >= 3 and len(left) >= 2
    if not recovery:
        assert [v[12] for _, v in events][:5] == [128, 0, 128, 0, 128]
    capabilities = [p for p in rows if p["kind"] == 0x800A and p["source"] == AL.replace(":", "")]
    assert capabilities
    for reply in capabilities:
        query = next(p for p in rows if p["kind"] == 0x8009 and p["mid"] == reply["mid"])
        assert 0 <= reply["time"] - query["time"] <= 1
        assert REFERENCE["value"](reply, 0x91) == b"\1"
        assert REFERENCE["value"](reply, 0xA3) == b"\3" + bytes.fromhex(STA.replace(":", ""))
    counts = samples[-1]["session"]["counts"]
    return {
        "pilot_checks_passed": True,
        "scope": "native active-worker client-cycle pilot",
        "bounded_onboarding": onboarding,
        "active_seconds_observed": result["active_observed_seconds"],
        "samples": len(samples),
        "native_connected_observations": connected_observations,
        "native_disconnected_observations": len(detached),
        "captured_joins": len(joined),
        "captured_leaves": len(left),
        "capability_unavailable_reports": len(capabilities),
        "rss_kib_range": [min(rss), max(rss)],
        "file_descriptor_range": [min(descriptors), max(descriptors)],
        "unimplemented_request_counts": {
            k: v for k, v in counts.items() if re.fullmatch(r"unsupported_message_[0-9a-f]{4}", k)
        },
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
        "remaining": [
            "independent channel and recovery review",
            "reporting policy/metrics, link metrics and final disassociation statistics",
            "complete sustained-operation acceptance",
        ]
        if recovery
        else [
            "final disassociation statistics and reason",
            "channel and policy/metrics procedures",
            "native adapter and pod-connection recovery",
            "15-minute complete acceptance run",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    print(json.dumps(check(args.directory, args.tshark), indent=2))
