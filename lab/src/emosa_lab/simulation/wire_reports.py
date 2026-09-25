"""Offline synthetic report exercise. No sockets, radio, controller or pod I/O."""

import argparse
import json
import struct
from pathlib import Path

from emosa.easymesh_payloads import (
    AKMSuiteCapabilities,
    APCapability,
    APHTCapabilities,
    APOperationalBss,
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    AssociatedClients,
    BasicOperatingClass,
    BssClients,
    BssConfigurationReport,
    ConfiguredBss,
    ConfiguredRadio,
    OperationalBss,
    OperationalRadio,
    Profile2APCapability,
    SupportedCipherSuites,
    decode_value,
)
from emosa.wire.autoconfiguration import PeerBinding
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.reports import (
    CCMP128,
    PSK,
    EarlyCapabilities,
    EarlyRadio,
    ReportStamp,
    TopologyFacts,
    early_report,
    topology_response,
)
from emosa.wire.topology_values import (
    BridgingCapability,
    DeviceInformation,
    LocalInterface,
    Neighbor,
    Neighbors1905,
)

AGENT = bytes.fromhex("020000004001")
CONTROLLER = bytes.fromhex("020000004002")
RUID = bytes.fromhex("020000004010")
BSSID = bytes.fromhex("020000004011")
SSID = b"EMOSA-report-fixture"


def fixtures(*, ingress="fixture"):
    """Invented facts for layout/transport tests, never evidence about a physical pod."""
    binding = PeerBinding(ingress, 1, AGENT, CONTROLLER, (CONTROLLER,))
    basic = APRadioBasicCapabilities(RUID, 1, (BasicOperatingClass(81, 20, ()),))
    capabilities = EarlyCapabilities(
        (
            EarlyRadio(
                basic,
                APHTCapabilities(RUID, 0),
                APRadioAdvancedCapabilities(RUID, 0),
                False,
                False,
                False,
            ),
        ),
        APCapability(0),
        Profile2APCapability(0, 0, 0, 0),
        AKMSuiteCapabilities((), (PSK,)),
        SupportedCipherSuites((CCMP128,)),
        True,
    )
    topology = TopologyFacts(
        DeviceInformation(
            AGENT,
            (LocalInterface(AGENT, 1, b""), LocalInterface(BSSID, 0x103, BSSID + b"\0\0\x06\0")),
        ),
        BridgingCapability(((AGENT, BSSID),)),
        (),
        (Neighbors1905(AGENT, (Neighbor(CONTROLLER, False),)),),
        APOperationalBss((OperationalRadio(RUID, (OperationalBss(BSSID, SSID),)),)),
        BssConfigurationReport((ConfiguredRadio(RUID, (ConfiguredBss(BSSID, 0x40, SSID),)),)),
        AssociatedClients((BssClients(BSSID, ()),)),
        True,
        True,
        True,
        True,
    )
    return binding, capabilities, topology


def query_frames(mid=65535):
    return fragment_message(AGENT, CONTROLLER, 2, mid, (Tlv(0xB3, b"\1"),))


def pcap(frames):
    """Synthetic timestamps only; this does not claim independent sniffing."""
    data = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for index, frame in enumerate(frames):
        data.extend(struct.pack("<IIII", 1, index, len(frame), len(frame)) + frame)
    return bytes(data)


def inventory(message):
    value = next(t.value for t in message.tlvs if t.kind == 0x83)
    bsses = decode_value(0x83, value)
    return {
        "source": "synthetic_receiver_decoded_topology_response",
        "radios": [
            {"ruid": radio.ruid.hex(":"), "bssids": [b.ap_mac.hex(":") for b in radio.bsses]}
            for radio in bsses.radios
        ],
        "native_controller_inventory": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    binding, capabilities, topology = fixtures()
    stamp = ReportStamp("public-synthetic-report-facts-v1", 0, 2)
    early = early_report(binding, capabilities, stamp, MidSequence(0), clock=lambda: 0)
    frames = []
    early.send(frames.append, lambda: stamp, clock=lambda: 0)
    request = query_frames()[0]
    frames.append(request)
    reply = topology_response(
        Reassembler().feed(request),
        binding,
        topology,
        stamp,
        ingress="fixture",
        generation=1,
        received_at=0,
        clock=lambda: 0,
    )
    received = []
    reply.send(received.append, lambda: stamp, clock=lambda: 0)
    frames.extend(received)
    assembly = Reassembler()
    response = None
    for frame in received:
        response = assembly.feed(frame)
    assert response.mid == 65535
    result = {
        "passed": True,
        "scope": "offline_synthetic_complete_reports",
        "early_report_message_type": "0x8043",
        "query_mid": 65535,
        "response_mid": response.mid,
        "frames": len(frames),
        "receiver_inventory": inventory(response),
        "socket_io": False,
        "operations_created": 0,
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
    }
    (args.output / "synthetic-reports.pcap").write_bytes(pcap(frames))
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
