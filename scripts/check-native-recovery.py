"""Independent operational-soak checks; incomplete policy/metrics remain gaps.

Only stdlib/tshark, never imports EMOSA. Correlates captures, public receipts,
manager observations, native inventory, process samples and independent traffic.
"""

import argparse
import hashlib
import json
import re
import runpy
from pathlib import Path

ACTIVE = runpy.run_path(str(Path(__file__).with_name("check-native-active.py")))
BASE = ACTIVE["REFERENCE"]
read, value = BASE["read"], BASE["value"]
AGENT, CONTROLLER, RADIO = BASE["AGENT"], BASE["CONTROLLER"], BASE["RADIO"]


def traffic(directory, faults, *, require_outages):
    outages_path = directory / "client-outages.json"
    outages = read(outages_path) if outages_path.exists() else []
    if require_outages:
        assert len(outages) >= 2
        assert all(o["restored_at"] >= o["started_at"] for o in outages)
    result = {}
    for name in ("em-baseline-wired", "em-baseline-wifi"):
        text = (directory / (name + "-continuous-ping.log")).read_text()
        received = [
            (float(t), int(seq))
            for t, seq in re.findall(
                r"\[([0-9.]+)\] 64 bytes from 192\.0\.2\.1: icmp_seq=(\d+)", text
            )
        ]
        assert len(received) >= 100 and received == sorted(set(received))
        counts = re.search(r"(\d+) packets transmitted, (\d+) received", text)
        assert counts and int(counts[2]) == len(received)
        assert int(counts[1]) >= len(received)
        gaps = [
            (a, b) for (a, _), (b, _) in zip(received, received[1:], strict=False) if b - a > 1.5
        ]
        if name.endswith("wired"):
            assert not gaps and counts[1] == counts[2]
        elif require_outages:
            assert all(
                any(a >= o["started_at"] - 2 and b <= o["restored_at"] + 3 for o in outages)
                for a, b in gaps
            ), "wireless traffic gap outside a deliberate client outage"
        for fault in faults:
            # Both clients must continue passing traffic during management loss.
            observed = [t for t, _ in received if fault["started_at"] <= t <= fault["recovered_at"]]
            assert observed and observed[0] - fault["started_at"] < 1.5
            assert fault["recovered_at"] - observed[-1] < 1.5
            assert all(b - a < 1.5 for a, b in zip(observed, observed[1:], strict=False))
        result[name] = {
            "transmitted": int(counts[1]),
            "received": len(received),
            "span_seconds": received[-1][0] - received[0][0],
            "gaps_over_1_5_seconds": len(gaps),
        }
    return result


def channels(directory, packets):
    observations = [
        json.loads(line) for line in (directory / "manager.jsonl").read_text().splitlines()
    ]
    preference, selection, operating = [], [], []
    for p in packets:
        if p["source"] == CONTROLLER and p["kind"] in (0x8004, 0x8006):
            replies = [
                r
                for r in packets
                if r["source"] == AGENT
                and r["kind"] == p["kind"] + 1
                and r["mid"] == p["mid"]
                and 0 <= r["time"] - p["time"] <= 1
            ]
            assert replies, "channel query/selection missing timely response"
            if p["kind"] == 0x8004:
                assert replies[0]["tlvs"] == []  # Implicit preference 15.
                preference.append(p["frame"])
            else:
                assert value(replies[0], 0x8E) == bytes.fromhex(RADIO) + b"\0"
                selection.append(p["frame"])
        if p["source"] == AGENT and p["kind"] == 0x8008:
            data = value(p, 0x8F)
            assert len(data) == 10 and data[:9] == bytes.fromhex(RADIO) + bytes((1, 81, 6))
            candidates = [
                o
                for o in observations
                if 0 <= p["time"] - o.get("stations", {}).get("timestamp_ms", 0) / 1000 <= 2
                and o.get("observation", {}).get("tx_power_dbm") is not None
            ]
            assert candidates, "no fresh independent operating-power observation"
            latest = max(candidates, key=lambda o: o["stations"]["timestamp_ms"])
            assert (
                int.from_bytes(data[9:], "big", signed=True)
                == latest["observation"]["tx_power_dbm"]
            )
            assert any(
                a["source"] == CONTROLLER
                and a["kind"] == 0x8000
                and a["mid"] == p["mid"]
                and 0 <= a["time"] - p["time"] <= 1
                and not a["tlvs"]
                for a in packets
            )
            operating.append(p["frame"])
    assert preference and selection and operating
    return {
        "preference_queries": len(preference),
        "accepted_selections": len(selection),
        "acknowledged_measured_operating_reports": len(operating),
    }


