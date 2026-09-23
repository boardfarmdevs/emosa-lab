"""Independently check native policy receipt/Ack and expose missing reports.

This is deliberately not a complete policy/reporting acceptance checker.
It joins the actual native request bytes, timely wire Acks, durable policy
status and recovery snapshots without importing the implementation.
"""

import argparse
import json
import runpy
from pathlib import Path

BASE = runpy.run_path(str(Path(__file__).with_name("check-native-onboarding.py")))
HEALTH = runpy.run_path(str(Path(__file__).with_name("check-capture-health.py")))["check"]
AGENT, CONTROLLER, RADIO = BASE["AGENT"], BASE["CONTROLLER"], BASE["RADIO"]


def check(directory):
    def read(name):
        return json.loads((directory / name).read_text())

    health = HEALTH(directory)
    result = read("result.json")
    assert result["reporting_policy_receipt_expected"] and not result["cleanup_errors"]
    packets = BASE["packets"](directory / "ethernet.pcap")
    requests = [p for p in packets if p["source"] == CONTROLLER and p["kind"] == 0x8003]
    assert len(requests) == 3
    matches = []
    for request in requests:
        # Literal selected Table 35 / 34 / 115 values independently checked in
        # the native request. All three requested STA inclusion bits remain on.
        assert request["tlvs"] == [
            (0x8A, bytes.fromhex("3c01" + RADIO + "000000e0")),
            (0x89, bytes.fromhex("000001" + RADIO + "000000")),
            (0xDB, bytes(22)),
        ]
        responses = [
            p
            for p in packets
            if p["source"] == AGENT
            and p["destination"] == CONTROLLER
            and p["kind"] == 0x8000
            and p["mid"] == request["mid"]
            and 0 <= p["time"] - request["time"] < 1
        ]
        assert len(responses) == 1 and responses[0]["tlvs"] == []
        matches.append(
            {
                "request_frame": request["frame"],
                "ack_frame": responses[0]["frame"],
                "mid": request["mid"],
                "ack_delay_ms": (responses[0]["time"] - request["time"]) * 1000,
            }
        )
    state = read("native-session.json")["reporting_policy"]
    assert not state["required_reporting_proven"] and not state["policy_application_proven"]
    final = state["received_policy"]
    assert final["identity"] == {"controller": CONTROLLER, "local_al": AGENT, "ruid": RADIO}
    assert final["receipt_count"] == len(requests) and final["schedule_rebases"] == 0
    assert final["latest_mid"] == requests[-1]["mid"]
    assert final["latest_request"] == [
        {"kind": kind, "value": value.hex()} for kind, value in requests[-1]["tlvs"]
    ]
    assert final["policy"] == {
        "metrics": {
            "interval_seconds": 60,
            "radios": [
                {
                    "ruid": RADIO,
                    "rcpi_threshold": 0,
                    "rcpi_hysteresis_db": 0,
                    "utilization_threshold": 0,
                    "include_traffic": True,
                    "include_link": True,
                    "include_wifi6_status": True,
                }
            ],
        },
        "steering": {
            "local_disallowed": [],
            "btm_disallowed": [],
            "radios": [
                {
                    "ruid": RADIO,
                    "policy": 0,
                    "utilization_threshold": 0,
                    "rcpi_threshold": 0,
                }
            ],
        },
        "qos": [{"mscs_disallowed": [], "scs_disallowed": []}],
    }
    assert final["periods_due_without_report"] >= 3
    assert final["next_due"] - final["last_unfulfilled_due"] == 60
    origin = final["next_due"] - 60 * final["periods_due_without_report"]
    for fault in read("recovery-checks.json"):
        for name in ("session_before", "session_after"):
            saved = fault[name]["reporting_policy"]["received_policy"]
            assert saved["policy"] == final["policy"]
            assert saved["boot_id"] == final["boot_id"]
            assert abs(saved["next_due"] - saved["periods_due_without_report"] * 60 - origin) < 1e-6
    reports = [p["frame"] for p in packets if p["source"] == AGENT and p["kind"] == 0x800C]
    assert reports == [], "update qualification before promoting metrics delivery"
    return {
        "policy_receipt_checks_passed": True,
        "scope": "durable native policy receipt/Ack and recovery, not fulfilled reporting",
        "capture_health": health,
        "requests": matches,
        "interval_seconds": 60,
        "all_requested_sta_inclusions_preserved": True,
        "schedule_preserved_across_both_faults": True,
        "periods_due_without_report": final["periods_due_without_report"],
        "policy_application_proven": False,
        "required_reporting_proven": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
        "remaining": [
            "Qualified AP/STA measurements and required report delivery",
            "IEEE 1905 neighbor measurements and final disassociation records",
            "Complete 15-minute acceptance after reporting is implemented",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), indent=2))
