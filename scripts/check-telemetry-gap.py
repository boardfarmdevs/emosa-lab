"""Check an owned telemetry-only outage without importing EMOSA implementation.

Correlates worker availability with manager observations, captured wire messages,
durable operations and independent client traffic. This is a focused freshness
and recovery check, not complete sustained-operation or telemetry qualification.
"""

import argparse
import json
import runpy
from pathlib import Path

BASE = runpy.run_path(str(Path(__file__).with_name("check-native-onboarding.py")))
HEALTH = runpy.run_path(str(Path(__file__).with_name("check-capture-health.py")))["check"]


def check(directory):
    def read(name):
        return json.loads((directory / name).read_text())

    health = HEALTH(directory)
    result = read("result.json")
    assert result["telemetry_gap_check_requested"] and result["telemetry_gap_check_completed"]
    assert not result["cleanup_errors"]
    gap = read("telemetry-gap-check.json")
    before, during, after = (gap["session_" + key] for key in ("before", "withheld", "after"))
    assert 4 <= gap["restored_at"] - gap["started_at"] < 20
    assert gap["restored_at"] <= gap["completed_at"] < gap["restored_at"] + 5
    for session in (before, during, after):
        assert session["state"] == "provisioning"
        assert session["report_source"]["available"], "control authority lost during telemetry gap"
        assert session["report_source"]["context_token"] == before["report_source"]["context_token"]
        assert session["worker"] == before["worker"]
        assert session["recovery"]["attempts_started"] == 1
        assert session["counts"]["search_sent"] == 1
        assert session["counts"].get("client_leave_notification", 0) == 0
    assert before["report_source"]["inventory_complete"]
    assert after["report_source"]["inventory_complete"]
    assert before["telemetry"]["station_count"] == after["telemetry"]["station_count"] == 1
    assert before["report_source"]["operating_radio_count"] == 1
    assert after["report_source"]["operating_radio_count"] == 1
    assert during["telemetry"]["timestamp_ms"] is None
    assert not during["report_source"]["inventory_complete"], "stale inventory remained available"
    assert during["report_source"]["operating_radio_count"] == 0
    assert after["telemetry"]["timestamp_ms"] > before["telemetry"]["timestamp_ms"]
    assert gap["operation_before"] == gap["operation_after"], "telemetry gap changed operations"
    assert gap["operation_after"]["operation_count"] == 1
    assert gap["operation_after"]["journal_write_attempts"] == 1
    observations = [
        json.loads(line) for line in (directory / "manager.jsonl").read_text().splitlines()
    ]
    withheld = [o for o in observations if o.get("telemetry_withheld")]
    assert len(withheld) >= 4
    assert withheld[-1]["monotonic"] - withheld[0]["monotonic"] >= 3
    assert all(
        o["publication"] == "observed-state" and "telemetry_published" not in o for o in withheld
    )
    assert all(o["stations"]["clients"] for o in withheld)
    resumed = [
        o
        for o in observations
        if o["monotonic"] > withheld[-1]["monotonic"] and o.get("telemetry_published")
    ]
    assert resumed, "no telemetry publication after restoring manager policy"
    packets = BASE["packets"](directory / "ethernet.pcap")
    window = [p for p in packets if gap["started_at"] <= p["time"] <= gap["completed_at"]]
    assert not any(p["source"] == BASE["AGENT"] and p["kind"] in (7, 9) for p in window)
    assert not any(
        kind == 0x92 and len(value) == 13 and value[-1] == 0
        for p in window
        if p["source"] == BASE["AGENT"]
        for kind, value in p["tlvs"]
    ), "fabricated departure while telemetry was withheld"
    clients = read("clients-telemetry-withheld.json")
    for name, interface in (("em-baseline-wired", "eth1"), ("em-baseline-wifi", "wlan0")):
        client = clients["observations"][name]
        assert client["interface"] == interface
        assert client["application"]["nonce"] == clients["nonce"]
        assert BASE["zero_packet_loss"](client["ping"])
    assert clients["observations"]["em-baseline-wifi"]["supplicant"]["wpa_state"] == "COMPLETED"
    return {
        "telemetry_gap_checks_passed": True,
        "capture_health": health,
        "withheld_seconds": gap["restored_at"] - gap["started_at"],
        "observed_manager_cycles_without_telemetry": len(withheld),
        "control_context_preserved": True,
        "stale_inventory_and_operating_radio_withdrawn": True,
        "false_departures": 0,
        "new_onboarding_operations": 0,
        "new_config_writes": 0,
        "wired_and_wifi_traffic_during_gap": True,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), indent=2))
