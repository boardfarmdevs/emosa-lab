"""Independent tshark validation of the native-controller discovery trial.

No EMOSA imports and no network transmission. A passing check establishes a
correlated Search/Response measurement, never onboarding or profile conformance.
"""

import argparse
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def field(packet, name):
    values = [f.attrib["value"] for f in packet.findall(f".//field[@name='{name}']")]
    assert len(values) == 1, (name, values)
    return values[0]


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
    report = json.loads((directory / "result.json").read_text())
    probe = json.loads((directory / "probe.json").read_text())
    assert report["exchange_observed"] and report["cleanup_errors"] == []
    assert report["transport_readiness"]["ready"]
    assert report["capture_response_observed"]
    assert report["probe"] == probe
    assert probe["state"] == "response_observed"
    assert not probe["wsc_started"] and probe["operations_created"] == 0
    assert not probe["profile_qualified"] and not probe["controller_onboarding_proven"]
    path = directory / "ethernet.pcap"
    data = subprocess.check_output([tshark, "-n", "-r", str(path), "-T", "pdml"], timeout=30)
    packets = ET.fromstring(data).findall("packet")
    searches, responses, response_fields = [], [], None
    for packet in packets:
        source, destination = field(packet, "eth.src"), field(packet, "eth.dst")
        kind = int(field(packet, "ieee1905.message_type"), 16)
        mid = int(field(packet, "ieee1905.message_id"), 16)
        fields = tlvs(packet)
        if source == "020000003001":
            assert kind == 7 and destination == "0180c2000013"
            assert fields[1] == bytes.fromhex(source)
            assert fields[0x0D] == fields[0x0E] == b"\0"
            assert fields[0x80] == b"\x01\x01" and fields[0x81] == b"\x01\0"
            assert fields[0xB3] == b"\x01" and fields[0xB4] == bytes(4)
            searches.append(mid)
        else:
            assert source == "020000e00001" and destination == "020000003001" and kind == 8
            assert mid in searches, "Response must follow its captured Search"
            assert fields[0x0F] == fields[0x10] == b"\0" and fields[0x80] == b"\x01\0"
            assert fields[0xB3] == b"\x01"
            responses.append(mid)
            if mid == probe["response_mid"]:
                response_fields = fields
    assert 1 <= len(searches) <= 3 and responses and response_fields is not None
    assert sorted(searches) == probe["sent_mids"] and probe["profile_correlated"]
    assert probe["response_profile"] == probe["search_profile"] == 1
    for kind, name in ((0xDD, "controller_flags_hex"), (0xA9, "security_capability_hex")):
        value = response_fields.get(kind)
        assert probe[name] == (value.hex() if value is not None else None)
    issues = []
    flags = response_fields.get(0xDD)
    if flags is None:
        issues.append("controller_capability_absent")
    else:
        assert flags
        if not flags[0] & 0x80:
            issues.append("kib_mib_support_absent")
        if not flags[0] & 0x40:
            issues.append("early_ap_capability_bit_absent_for_non_dpp_search")
    security = response_fields.get(0xA9)
    if security is None:
        issues.append("security_capability_absent")
    elif len(security) != 3:
        issues.append("security_capability_length_invalid")
    elif security != bytes(3):
        issues.append("security_capability_reserved_algorithm")
    assert issues == probe["selected_response_issues"]
    for name in ("controller-before.json", "controller-after.json"):
        objects = json.loads((directory / name).read_text())
        devices = {
            value["ID"]: key
            for obj in objects
            for key, value in obj.items()
            if re.fullmatch(r"Device\.WiFi\.DataElements\.Network\.Device\.\d+\.", key)
            and isinstance(value, dict)
            and "ID" in value
        }
        if name == "controller-before.json":
            assert "02:00:00:00:30:01" not in devices
            assert report["inventory_before"]["probe_entry"] is None
        else:
            prefix = devices["02:00:00:00:30:01"]
            rows = {key: value for obj in objects for key, value in obj.items()}
            assert rows[prefix]["MultiAPProfile"] == 1
            assert rows[prefix]["RadioNumberOfEntries"] == 0
            assert not any(re.fullmatch(re.escape(prefix) + r"Radio\.\d+\.", key) for key in rows)
            assert report["discovery_entry_observed"]
            assert sorted(devices) == report["inventory_after"]["device_ids"]
            assert report["inventory_after"]["probe_entry"]["path"] == prefix
    return {
        "directory": directory.name,
        "passed": True,
        "search_mids": searches,
        "response_mids": responses,
        "search_profile": 1,
        "response_profile": 1,
        "controller_flags_hex": probe["controller_flags_hex"],
        "security_capability_hex": probe["security_capability_hex"],
        "selected_response_issues": issues,
        "discovery_entry_observed": True,
        "represented_radio_count": 0,
        "pcap_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", action="append", type=Path, required=True)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    results = [check(args.tshark, directory) for directory in args.directory]
    version = subprocess.check_output([args.tshark, "--version"], text=True).splitlines()[0]
    print(json.dumps({"passed": True, "tshark": version, "runs": results}, indent=2))


if __name__ == "__main__":
    main()
