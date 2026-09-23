"""Independently correlate native wire, durable receipt, inventory and clients.

No EMOSA imports or secret material. Inputs are a reviewed per-run file allowlist.
This checker establishes a bounded simulation result, never physical conformance.
"""

import argparse
import hashlib
import json
import re
import runpy
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

AGENT, CONTROLLER, RADIO = "020000003001", "020000e00001", "020000ec0200"
SSID = "emosa-controller-trial"


def read(path):
    return json.loads(path.read_text())


def tlvs(data):
    result = []
    while data:
        assert len(data) >= 3
        kind, length = struct.unpack_from("!BH", data)
        data = data[3:]
        assert len(data) >= length
        if kind == 0:
            assert length == 0 and not any(data)
            return result
        result.append((kind, data[:length]))
        data = data[length:]
    raise AssertionError("missing end TLV")


def packets(path):
    data = path.read_bytes()
    assert data[:4] == bytes.fromhex("d4c3b2a1")
    assert struct.unpack_from("<I", data, 20)[0] == 1
    data = data[24:]
    rows = []
    while data:
        assert len(data) >= 16
        sec, usec, size, original = struct.unpack_from("<IIII", data)
        assert size == original
        frame, data = data[16 : 16 + size], data[16 + size :]
        assert len(frame) == size and size >= 25 and frame[12:14] == b"\x89\x3a"
        version, reserved, kind, mid, fragment, flags = struct.unpack_from("!BBHHBB", frame, 14)
        assert version == reserved == fragment == 0 and flags & 0x80
        rows.append(
            {
                "frame": len(rows) + 1,
                "time": sec + usec / 1e6,
                "kind": kind,
                "mid": mid,
                "source": frame[6:12].hex(),
                "destination": frame[:6].hex(),
                "tlvs": tlvs(frame[22:]),
            }
        )
    return rows


def value(packet, kind):
    values = [v for t, v in packet["tlvs"] if t == kind]
    assert len(values) == 1
    return values[0]


def zero_packet_loss(text):
    # A substring check for "0%" also accepts "50%" and "100%".
    values = re.findall(r"(\d+(?:\.\d+)?)% packet loss", text)
    return len(values) == 1 and float(values[0]) == 0


def candidate_inputs(build, trial):
    """Accept only the two explicitly qualified native candidate recipes."""
    assert trial["candidate_sha256"] == build["candidate_sha256"]
    assert build["counter_regression"]["passed"] == 12
    onboarding = {
        "name": "0005-controller-configuration-scope.patch",
        "sha256": "9bcab5ce7b22b7d36bdb55b857eadfb0e75f71b03332e693f40226ba4ab31ddb",
    }
    lifecycle = {
        "name": "0006-bpl-model-lifetime.patch",
        "sha256": "fb9b3a45721952532b21592aafb2eba77e5795fd91543940e756510b60615785",
    }
    assert build["extra_patches"] in ([onboarding], [onboarding, lifecycle])
    if lifecycle in build["extra_patches"]:
        libraries = build["runtime_libraries"]
        assert set(libraries) == {"libbpl.so.6.0.0"}
        assert trial["runtime_libraries"] == libraries
        assert trial["runtime_library_restoration"] == {
            name: {"restored_sha256": hashes["baseline_sha256"], "restored": True}
            for name, hashes in libraries.items()
        }


