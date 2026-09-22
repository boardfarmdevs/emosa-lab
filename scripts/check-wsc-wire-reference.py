"""Independently inspect the owned WSC packet experiment and its public receipt.

Uses tshark for Ethernet/CMDU headers, then a small standalone PCAP/TLV reader
for the deliberately reversed fragments. No EMOSA imports, decryption or claim
of native-controller admission. The live hostap peer separately checks payloads.
"""

import argparse
import hashlib
import json
import struct
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def packets(path):
    data = path.read_bytes()
    assert len(data) >= 24 and data[:4] == bytes.fromhex("d4c3b2a1")
    assert struct.unpack_from("<I", data, 20)[0] == 1  # Ethernet, no FCS.
    offset, result = 24, []
    while offset < len(data):
        assert offset + 16 <= len(data)
        _, _, captured, original = struct.unpack_from("<IIII", data, offset)
        assert captured == original and 22 <= captured <= 1514
        offset += 16
        frame = data[offset : offset + captured]
        assert len(frame) == captured
        result.append(frame)
        offset += captured
    assert offset == len(data)
    return result


def fields(data, *, wps=False, final=True):
    offset, result = 0, {}
    size = 4 if wps else 3
    while offset < len(data):
        assert offset + size <= len(data)
        kind, length = struct.unpack_from("!HH" if wps else "!BH", data, offset)
        offset += size
        assert kind not in result
        if not wps and kind == 0:
            assert final and length == 0 and not any(data[offset:])
            return result
        assert offset + length <= len(data)
        result[kind] = data[offset : offset + length]
        offset += length
    assert wps or not final
    return result


def check(tool, directory):
    raw, digests = {}, {}
    expected = {
        "left": [(501, 0), (502, 0), (503, 1), (503, 0), (504, 0), (505, 0), (506, 0)],
        "right": [(101, 0), (102, 0)],
    }
    for side in ("left", "right"):
        path = directory / (side + ".pcap")
        digests[side] = hashlib.sha256(path.read_bytes()).hexdigest()
        raw[side] = packets(path)
        pdml = subprocess.check_output([tool, "-n", "-r", str(path), "-T", "pdml"], timeout=30)
        dissected = ET.fromstring(pdml).findall("packet")
        assert len(dissected) == len(raw[side]) == len(expected[side])
        source, destination = ("020000005002", "020000005001")
        if side == "right":
            source, destination = destination, source
        for frame, packet, (mid, fid) in zip(raw[side], dissected, expected[side], strict=True):
            assert frame[:14].hex() == destination + source + "893a"
            assert frame[14:22] == struct.pack(
                "!BBHHBB", 0, 0, 9, mid, fid, 0 if (mid, fid) == (503, 0) else 128
            )
            for name, value in {
                "eth.src": source,
                "eth.dst": destination,
                "ieee1905.message_type": "0009",
                "ieee1905.message_id": f"{mid:04x}",
                "ieee1905.fragment_id": f"{fid:02x}",
            }.items():
                assert [f.attrib["value"] for f in packet.findall(f".//field[@name='{name}']")] == [
                    value
                ]

    m1 = [fields(frame[22:]) for frame in raw["right"]]
    assert all(set(row) == {0x85, 0x11, 0xB4, 0xBE} for row in m1)
    first, second = [fields(row[0x11], wps=True) for row in m1]
    for row in (first, second):
        assert row[0x1022] == b"\x04" and row[0x1020].hex() == "020000005001"
    assert first[0x101A] != second[0x101A] and first[0x1032] != second[0x1032]
    requests = [hashlib.sha256(row[0x11]).hexdigest() for row in m1]
    peer = json.loads((directory / "right.json").read_text())
    adapter = json.loads((directory / "left.json").read_text())
    assert peer["m1_sha256"] == requests
    receipt = adapter["operation"]["receipt"]
    assert receipt["m1_sha256"] == requests[1] and receipt["first_mid"] == 503
    assert receipt["agent_al"] == "020000005001" and receipt["controller_al"] == "020000005002"
    assert receipt["ruid"] == "020000005010"

    messages = {}
    for frame in sorted(raw["left"], key=lambda data: (data[18:20], data[20])):
        mid = int.from_bytes(frame[18:20], "big")
        part = fields(frame[22:], final=bool(frame[21] & 128))
        message = messages.setdefault(mid, {})
        assert not message.keys() & part.keys()
        message.update(part)
    assert messages[503][0xEF] == bytes(1024)
    payloads = {}
    for mid, row in messages.items():
        expected_ruid = "02000000ffff" if mid == 502 else "020000005010"
        assert row[0x82].hex() == expected_ruid
        payloads[mid] = fields(row[0x11], wps=True)
        assert payloads[mid][0x1022] == b"\x05"
        assert payloads[mid][0x101A] == (first if mid == 501 else second)[0x101A]
        assert len(payloads[mid][0x1005]) == 8 and len(payloads[mid][0x1018]) >= 32
        assert b"OnlySimulationWscKey2026!" not in row[0x11]
    assert messages[502][0x11] == messages[503][0x11] == messages[504][0x11]
    assert len({messages[mid][0x11] for mid in (503, 505, 506)}) == 3
    assert all(
        adapter[key] == 0
        for key in ("invalid_authentication_operations", "incomplete_request_operations")
    )
    operation = adapter["operation"]
    assert operation["initiating_interface"] == "wsc-component"
    assert operation["state"] == "OBSERVED_APPLIED"
    assert operation["attempts"] == adapter["transaction_attempts"] == 1
    assert not adapter["native_controller_used"] and not adapter["physical_pod_proven"]
    if adapter["lost_reply_injected"]:
        assert operation["commit_attribution"] == "unknown"
        assert operation["application_evidence"]["attribution"] == "current_condition_only"
        counts = {"rejected": 4, "incomplete": 1, "operation": 1}
    else:
        assert operation["commit_attribution"] == "reply"
        counts = {"rejected": 2, "incomplete": 1, "operation": 3}
    assert adapter["packet_session"]["counts"] == counts
    return {
        "directory": str(directory),
        "sha256": digests,
        "received_frames": [7, 2],
        "m1_mids": [101, 102],
        "receipt_correlated": True,
    }