def check(directory, tshark, *, minimum_seconds=900):
    active = ACTIVE["check"](directory, tshark, recovery=True)
    result = read(directory / "result.json")
    assert result["active_seconds_requested"] >= minimum_seconds
    assert result["active_observed_seconds"] >= result["active_seconds_requested"]
    faults = read(directory / "recovery-checks.json")
    assert [f["kind"] for f in faults] == ["pod_connection_loss", "adapter_sigkill"]
    assert result["recovery_checks_requested"] and result["recovery_checks_completed"] == 2
    assert all(not f["manual_intervention"] for f in faults)
    final = read(directory / "native-operation.json")
    assert final["operation_count"] == len(final["operations"]) == 3
    assert final["journal_write_attempts"] == sum(o["attempts"] for o in final["operations"]) == 1
    assert final["writes"] == 0  # The restarted process submitted no writes.
    assert len({o["receipt"]["m1_sha256"] for o in final["operations"]}) == 3
    packets = BASE["packets"](directory / "ethernet.pcap")
    evidence = []
    for index, fault in enumerate(faults):
        before, after = fault["before"], fault["after"]
        assert before["operations"] == final["operations"][: index + 1]
        assert after["operations"] == final["operations"][: index + 2]
        assert 0 <= fault["restored_at"] - fault["started_at"] <= 15
        assert 0 <= fault["recovered_at"] - fault["restored_at"] <= 60
        op = after["operations"][-1]
        assert op["state"] == "OBSERVED_APPLIED" and op["attempts"] == 0
        assert op["application_evidence"]["observed_noop"]
        assert op["initiating_interface"] == "wsc-component"
        assert fault["clients"]["clients"] == "passed"
        session = fault["session_after"]
        assert session["state"] == "provisioning"
        assert session["worker"]["pid"] == fault["new_pid"]
        if index == 0:
            assert fault["old_pid"] == fault["new_pid"]
            assert fault["session_unavailable"]["state"] == "recovering"
            assert session["recovery"]["attempts_started"] == 2
            assert (
                session["worker"]["process_id"] == fault["session_before"]["worker"]["process_id"]
            )
        else:
            assert fault["exit_code"] == -9 and fault["old_pid"] != fault["new_pid"]
            assert (
                session["worker"]["process_id"] != fault["session_before"]["worker"]["process_id"]
            )
            assert session["recovery"]["attempts_started"] == 1
        window = [
            p
            for p in packets
            if fault["restored_at"] - 0.1 <= p["time"] <= fault["recovered_at"] + 0.1
        ]
        m1 = next(
            p
            for p in window
            if p["source"] == AGENT
            and p["kind"] == 9
            and hashlib.sha256(value(p, 0x11)).hexdigest() == op["receipt"]["m1_sha256"]
        )
        early = next(p for p in window if p["source"] == AGENT and p["kind"] == 0x8043)
        response = next(p for p in window if p["source"] == CONTROLLER and p["kind"] == 8)
        search = next(
            p
            for p in window
            if p["source"] == AGENT and p["kind"] == 7 and p["mid"] == response["mid"]
        )
        m2 = next(
            p
            for p in window
            if p["source"] == CONTROLLER
            and p["kind"] == 9
            and p["mid"] == op["receipt"]["first_mid"]
        )
        assert search["frame"] < response["frame"] < early["frame"] < m1["frame"] < m2["frame"]
        assert value(m2, 0x82).hex() == op["receipt"]["ruid"] == RADIO
        evidence.append(
            {
                "kind": fault["kind"],
                "seconds_until_recovered": fault["recovered_at"] - fault["started_at"],
                "m1_frame": m1["frame"],
                "m2_frame": m2["frame"],
                "new_config_writes": 0,
            }
        )
    samples = read(directory / "active-samples.json")
    assert active["native_connected_observations"] == len(samples)
    assert samples[-1]["elapsed"] >= result["active_seconds_requested"] - 12
    assert samples[0]["elapsed"] < 10
    assert [
        p
        for i, p in enumerate(s["adapter_pid"] for s in samples)
        if i == 0 or p != samples[i - 1]["adapter_pid"]
    ] == [faults[0]["old_pid"], faults[1]["new_pid"]]
    assert active["rss_kib_range"][1] - active["rss_kib_range"][0] < 32 * 1024
    assert active["file_descriptor_range"][1] <= 32
    probes = traffic(directory, faults, require_outages=minimum_seconds >= 900)
    assert all(p["span_seconds"] >= minimum_seconds - 3 for p in probes.values())
    return {
        "operational_recovery_checks_passed": True,
        "active_seconds": result["active_observed_seconds"],
        "active_client_pilot": active,
        "recoveries": evidence,
        "channels": channels(directory, packets),
        "continuous_traffic": probes,
        "unanswered_controller_requests": {
            f"0x{kind:04x}": [
                p["frame"]
                for p in packets
                if p["source"] == CONTROLLER
                and p["kind"] == kind
                and not any(
                    r["source"] == AGENT
                    and r["kind"] == response
                    and r["mid"] == p["mid"]
                    and 0 <= r["time"] - p["time"] <= 1
                    for r in packets
                )
            ]
            for kind, response in ((0x8003, 0x8000), (0x0005, 0x0006))
        },
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
        "remaining": [
            "reporting policy and qualified metrics measurements",
            "IEEE 1905 neighbor link metrics",
            "final disassociation statistics and reason",
            "complete acceptance criteria in doc/protocol/sustained-operation.md",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tshark", default="tshark")
    parser.add_argument("--minimum-seconds", type=int, default=900)
    args = parser.parse_args()
    print(
        json.dumps(
            check(args.directory, args.tshark, minimum_seconds=args.minimum_seconds), indent=2
        )
    )
