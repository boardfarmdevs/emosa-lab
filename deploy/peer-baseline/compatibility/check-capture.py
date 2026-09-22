"""Recheck the published candidate capture with tshark; no EMOSA imports or traffic.

The older dissector calls the MCS length nibble reserved. Compare raw bytes, not
that obsolete label. This verifies the narrow native experiment, not conformance.
"""

import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CAPTURE = ROOT / "doc/evidence/native-compatibility/ethernet.pcap"
EXPECTED_SHA256 = "331e9f4a58cc78e5c84036d3e24087ebad31891f03c5cb2c45173e145dd2997f"
EXPECTED_WIFI6 = "020000ec020002040000004003221212d0440000004003221212d0"


def field(packet, name):
    result = list(packet.iterfind(f".//field[@name='{name}']"))
    if len(result) != 1:
        raise ValueError("Expected one independently dissected field: " + name)
    return result[0]


def main():
    if hashlib.sha256(CAPTURE.read_bytes()).hexdigest() != EXPECTED_SHA256:
        raise SystemExit("Capture differs from reviewed synthetic candidate evidence")
    tshark = shutil.which("tshark")
    if not tshark:
        raise SystemExit("Install tshark to reproduce the independent check")
    command = [tshark, "-r", str(CAPTURE), "-Y", "ieee1905", "-T", "pdml"]
    pdml = subprocess.run(command, capture_output=True, check=True, timeout=30).stdout
    records = []
    for packet in ET.fromstring(pdml).findall("packet"):
        number = int(field(packet, "frame.number").get("show"))
        if number not in (1, 2, 3, 6, 21):
            continue
        record = {"frame": number, "wifi6_values": [], "profiles": [], "wps_types": []}
        for tlv in packet.iter("field"):
            kind = tlv.find("field[@name='ieee1905.tlv_type']")
            if kind is None:
                continue
            if int(kind.get("value"), 16) == 0xAA:
                header = int(kind.get("size")) + int(field(tlv, "ieee1905.tlv_length").get("size"))
                value = bytes.fromhex(tlv.get("value"))[header:]
                if len(value) != int(field(tlv, "ieee1905.tlv_length").get("show")):
                    raise ValueError("Dissector value length mismatch")
                record["wifi6_values"].append(value.hex())
        record["profiles"] = [
            int(f.get("value"), 16)
            for f in packet.iterfind(".//field[@name='ieee1905.multi_ap_version']")
        ]
        record["wps_types"] = [
            int(f.get("value"), 16) for f in packet.iterfind(".//field[@name='wps.message_type']")
        ]
        records.append(record)
    by_frame = {r["frame"]: r for r in records}
    if set(by_frame) != {1, 2, 3, 6, 21}:
        raise ValueError("Selected native frames missing")
    if by_frame[1]["profiles"] != [2] or by_frame[2]["profiles"] != [1]:
        raise ValueError("The retained profile mismatch was not reproduced")
    if by_frame[6]["wps_types"] != [5]:
        raise ValueError("Expected exactly one M2 and no M8 in the selected request")
    if any(by_frame[f]["wifi6_values"] != [EXPECTED_WIFI6] for f in (3, 21)):
        raise ValueError("Expected four-byte lengths for both native AP/STA roles")
    print(
        json.dumps(
            {
                "passed": True,
                "capture_sha256": EXPECTED_SHA256,
                "dissector": subprocess.run(
                    [tshark, "--version"], capture_output=True, text=True, check=True, timeout=10
                ).stdout.splitlines()[0],
                "records": records,
                "full_protocol_conformance": False,
                "mcs_value_ordering_qualified": False,
                "emosa_wire_onboarding": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