def check_radio(tool, directory):
    result = json.loads((directory / "result.json").read_text())
    assert result["passed"] and result["status"] == "passed" and not result["cleanup_errors"]
    assert result["initiating_interface"] == "wsc-component" and not result["semantic_submission"]
    assert not result["native_controller_used"] and not result["physical_pod_proven"]
    cases = result["cases"]
    before = cases["wire_operation_before_apply"]
    assert before["config_ssid"] == "EMOSA-WSC-component"
    assert before["observed_ssid"] == "emosa-radio-initial"
    operation = cases["wire_operation_applied"]
    adapter = json.loads((directory / "ethernet/left.json").read_text())
    assert operation == adapter["operation"] and before == adapter["before_application"]
    assert operation["operation_id"] == before["operation"]["operation_id"]
    assert operation["receipt"]["bssid"] == "020000ec0200"
    assert operation["application_evidence"]["provenance"] == (
        "independent-hostapd-nl80211-manager:Wifi_VIF_State"
    )
    nonces = set()
    for phase in ("initial", "withheld", "changed", "restored"):
        row = json.loads((directory / f"clients-{phase}.json").read_text())
        assert row["nonce"] not in nonces
        nonces.add(row["nonce"])
        assert row["nonce"] == cases[phase + "_clients"]["nonce"]
        assert set(row["observations"]) == {"em-baseline-wired", "em-baseline-wifi"}
        for name, observation in row["observations"].items():
            wifi = name.endswith("wifi")
            assert observation["interface"] == ("wlan0" if wifi else "eth1")
            assert observation["elapsed_seconds"] <= 30
            assert observation["application"]["nonce"] == row["nonce"]
            assert observation["application"]["peer"] == ("192.0.2.21" if wifi else "192.0.2.20")
            assert all(route["dev"] != "eth0" for route in observation["routes"])
            if wifi:
                state = observation["supplicant"]
                assert state["wpa_state"] == "COMPLETED" and state["bssid"] == "02:00:00:ec:02:00"
                assert state["ssid"] == (
                    "emosa-radio-initial"
                    if phase in {"initial", "withheld"}
                    else "EMOSA-WSC-component"
                )
    assert cases["wrong_key"] == {"fresh_wrong_key_event": True, "authenticated": False}
    assert "WRONG_KEY" in (directory / "wrong-key.log").read_text()
    capture = directory / "radio.pcap"
    packets = json.loads((directory / "packet-observations.json").read_text())
    assert packets["capture"]["sha256"] == hashlib.sha256(capture.read_bytes()).hexdigest()
    assert packets["capture"]["size"] == capture.stat().st_size

    def dissect(display_filter, field):
        return subprocess.check_output(
            [tool, "-n", "-r", str(capture), "-Y", display_filter, "-T", "fields", "-e", field],
            text=True,
            timeout=30,
        ).splitlines()

    beacons = dissect("wlan.fc.type_subtype == 8 && wlan.sa == 02:00:00:ec:02:00", "wlan.ssid")
    # Wireshark 3.x displays this field as text; 4.x exposes its octets as hex.
    # The selected fixture SSIDs are unambiguous in both representations.
    expected_ssids = {"emosa-radio-initial", "EMOSA-WSC-component"}
    counts = Counter(
        line if line in expected_ssids else bytes.fromhex(line).decode() for line in beacons
    )
    assert counts == packets["beacon_counts"]
    assert {"emosa-radio-initial", "EMOSA-WSC-component"} <= counts.keys()
    eapol = dissect("eapol && wlan.addr == 02:00:00:00:02:00", "wlan_rsna_eapol.keydes.msgnr")
    assert {"1", "2", "3", "4"} <= set(eapol) and len(eapol) == packets["eapol_frames"]
    return {
        "directory": str(directory),
        "capture_sha256": packets["capture"]["sha256"],
        "client_phases": 4,
        "beacon_counts": counts,
        "eapol_frames": len(eapol),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, action="append", required=True)
    parser.add_argument("--tshark", default="tshark")
    parser.add_argument("--radio-directory", type=Path, action="append", default=[])
    args = parser.parse_args()
    checks = [check(args.tshark, directory) for directory in args.directory]
    print(
        json.dumps(
            {
                "passed": True,
                "checks": checks,
                "radio_checks": [
                    check_radio(args.tshark, directory) for directory in args.radio_directory
                ],
                "dissector": subprocess.check_output(
                    [args.tshark, "--version"], text=True
                ).splitlines()[0],
                "scope": (
                    "Owned received Ethernet frames and receipt; "
                    "no independent decryption or native admission claim"
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
