"""Independent byte/layout audit of explicit synthetic AP reporting vectors.

No EMOSA imports. Literal values check complete policy-dependent composition,
wire widths/order, sparse ESP fields and missing-versus-empty queue inventory.
Opaque ESP/Data Elements values do not establish source conversion semantics.
"""

import argparse
import hashlib
import json
import runpy
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKETS = runpy.run_path(str(ROOT / "scripts/check-native-onboarding.py"))["packets"]


def check(directory, tshark="tshark"):
    capture = directory / "synthetic-ap-metrics.pcap"
    packets = PACKETS(capture)
    assert len(packets) == 8
    assert [(p["kind"], p["mid"]) for p in packets] == [
        (0x8003, 11),
        (0x8000, 11),
        (0x800B, 71),
        (0x800C, 71),
        (0x800C, 101),
        (0x800B, 72),
        (0x800B, 73),
        (0x800C, 73),
    ]
    radio = bytes.fromhex("020000004010")
    bss = bytes.fromhex("020000004011")
    station = bytes.fromhex("020000004020")
    assert packets[0]["tlvs"] == [(0x8A, b"\x3c\1" + radio + b"\0\0\0\xe0")]
    assert packets[1]["tlvs"] == []
    expected = [
        (0x94, bss + bytes.fromhex("7b000190010203023040")),
        (0xC7, bss + struct.pack("!6I", 0x01020304, 2, 3, 4, 5, 0xFFFFFFFF)),
        (0xC6, radio + bytes((160, 21, 22, 23))),
        (0xA2, station + struct.pack("!7I", 2049, 1002, 3, 4, 5, 6, 7)),
        (0x96, station + b"\1" + bss + struct.pack("!3IB", 125, 65, 32, 120)),
        (0xC8, station + b"\1" + bss + struct.pack("!4I", 65000, 32000, 101, 202)),
        (0xB0, station + bytes.fromhex("0207ff0001")),
    ]
    assert packets[3]["tlvs"] == packets[4]["tlvs"] == expected
    assert packets[7]["tlvs"] == [v for v in expected if v[0] not in (0xC6, 0xB0)] + [
        (0xB0, station + b"\0")
    ]
    for p in packets:
        request = p["kind"] in (0x8003, 0x800B)
        assert (p["source"], p["destination"]) == (
            ("020000004002", "020000004001") if request else ("020000004001", "020000004002")
        )
    decoded = ET.fromstring(
        subprocess.check_output(
            [tshark, "-n", "-r", str(capture), "-T", "pdml"],
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    ).findall("packet")
    assert len(decoded) == 8
    for index, packet in enumerate(decoded):
        assert not packet.findall(".//proto[@name='_ws.malformed']")
        fields = packet.findall(".//field[@name='ieee1905.tlv_type']")
        assert [int(f.attrib["value"], 16) for f in fields] == [
            t for t, _ in packets[index]["tlvs"]
        ] + [0]
        if index not in (3, 4, 7):
            continue
        for name, expected_value in {
            "ap_metrics.bssid": bss.hex(),
            "ap_metrics.channel_util": "7b",
            "ap_metrics.sta_count": "0001",
            "ap_metrics.est_param_be": "010203",
            "ap_metrics.est_param_vi": "023040",
        }.items():
            (field,) = packet.findall(f".//field[@name='ieee1905.{name}']")
            assert field.attrib["value"] == expected_value
        assert not packet.findall(".//field[@name='ieee1905.ap_metrics.est_param_bk']")
        assert not packet.findall(".//field[@name='ieee1905.ap_metrics.est_param_vo']")
        integers = {
            "ap_extended_metrics.unicast_bytes_sent": 0x01020304,
            "ap_extended_metrics.unicast_bytes_received": 2,
            "ap_extended_metrics.multicast_bytes_sent": 3,
            "ap_extended_metrics.multicast_bytes_received": 4,
            "ap_extended_metrics.Broadcast_bytes_sent": 5,
            "ap_extended_metrics.broadcast_bytes_received": 0xFFFFFFFF,
            "assoc_sta_traffic_stats.bytes_sent": 2049,
            "assoc_sta_traffic_stats.bytes_rcvd": 1002,
            "assoc_sta_traffic_stats.packets_sent": 3,
            "assoc_sta_traffic_stats.packets_rcvd": 4,
            "assoc_sta_traffic_stats.tx_pkt_errs": 5,
            "assoc_sta_traffic_stats.rx_packet_errs": 6,
            "assoc_sta_traffic_stats.retrans_count": 7,
            "assoc_sta_link_metrics.time_delta": 125,
            "assoc_sta_link_metrics.down_rate": 65,
            "assoc_sta_link_metrics.up_rate": 32,
            "assoc_sta_link_metrics.rcpi": 120,
            "assoc_sta_extended_link_metrics.lddlr": 65000,
            "assoc_sta_extended_link_metrics.ldulr": 32000,
            "assoc_sta_extended_link_metrics.ur": 101,
            "assoc_sta_extended_link_metrics.ut": 202,
            "assoc_wf6_sta_status_report.tid_count": 0 if index == 7 else 2,
        }
        if index != 7:
            integers.update(
                {
                    "radio_metrics.noise": 160,
                    "radio_metrics.transmit": 21,
                    "radio_metrics.receive_self": 22,
                    "radio_metrics.receive_other": 23,
                }
            )
        for name, value in integers.items():
            fields = packet.findall(f".//field[@name='ieee1905.{name}']")
            if name == "assoc_sta_link_metrics.rcpi":
                # Wireshark 3.6 used an RSSI label for the same RCPI wire octet.
                fields += packet.findall(".//field[@name='ieee1905.assoc_sta_link_metrics.rssi']")
            assert len(fields) == 1, name
            (field,) = fields
            assert int(field.attrib["value"], 16) == value
        for name, values in (("tid", [7, 0]), ("queue_size", [255, 1])):
            fields = packet.findall(
                f".//field[@name='ieee1905.assoc_wf6_sta_status_report.{name}']"
            )
            assert [int(f.attrib["value"], 16) for f in fields] == ([] if index == 7 else values)
    result = json.loads((directory / "result.json").read_text())
    schedule = result["schedule"]
    assert schedule["reports_transmitted"] == schedule["periods_due_without_report"] == 1
    assert schedule["next_due"] == 190 and schedule["last_unfulfilled_due"] == 130
    assert result["restart_extra_frames"] == 0
    assert all(
        schedule["policy"]["metrics"]["radios"][0][k]
        for k in ("include_traffic", "include_link", "include_wifi6_status")
    )
    assert not result["measurement_source_qualified"] and not result["native_ap_reporting_proven"]
    return {
        "passed": True,
        "scope": "synthetic composition and scheduler output audit",
        "decoded_reports": 3,
        "unanswered_incomplete_measurement_queries": [72],
        "sparse_esp_order_checked": True,
        "empty_known_queue_inventory_checked": True,
        "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        "dissector": subprocess.check_output([tshark, "--version"], text=True).splitlines()[0],
        "field_conversion_qualified": False,
        "native_ap_reporting_proven": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    print(json.dumps(check(args.directory, args.tshark), indent=2))
