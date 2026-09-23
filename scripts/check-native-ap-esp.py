"""Independently check native sparse ESP receipt or the retained baseline crash.

No adapter imports. Literal Ethernet/TLV fields and later native data-model reads
establish parser behavior only; ESP values are synthetic, not qualified sensors.
"""

import argparse
import json
import runpy
from pathlib import Path

HERE = Path(__file__).parent
NATIVE = runpy.run_path(str(HERE / "check-native-onboarding.py"))
HEALTH = runpy.run_path(str(HERE / "check-capture-health.py"))["check"]
NAMES = tuple("EstServiceParameters" + ac for ac in ("BE", "BK", "VO", "VI"))
FLAGS = (
    0x90,
    0xF0,
    0x80,
    0xC0,
    0xA0,
    0xB0,
    0xD0,
    0xE0,
    0x10,
    0x90,
    0x90,
    0x90,
    0xF0,
    0xF0,
    0,
    0x80,
)
LENGTHS = (6, 12, 3, 6, 6, 9, 9, 9, 3, 5, 7, 0, 11, 13, 0, 4)


def observed(objects):
    rows = {k: v for group in objects for k, v in group.items() if isinstance(v, dict)}
    (device,) = [k for k, v in rows.items() if v.get("ID") == "02:00:00:00:30:01"]
    (path,) = [
        k for k, v in rows.items() if k.startswith(device) and v.get("BSSID") == "02:00:00:ec:02:00"
    ]
    bss = rows[path]
    return {name: bss[name] for name in NAMES} | {
        "Utilization": rows[path.rsplit("BSS.", 1)[0]]["Utilization"]
    }


def check_case(row, packet, objects, previous):
    index = row["index"]
    assert 0 <= index < 16
    assert packet["kind"] == 0x800C and packet["mid"] == 0xD000 + index
    assert (packet["source"], packet["destination"]) == ("020000003001", "020000e00001")
    assert len(packet["tlvs"]) == 1 and packet["tlvs"][0][0] == 0x94
    data = packet["tlvs"][0][1]
    assert data[:6] == bytes.fromhex("020000ec0200")
    assert data[6:10] == bytes((40 + index, 0, 0, FLAGS[index]))
    assert len(data) == 10 + LENGTHS[index]
    expected_payload = b"".join(
        bytes((0x10 + i, 0x40 + index, 0x80 + i)) for i in range(4) if FLAGS[index] & (0x80 >> i)
    )
    assert data[10:] == (expected_payload + bytes(15))[: LENGTHS[index]]
    valid = index < 8
    assert row["valid_presence_length"] is valid and row["flags"] == FLAGS[index]
    expected = dict(previous)
    if valid:
        cursor = 10
        for i, name in enumerate(NAMES):
            if FLAGS[index] & (0x80 >> i):
                # The selected x86 native model preserves the existing low-byte
                # scalar representation. This is not a new WFA ESP conversion.
                a, b, c = data[cursor : cursor + 3]
                expected[name] = a + 256 * b + 65536 * c
                cursor += 3
        expected["Utilization"] = 40 + index
    assert row["expected"] == expected
    actual = observed(objects)
    assert actual == expected, "controller sparse ESP receipt mismatch"
    last = row["observations"][-1]
    assert last["actual"] == actual
    assert packet["time"] + 0.001 < last["wall_started_ns"] / 1e9 <= last["wall_ended_ns"] / 1e9
    assert last["wall_ended_ns"] / 1e9 < packet["time"] + 3
    assert 0 <= packet["time"] - row["sent_at"] < 1
    return expected


def check(directory, baseline_failure=False):
    def read(name):
        return json.loads((directory / name).read_text())

    result, probe, trial = (
        read("result.json"),
        read("ap-esp-probe.json"),
        read("candidate-trial.json"),
    )
    assert result["synthetic_ap_esp_probe_requested"] and result["active_seconds_requested"] == 0
    assert not probe["measurement_source_qualified"] and not probe["sustained_operation_proven"]
    assert trial["baseline_restored"]
    restored = read("post-restoration-observation.json")
    assert restored["owned_idle_check_passed"]
    assert (
        restored["actual_restored_files"]["/opt/prpl-install-nl80211/bin/beerocks_controller"]
        == trial["baseline_sha256"]
    )
    for name, hashes in trial["runtime_libraries"].items():
        assert (
            restored["actual_restored_files"]["/opt/prpl-install-nl80211/lib/" + name]
            == hashes["baseline_sha256"]
        )
    health = HEALTH(directory)
    packets = NATIVE["packets"](directory / "ethernet.pcap")
    reports = [p for p in packets if p["kind"] == 0x800C]
    outcomes = {item["unit"]: item for item in result["native_shutdown"]["em-baseline-controller"]}
    if baseline_failure:
        assert not probe["passed"] and result["status"] == "failed"
        assert len(probe["cases"]) == len(reports) == 1
        assert reports[0]["mid"] == 0xD000
        assert (reports[0]["source"], reports[0]["destination"]) == ("020000003001", "020000e00001")
        assert reports[0]["tlvs"] == [(0x94, bytes.fromhex("020000ec020028000090104080134083"))]
        NATIVE["candidate_inputs"](read("candidate.json"), trial)
        assert len(read("candidate.json")["extra_patches"]) == 2
        assert outcomes["controller"]["after"] == {"Result": "core-dump", "ExecMainStatus": "11"}
        assert result["cleanup_errors"] == ["native_process_observation:CalledProcessError"]
        return {
            "baseline_sparse_report_crash_observed": True,
            "signal": 11,
            "capture_health": health,
            "baseline_restored": True,
            "physical_pod_proven": False,
        }
    assert probe["passed"] and not result["cleanup_errors"]
    assert len(probe["cases"]) == len(reports) == 16
    expected = observed(read("controller-after.json"))
    for index, row in enumerate(probe["cases"]):
        assert row["index"] == index
        packet = next(p for p in reports if p["mid"] == 0xD000 + index)
        objects = read(f"ap-esp-inventory-{index:02}.json")
        expected = check_case(row, packet, objects, expected)
        if index < 15:
            assert (
                row["observations"][-1]["wall_ended_ns"] / 1e9
                < probe["cases"][index + 1]["sent_at"]
            )
    for outcome in outcomes.values():
        assert outcome["after"] == {"Result": "success", "ExecMainStatus": "0"}
    start, stop = read("native-processes-start.json"), read("native-processes-stop.json")
    assert (
        start["units"]["controller"]["properties"]["MainPID"]
        == stop["units"]["controller"]["properties"]["MainPID"]
    )
    assert start["units"]["controller"]["executable_sha256"] == trial["candidate_sha256"]
    onboarding = NATIVE["check"](directory, "tshark")
    return {
        "native_sparse_esp_parser_checks_passed": True,
        "valid_presence_combinations": 8,
        "malformed_inputs_rejected_without_esp_or_utilization_change": 8,
        "bounded_onboarding": onboarding,
        "capture_health": health,
        "clean_native_shutdown": True,
        "baseline_restored": True,
        "measurement_source_qualified": False,
        "native_ap_report_delivery_proven": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--baseline-failure", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.directory, args.baseline_failure), indent=2))
