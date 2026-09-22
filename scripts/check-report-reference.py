"""Independent tshark field/value projection for the new report components.

Reads the hash-pinned native capture and optional public --reports traces.
No EMOSA imports.
Older dissector labels do not replace the selected IEEE/WFA field definitions.
"""

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/protocol/reports/native-values.json"
CAPTURE = ROOT / "doc/evidence/peer-baseline/samples/wired/ethernet.pcap"
CAPTURE_SHA = "6cf3010c059058c6c8a9abeb39bc6eedf46c68cca65c8e8e0010694c0c36b964"


def fields(element, name):
    return [f.attrib for f in element.findall(f".//field[@name='{name}']")]


def values(element, name):
    return [f["value"] for f in fields(element, name)]


def project(data):
    output = []
    for packet in ET.fromstring(data).findall("packet"):
        frame = int(fields(packet, "frame.number")[0]["show"])
        for tlv in packet.iter("field"):
            kind = tlv.find("field[@name='ieee1905.tlv_type']")
            if kind is None or int(kind.attrib["value"], 16) not in (3, 6, 0xB7, 0xCC):
                continue
            kind = int(kind.attrib["value"], 16)
            length = int(values(tlv, "ieee1905.tlv_length")[0], 16) & 0x3FFF
            raw = bytes.fromhex(tlv.attrib["value"])[3:]
            assert len(raw) == length
            if kind == 3:
                decoded = {
                    "al_mac": values(tlv, "ieee1905.1905_al_mac_addr")[0],
                    "interfaces": [
                        {"mac": mac, "media_type": int(media, 16), "media_length": int(size, 16)}
                        for mac, media, size in zip(
                            values(tlv, "ieee1905.mac_addr"),
                            values(tlv, "ieee1905.dev_info.media_type"),
                            values(tlv, "ieee1905.dev_info.spec_info_len"),
                            strict=True,
                        )
                    ],
                }
            elif kind == 6:
                decoded = {
                    "local_interface": values(tlv, "ieee1905.local_intf.mac_address")[0],
                    "neighbors": values(tlv, "ieee1905.non_1905_neighbor.mac_address"),
                }
            elif kind == 0xCC:
                decoded = {
                    "backhaul_count": int(
                        values(tlv, "ieee1905.akm_suite_capabilities.backhaul_akm_suite_count")[0],
                        16,
                    ),
                    "fronthaul_count": int(
                        values(tlv, "ieee1905.akm_suite_capabilities.fronthaul_akm_suite_count")[0],
                        16,
                    ),
                }
            else:
                prefix = "ieee1905.bss_config_report."
                assert len(values(tlv, prefix + "radio_id")) == 1
                decoded = {
                    "ruid": values(tlv, prefix + "radio_id")[0],
                    "bsses": [
                        {"bssid": bssid, "flags": int(flags, 16), "ssid_hex": ssid}
                        for bssid, flags, ssid in zip(
                            values(tlv, prefix + "mac_addr"),
                            values(tlv, prefix + "report_flags"),
                            values(tlv, prefix + "ssid"),
                            strict=True,
                        )
                    ],
                }
            output.append(
                {"frame": frame, "type": kind, "value_hex": raw.hex(), "decoded": decoded}
            )
    return output


def check_generated(tshark, capture):
    """Check the public --reports receiver trace, independently of EMOSA codecs."""
    data = subprocess.check_output(
        [tshark, "-n", "-2", "-r", str(capture), "-T", "pdml"], timeout=30
    )
    packets = ET.fromstring(data).findall("packet")
    assert len(packets) == 2
    assert [values(p, "ieee1905.message_type") for p in packets] == [["8043"], ["0003"]]
    assert [values(p, "ieee1905.message_id") for p in packets] == [["0001"], ["ffff"]]
    inventories = []
    for packet in packets:
        tlvs = {}
        for row in packet.iter("field"):
            kind = row.find("field[@name='ieee1905.tlv_type']")
            if kind is None:
                continue
            kind = int(kind.attrib["value"], 16)
            assert kind not in tlvs
            raw = bytes.fromhex(row.attrib["value"])
            assert len(raw) == 3 + (int.from_bytes(raw[1:3], "big") & 0x3FFF)
            tlvs[kind] = raw[3:].hex()
        inventories.append(tlvs)
    early, topology = inventories
    assert set(early) == {0, 0xA1, 0x85, 0x86, 0xBE, 0xCC, 0xB4, 0xED}
    assert early[0xCC] == "0001000fac02" and early[0xED] == "01000fac04"
    assert set(topology) == {0, 3, 4, 7, 0x80, 0x83, 0xB7, 0xB3}
    assert topology[4] == "0102020000004001020000004011"
    assert topology[0x80] == "0101" and topology[0xB3] == "01"
    assert values(packets[1], "ieee1905.1905_al_mac_addr") == ["020000004001"]
    assert values(packets[1], "ieee1905.dev_info.media_type") == ["0001", "0103"]
    assert values(packets[1], "ieee1905.dev_info.spec_info_len") == ["00", "0a"]
    prefix = "ieee1905.bss_config_report."
    assert values(packets[1], prefix + "radio_id") == ["020000004010"]
    assert values(packets[1], prefix + "mac_addr") == ["020000004011"]
    assert values(packets[1], prefix + "report_flags") == ["40"]
    assert values(packets[1], prefix + "ssid") == [b"EMOSA-report-fixture".hex()]
    return {"capture": capture.name, "sha256": hashlib.sha256(capture.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--tshark", default="tshark")
    parser.add_argument("--report-capture", type=Path, action="append", default=[])
    args = parser.parse_args()
    assert hashlib.sha256(CAPTURE.read_bytes()).hexdigest() == CAPTURE_SHA
    result = subprocess.run(
        [
            args.tshark,
            "-n",
            "-2",
            "-r",
            str(CAPTURE),
            "-Y",
            "frame.number == 3 || frame.number == 20",
            "-T",
            "pdml",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    cases = project(result.stdout)
    if args.record:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        with FIXTURE.open("x") as stream:
            stream.write(json.dumps(cases, indent=2) + "\n")
    assert cases == json.loads(FIXTURE.read_text())
    print(
        json.dumps(
            {
                "passed": True,
                "cases": len(cases),
                "capture_sha256": CAPTURE_SHA,
                "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                "dissector": subprocess.check_output(
                    [args.tshark, "--version"], text=True
                ).splitlines()[0],
                "scope": "native fields and selected-edition negatives; not conformance",
                "generated_report_checks": [
                    check_generated(args.tshark, capture) for capture in args.report_capture
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
