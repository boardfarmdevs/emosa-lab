"""Independent raw kernel-event/radio/EasyMesh leave correlation.

Standard library plus tshark, with no collector or EMOSA imports. This verifies
acquisition and correspondence, not the semantics of EasyMesh traffic counters.
"""

import argparse
import hashlib
import json
import runpy
import subprocess
from pathlib import Path

REFERENCE = runpy.run_path(str(Path(__file__).with_name("check-native-onboarding.py")))
STA, BSSID = "02:00:00:00:02:00", "02:00:00:ec:02:00"


def check(directory, tshark):
    stream = directory / "station-events.jsonl"
    records = [json.loads(s) for s in stream.read_text().splitlines()]
    metadata, end = records[0], records[-1]
    assert metadata["event"] == "ready" and end["event"] == "finished"
    assert not end["errors"] and not metadata["final_counter_source_qualified"]
    assert metadata["interface"] == "wlan0" and metadata["bssid"] == BSSID
    assert metadata["byteorder"] in ("little", "big")
    hashes = json.loads((directory / "source-hashes.json").read_text())
    assert hashes["/opt/emosa-radio-manager/station-events.py"] == metadata["collector_sha256"]
    result = json.loads((directory / "result.json").read_text())
    assert result["station_removal_observation_requested"]
    assert result["station_removal_observation"] == end and not result["cleanup_errors"]
    assert all(
        first["received_monotonic_ns"] <= second["received_monotonic_ns"]
        and first["received_wall_ns"] <= second["received_wall_ns"]
        for first, second in zip(records[:-1], records[1:], strict=True)
    )
    active, counts, final = {}, {"new_station": 0, "del_station": 0}, []
    for event in records[1:-1]:
        kind = event["event"]
        counts[kind] += 1
        assert event["ifindex"] == metadata["ifindex"] and event["station"] == STA
        if kind == "new_station":
            assert STA not in active
            active[STA] = counts[kind]
        assert event["observed_lifetime"] == active[STA]
        if kind == "del_station":
            active.pop(STA)
            fields = event["observed_fields"]
            # Independently decode selected stable Linux UAPI attributes. These
            # are raw counters, deliberately not an EasyMesh conversion table.
            for ident, name, length in (
                (23, "rx_bytes64", 8),
                (24, "tx_bytes64", 8),
                (9, "rx_packets", 4),
                (10, "tx_packets", 4),
                (11, "tx_retries", 4),
                (12, "tx_failed", 4),
                (28, "rx_drop_misc", 8),
                (42, "assoc_at_boottime_ns", 8),
            ):
                assert ident in event["station_info_ids"]
                raw = bytes.fromhex(event["raw_station_info"][str(ident)])
                assert len(raw) == length
                assert int.from_bytes(raw, metadata["byteorder"]) == fields[name]
            assert fields["rx_bytes64"] > 0 and fields["tx_bytes64"] > 0
            assert fields["rx_packets"] > 0 and fields["tx_packets"] > 0
            # Current kernel removal events have no 802.11 reason attribute.
            assert 54 not in event["attribute_ids"] and "reason_code" not in event
            final.append(event)
    assert counts == end["counts"] and len(final) >= 3
    assert end["remaining_observed_stations"] == sorted(active)
    association_times = [e["observed_fields"]["assoc_at_boottime_ns"] for e in final]
    assert all(t > 0 for t in association_times) and len(set(association_times)) == len(final)

    fields = ["frame.number", "frame.time_epoch", "wlan.fixed.reason_code", "wlan.seq"]
    decoded = subprocess.check_output(
        [
            tshark,
            "-n",
            "-r",
            str(directory / "radio.pcap"),
            "-Y",
            f"wlan.fixed.reason_code && wlan.sa == {STA}"
            f" && wlan.da == {BSSID} && wlan.bssid == {BSSID}",
            "-T",
            "fields",
            *[v for f in fields for v in ("-e", f)],
        ],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    disconnects = [line.split("\t") for line in decoded.splitlines()]
    ethernet = REFERENCE["packets"](directory / "ethernet.pcap")
    leaves = [
        p
        for p in ethernet
        if p["kind"] == 1
        and p["source"] == "020000003001"
        and any(
            k == 0x92 and v == bytes.fromhex(STA.replace(":", "") + BSSID.replace(":", "")) + b"\0"
            for k, v in p["tlvs"]
        )
    ]
    assert len(leaves) == len(final)
    correlations, used_radio, used_ethernet = [], set(), set()
    for event in final:
        timestamp = event["received_wall_ns"] / 1e9
        reason = [row for row in disconnects if 0 <= timestamp - float(row[1]) < 1]
        assert len(reason) == 1 and int(reason[0][2], 0) == 3
        assert reason[0][0] not in used_radio
        used_radio.add(reason[0][0])
        notification = [p for p in leaves if -0.05 <= p["time"] - timestamp < 2]
        assert len(notification) == 1 and notification[0]["frame"] not in used_ethernet
        used_ethernet.add(notification[0]["frame"])
        correlations.append(
            {
                "observed_lifetime": event["observed_lifetime"],
                "radio_frame": int(reason[0][0]),
                "actual_reason": 3,
                "kernel_delivery_delay_ms": round((timestamp - float(reason[0][1])) * 1000, 3),
                "leave_notification_frame": notification[0]["frame"],
                "raw_observed_fields": event["observed_fields"],
            }
        )
    return {
        "observation_checks_passed": True,
        "scope": "kernel final-station event acquisition and independent leave/reason correlation",
        "kernel": metadata["kernel"],
        "counts": counts,
        "correlations": correlations,
        "event_stream_sha256": hashlib.sha256(stream.read_bytes()).hexdigest(),
        "final_counter_source_qualified": False,
        "native_final_statistics_delivery_proven": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
        "remaining": [
            "Qualify successful-packet, byte, receive-error and retry-flag-packet semantics",
            "Integrate a qualified reason source and complete final counters with the sender",
            "Implement mandatory policy/AP/STA/neighbor metrics and full 15-minute acceptance",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    print(json.dumps(check(args.directory, args.tshark), indent=2))
