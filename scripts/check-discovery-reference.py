"""Independent tshark check of owned discovery-to-topology socket captures.

No EMOSA imports. Packet fields and received bytes are cross-checks, not native
controller acceptance or a resolution of the Table 117 specification conflict.
"""

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def values(packet, name):
    return [f.attrib["value"] for f in packet.findall(f".//field[@name='{name}']")]


def tlvs(packet):
    result = {}
    for row in packet.iter("field"):
        kind = row.find("field[@name='ieee1905.tlv_type']")
        if kind is None:
            continue
        kind = int(kind.attrib["value"], 16)
        assert kind not in result
        raw = bytes.fromhex(row.attrib["value"])
        assert len(raw) == 3 + (int.from_bytes(raw[1:3], "big") & 0x3FFF)
        result[kind] = raw[3:]
    return result


def check(tshark, directory):
    packets, digests = {}, {}
    for side in ("left", "right"):
        path = directory / (side + ".pcap")
        data = subprocess.check_output([tshark, "-n", "-r", str(path), "-T", "pdml"], timeout=30)
        packets[side] = ET.fromstring(data).findall("packet")
        digests[side] = hashlib.sha256(path.read_bytes()).hexdigest()
    left, right = packets["left"], packets["right"]

    def sequence(items):
        return [
            (values(p, "ieee1905.message_type")[0], int(values(p, "ieee1905.message_id")[0], 16))
            for p in items
        ]

    assert sequence(left) == [
        ("0008", 2),
        ("0002", 710),
        ("0008", 3),
        ("0002", 711),
        ("0002", 712),
        ("0002", 713),
        ("0002", 714),
    ]
    assert sequence(right) == [
        ("0007", 1),
        ("0007", 2),
        ("0007", 3),
        ("0003", 711),
        ("0003", 712),
        ("0003", 713),
    ]
    for packet in left:
        assert values(packet, "eth.src") == ["020000004002"]
        assert values(packet, "eth.dst") == ["020000004001"]
    for packet in right:
        assert values(packet, "eth.src") == ["020000004001"]
    for packet in right[:3]:
        assert values(packet, "eth.dst") == ["0180c2000013"]
        fields = tlvs(packet)
        assert fields[1] == bytes.fromhex("020000004001")
        assert fields[0x0D] == fields[0x0E] == b"\0"
        assert fields[0x80] == b"\x01\x01" and fields[0x81] == b"\x01\0"
        assert fields[0xB3] == b"\x01" and 0xB4 in fields
    first, second = tlvs(left[0]), tlvs(left[2])
    assert first[0xDD] == b"\x40" and 0xA9 not in first
    assert second[0xDD] == b"\xc0" and second[0xA9] == bytes(3)
    assert first[0xB3] == second[0xB3] == b"\x01"
    for packet, ssid in zip(
        right[3:], ["EMOSA-report-before"] * 2 + ["EMOSA-report-after"], strict=True
    ):
        assert values(packet, "eth.dst") == ["020000004002"]
        prefix = "ieee1905.bss_config_report."
        assert values(packet, prefix + "ssid") == [ssid.encode().hex()]
        assert values(packet, prefix + "radio_id") == ["020000004010"]
        assert values(packet, prefix + "mac_addr") == ["020000004011"]
    return {
        "directory": directory.name,
        "sha256": digests,
        "received_frames": [len(left), len(right)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, action="append", required=True)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    checks = [check(args.tshark, path) for path in args.directory]
    print(
        json.dumps(
            {
                "passed": True,
                "checks": checks,
                "dissector": subprocess.check_output(
                    [args.tshark, "--version"], text=True
                ).splitlines()[0],
                "scope": "synthetic received packet fields; no native onboarding claim",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
