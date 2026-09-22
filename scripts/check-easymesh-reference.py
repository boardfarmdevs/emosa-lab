"""Recheck selected native-peer TLV values with tshark, without importing EMOSA.

Only the hash-pinned, already published synthetic capture is accepted. The
dissector determines field boundaries and reassembly. This is an implementation
cross-check, not a normative substitute or validation of complete IEEE messages.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/protocol/easymesh"
DISPLAY_FILTER = " || ".join(f"frame.number == {number}" for number in (1, 2, 5, 7, 20))


def field(element, name):
    values = list(element.iterfind(f".//field[@name='{name}']"))
    if len(values) != 1:
        raise ValueError("expected one dissector field")
    return values[0]


def address(element, name):
    return bytes.fromhex(field(element, name).attrib["value"]).hex(":")


def projection(pdml):
    """Project independently dissected field values, not EMOSA's interpretation."""
    cases = []
    for packet in ET.fromstring(pdml).findall("packet"):
        frame = int(field(packet, "frame.number").attrib["show"])
        for tlv in packet.iter("field"):
            kind_field = tlv.find("field[@name='ieee1905.tlv_type']")
            if kind_field is None:
                continue
            kind = int(kind_field.attrib["value"], 16)
            if kind not in (0x80, 0x81, 0x82, 0x83, 0x85, 0xB3):
                continue
            length_field = field(tlv, "ieee1905.tlv_length")
            # Sizes come from the independent decoder, including on reassembled
            # frame 7. Parent/child pos attributes use different coordinate origins.
            header_size = int(kind_field.attrib["size"]) + int(length_field.attrib["size"])
            raw = bytes.fromhex(tlv.attrib["value"])[header_size:]
            if len(raw) != int(length_field.attrib["show"]):
                raise ValueError("dissector value length mismatch")
            if kind in (0x80, 0x81):
                prefix = "supported" if kind == 0x80 else "searched"
                decoded = {
                    "services": [
                        int(f.attrib["value"], 16)
                        for f in tlv.iterfind(
                            f".//field[@name='ieee1905.{prefix}_service.service']"
                        )
                    ]
                }
            elif kind == 0x82:
                decoded = {"ruid": address(tlv, "ieee1905.ap_radio_identifier")}
            elif kind == 0xB3:
                decoded = {"profile": int(field(tlv, "ieee1905.multi_ap_version").attrib["show"])}
            elif kind == 0x85:
                parents = {child: parent for parent in tlv.iter() for child in parent}
                classes = []
                for op in tlv.iterfind(".//field[@name='ieee1905.radio_basic.op_class']"):
                    parent = parents[op]
                    classes.append(
                        {
                            "operating_class": int(op.attrib["show"]),
                            "max_eirp_dbm": int(
                                field(parent, "ieee1905.radio_basic.max_power").attrib["show"]
                            ),
                            "non_operable_channels": [
                                int(f.attrib["show"])
                                for f in parent.iterfind(
                                    ".//field[@name='ieee1905.radio_basic.non_op_channel']"
                                )
                            ],
                        }
                    )
                decoded = {
                    "ruid": address(tlv, "ieee1905.ap_radio_identifier"),
                    "max_bss": int(field(tlv, "ieee1905.radio_basic_cap.max_bss").attrib["show"]),
                    "operating_classes": classes,
                }
            else:
                parents = {child: parent for parent in tlv.iter() for child in parent}
                radios = []
                for ruid in tlv.iterfind(".//field[@name='ieee1905.ap_radio_identifier']"):
                    radio = parents[ruid]
                    bsses = []
                    for mac in radio.iterfind(".//field[@name='ieee1905.ap_bss_local_intf_addr']"):
                        bss = parents[mac]
                        bsses.append(
                            {
                                "ap_mac": address(bss, "ieee1905.ap_bss_local_intf_addr"),
                                "ssid_hex": field(bss, "ieee1905.ap_bss_local_intf_ssid")
                                .attrib["value"]
                                .lower(),
                            }
                        )
                    radios.append(
                        {"ruid": bytes.fromhex(ruid.attrib["value"]).hex(":"), "bsses": bsses}
                    )
                decoded = {"radios": radios}
            cases.append(
                {
                    "frame": frame,
                    "type": f"0x{kind:02x}",
                    "value_hex": raw.hex(),
                    "decoded": decoded,
                }
            )
    return cases


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    capture = ROOT / provenance["source_capture"]
    checks = [
        (capture, provenance["capture_sha256"]),
        (FIXTURE / "native-values.json", provenance["vectors_sha256"]),
        (Path(__file__), provenance["extractor_sha256"]),
    ]
    if any(digest(path) != expected for path, expected in checks):
        raise SystemExit("retained reference input digest mismatch")
    executable = shutil.which(args.tshark)
    if executable is None:
        raise SystemExit("tshark is required for the independent reference check")
    version = subprocess.run(
        [executable, "--version"], check=True, capture_output=True, text=True, timeout=10
    ).stdout.splitlines()[0]
    result = subprocess.run(
        [executable, "-n", "-2", "-r", str(capture), "-Y", DISPLAY_FILTER, "-T", "pdml"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    cases = projection(result.stdout)
    if cases != json.loads((FIXTURE / "native-values.json").read_text())["cases"]:
        raise SystemExit("independent dissector output differs from retained values")
    print(
        json.dumps(
            {
                "status": "match",
                "scope": "selected native-peer value bytes and fields only",
                "cases": len(cases),
                "dissector": version,
                "dissector_sha256": digest(Path(executable).resolve()),
                "capture_sha256": digest(capture),
                "vectors_sha256": provenance["vectors_sha256"],
                "extractor_sha256": provenance["extractor_sha256"],
                "emosa_imported": False,
                "full_protocol_conformance": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
