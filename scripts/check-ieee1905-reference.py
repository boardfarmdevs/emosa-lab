"""Independent tshark envelope projection of the retained native Ethernet capture.

No EMOSA imports and no raw TLV values are published. The selected versions are
cross-checks of header/TLV boundaries, not normative replacements or proof of
complete EasyMesh procedures.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/protocol/ieee1905"
CAPTURE = ROOT / "doc/evidence/peer-baseline/samples/wired/ethernet.pcap"
CAPTURE_SHA = "6cf3010c059058c6c8a9abeb39bc6eedf46c68cca65c8e8e0010694c0c36b964"


def field(packet, name):
    found = packet.findall(f".//field[@name='{name}']")
    if len(found) != 1:
        raise ValueError("expected one " + name)
    return found[0]


def project(pdml):
    output = []
    for packet in ET.fromstring(pdml).findall("packet"):
        row = {"frame": int(field(packet, "frame.number").attrib["show"])}
        for name in ("message_version", "message_type", "message_id", "fragment_id"):
            row[name] = int(field(packet, "ieee1905." + name).attrib["value"], 16)
        for name in ("last_fragment", "relay_indicator"):
            row[name] = field(packet, "ieee1905." + name).attrib["show"] in ("1", "True")
        for name in ("src", "dst"):
            row[name] = bytes.fromhex(field(packet, "eth." + name).attrib["value"]).hex(":")
        tlvs = []
        for item in packet.iter("field"):
            kind = item.find("field[@name='ieee1905.tlv_type']")
            if kind is None or int(kind.attrib["value"], 16) == 0:
                continue
            length = item.find("field[@name='ieee1905.tlv_length']")
            tlvs.append(
                {
                    "type": f"0x{int(kind.attrib['value'], 16):02x}",
                    "length": int(length.attrib["value"], 16) & 0x3FFF,
                }
            )
        row["tlvs"] = tlvs
        output.append(row)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tshark", default="tshark")
    parser.add_argument("--record", action="store_true", help="create an absent reviewed fixture")
    args = parser.parse_args()
    assert hashlib.sha256(CAPTURE.read_bytes()).hexdigest() == CAPTURE_SHA
    tool = shutil.which(args.tshark)
    if tool is None:
        raise SystemExit("tshark required")
    result = subprocess.run(
        [tool, "-n", "-2", "-r", str(CAPTURE), "-Y", "ieee1905", "-T", "pdml"],
        capture_output=True,
        check=True,
        timeout=30,
    )
    rows = project(result.stdout)
    target = FIXTURE / "native-envelope.json"
    if args.record:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as stream:
            stream.write(json.dumps(rows, indent=2) + "\n")
    assert rows == json.loads(target.read_text()), "independent envelope projection changed"
    print(
        json.dumps(
            {
                "passed": True,
                "frames": len(rows),
                "capture_sha256": CAPTURE_SHA,
                "dissector": subprocess.check_output([tool, "--version"], text=True).splitlines()[
                    0
                ],
                "dissector_sha256": hashlib.sha256(Path(tool).read_bytes()).hexdigest(),
                "fixture_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "scope": "native header and TLV boundary cross-check; no onboarding claim",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