def check(directory, tool, *, initial_only=False):
    result, operation = read(directory / "result.json"), read(directory / "native-operation.json")
    capture_health = None
    if result.get("capture_health_required"):
        capture_health = runpy.run_path(str(Path(__file__).with_name("check-capture-health.py")))[
            "check"
        ](directory)
    if initial_only:
        initial = result.get("initial_operation", result["operation"])
        assert initial["operation"]["operation_id"] == operation["operations"][0]["operation_id"]
        assert initial["operation"]["receipt"] == operation["operations"][0]["receipt"]
        operation = initial
    assert result["status"] == "observed_pending_capture_review" and not result["cleanup_errors"]
    assert result["native_controller"] and not result["semantic_submission"]
    trial = read(directory / "candidate-trial.json")
    assert trial["baseline_restored"]
    assert trial["operations_created"] == (
        result["operation"]["operation_count"] if initial_only else 1
    )
    build = read(directory / "candidate.json")
    candidate_inputs(build, trial)
    before = read(directory / "withheld-operation.json")
    assert before["operation"]["state"] == "CONFIG_COMMITTED"
    assert before["observed_ssid"] == "emosa-radio-initial" and before["config_ssid"] == SSID
    assert operation["operation"]["state"] == "OBSERVED_APPLIED"
    assert operation["config_ssid"] == operation["observed_ssid"] == SSID
    assert operation["operation_count"] == operation["writes"] == 1
    assert operation["operation"]["operation_id"] == before["operation"]["operation_id"]
    receipt = operation["operation"]["receipt"]
    assert receipt["agent_al"] == AGENT and receipt["controller_al"] == CONTROLLER
    assert receipt["ruid"] == receipt["bssid"] == RADIO
    assert operation["operation"]["application_evidence"]["provenance"].startswith(
        "independent-hostapd"
    )
    rows = packets(directory / "ethernet.pcap")
    # Independently cross-check the small reader against Wireshark's dissector.
    decoded = subprocess.check_output(
        [
            tool,
            "-n",
            "-r",
            str(directory / "ethernet.pcap"),
            "-T",
            "fields",
            "-e",
            "ieee1905.message_type",
            "-e",
            "ieee1905.message_id",
        ],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    assert [tuple(int(v, 16) for v in line.split()) for line in decoded.splitlines()] == [
        (p["kind"], p["mid"]) for p in rows
    ]
    search = next(p for p in rows if p["kind"] == 7 and p["source"] == AGENT)
    response = next(p for p in rows if p["kind"] == 8 and p["source"] == CONTROLLER)
    early = next(p for p in rows if p["kind"] == 0x8043 and p["source"] == AGENT)
    m1 = next(p for p in rows if p["kind"] == 9 and p["source"] == AGENT)
    m2 = next(p for p in rows if p["kind"] == 9 and p["source"] == CONTROLLER)
    assert search["mid"] == response["mid"]
    assert value(search, 0xB3) == value(response, 0xB3) == b"\1"
    assert value(response, 0xDD)[0] & 0xC0 == 0xC0
    assert search["frame"] < response["frame"] < early["frame"] < m1["frame"] < m2["frame"]
    assert hashlib.sha256(value(m1, 0x11)).hexdigest() == receipt["m1_sha256"]
    assert value(m1, 0x85)[:7] == bytes.fromhex(RADIO) + b"\1"
    # Earlier bounded runs advertised KiB but sent no traffic-statistic TLVs.
    # New Profile-1 runs explicitly declare bytes to match EasyMesh Table 58.
    counter_units = result.get("agent_counter_units", 1)
    assert counter_units in (0, 1)
    assert value(m1, 0xB4)[2] == counter_units << 6
    assert all(
        len(data) == 4 and data[2] >> 6 == counter_units
        for packet in rows
        if packet["source"] == AGENT
        for kind, data in packet["tlvs"]
        if kind == 0xB4
    )
    if any(kind == 0xA2 for p in rows for kind, _ in p["tlvs"]):
        assert counter_units == 0
    assert value(m2, 0x82).hex() == RADIO and m2["mid"] == receipt["first_mid"]
    assert {k for k, _ in m2["tlvs"]} == {0x82, 0x11}
    expected_bss = bytes((1,)) + bytes.fromhex(RADIO) + b"\1" + bytes.fromhex(RADIO)
    expected_bss += bytes((len(SSID),)) + SSID.encode()
    topology = next(p for p in rows if p["kind"] == 3 and value(p, 0x83) == expected_bss)
    query = next(p for p in rows if p["kind"] == 2 and p["mid"] == topology["mid"])
    assert 0 <= topology["time"] - query["time"] < 1
    assert m2["frame"] < topology["frame"] and value(topology, 0xB3) == b"\1"
    capability = next(p for p in rows if p["kind"] == 0x8002)
    assert any(p["kind"] == 0x8001 and p["mid"] == capability["mid"] for p in rows)
    assert {0xA1, 0x85, 0x86, 0xB4, 0xD4} <= {k for k, _ in capability["tlvs"]}
    objects = {k: v for o in read(directory / "controller-after.json") for k, v in o.items()}
    prefix = next(
        k for k, v in objects.items() if isinstance(v, dict) and v.get("ID") == "02:00:00:00:30:01"
    )
    radios = [v for k, v in objects.items() if re.fullmatch(re.escape(prefix) + r"Radio\.\d+\.", k)]
    bsses = [
        v
        for k, v in objects.items()
        if re.fullmatch(re.escape(prefix) + r"Radio\.\d+\.BSS\.\d+\.", k)
    ]
    assert len(radios) == len(bsses) == 1
    assert radios[0]["ID"] == bsses[0]["BSSID"] == "02:00:00:ec:02:00"
    assert radios[0]["Enabled"] and bsses[0]["Enabled"] and bsses[0]["SSID"] == SSID
    assert bsses[0]["FronthaulUse"] and not bsses[0]["BackhaulUse"]
    assert "02:00:00:00:30:01" not in json.dumps(read(directory / "controller-before.json"))
    clients = read(directory / "clients-onboarded.json")
    for name, interface in (("em-baseline-wired", "eth1"), ("em-baseline-wifi", "wlan0")):
        obs = clients["observations"][name]
        assert obs["interface"] == interface and obs["application"]["nonce"] == clients["nonce"]
        assert all(r.get("dev") == interface for r in obs["routes"])
        assert zero_packet_loss(obs["ping"])
    wifi = clients["observations"]["em-baseline-wifi"]["supplicant"]
    assert wifi["wpa_state"] == "COMPLETED" and wifi["ssid"] == SSID
    assert wifi["bssid"] == "02:00:00:ec:02:00" and wifi["key_mgmt"] == "WPA2-PSK"

    def radio_fields(display, field):
        return subprocess.check_output(
            [
                tool,
                "-n",
                "-r",
                str(directory / "radio.pcap"),
                "-Y",
                display,
                "-T",
                "fields",
                "-e",
                field,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=30,
        ).splitlines()

    beacon_xml = subprocess.check_output(
        [
            tool,
            "-n",
            "-r",
            str(directory / "radio.pcap"),
            "-Y",
            "wlan.fc.type_subtype == 8 && wlan.sa == 02:00:00:ec:02:00",
            "-T",
            "pdml",
        ],
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    # -T fields changed SSID rendering between Wireshark versions; use raw octets.
    beacons = [
        field.attrib["value"]
        for field in ET.fromstring(beacon_xml).iter("field")
        if field.attrib.get("name") == "wlan.ssid"
    ]
    assert {"emosa-radio-initial", SSID} <= {bytes.fromhex(s).decode() for s in beacons}
    eapol = radio_fields("eapol && wlan.addr == 02:00:00:00:02:00", "wlan_rsna_eapol.keydes.msgnr")
    assert {"1", "2", "3", "4"} <= set(eapol)
    return {
        "passed": True,
        "scope": "bounded_native_controller_to_simulated_OpenSync_onboarding",
        "controller_onboarding_proven": True,
        "physical_pod_proven": False,
        "native_baseline_restored": True,
        "operations": 1,
        "writes": 1,
        "advertised_byte_counter_units": counter_units,
        "capture_health": capture_health,
        "frames": len(rows),
        "m1_sha256": receipt["m1_sha256"],
        "capability_report_frame": capability["frame"],
        "provisioned_topology_frame": topology["frame"],
        "independent_clients": ["wired", "wpa_supplicant"],
        "continuous_management_proven": False,
        "full_profile_qualified": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    print(json.dumps(check(args.directory, args.tshark), indent=2))
