"""Independent Wireshark projection of synthetic final-session wire vectors.

No EMOSA imports. Literal expected values come from the reviewed vectors and
EasyMesh 6.1 Tables 46/58/87. This verifies encoding, not measurement finality.
"""

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/protocol/disassociation"


def check(directory, tool):
    capture = directory / "synthetic-final-statistics.pcap"
    raw = subprocess.check_output(
        [tool, "-n", "-r", str(capture), "-T", "pdml"], stderr=subprocess.DEVNULL, timeout=30
    )
    packets = ET.fromstring(raw).findall("packet")
    assert len(packets) == 3
    # bytes; KiB; MiB, with scale-before-rollover and packet rollover.
    expected = (
        (2049, 1, 3, 4, 5, 6, 7),
        (2, 4194304, 3, 4, 5, 6, 7),
        (4194304, 4096, 3, 4, 5, 6, 7),
    )
    reasons = (3, 8, 71)
    for index, packet in enumerate(packets):

        def field(name, packet=packet):
            found = packet.findall(f".//field[@name='ieee1905.{name}']")
            assert len(found) == 1, name
            return found[0].attrib["value"]

        assert int(field("message_type"), 16) == 0x8022
        assert int(field("message_id"), 16) == 100 + index
        assert [
            int(f.attrib["value"], 16)
            for f in packet.findall(".//field[@name='ieee1905.tlv_type']")
        ] == [0x95, 0xCA, 0xA2, 0]
        assert field("sta_mac_addr_type.mac_addr") == "020000004020"
        assert field("assoc_sta_traffic_stats.mac_addr") == "020000004020"
        assert int(field("disassociation_reason_code.reason_code"), 16) == reasons[index]
        counts = tuple(
            int(field("assoc_sta_traffic_stats." + name), 16)
            for name in (
                "bytes_sent",
                "bytes_rcvd",
                "packets_sent",
                "packets_rcvd",
                "tx_pkt_errs",
                "rx_packet_errs",
                "retrans_count",
            )
        )
        assert counts == expected[index]
        assert not packet.findall(".//proto[@name='_ws.malformed']")
    return {
        "passed": True,
        "vectors": 3,
        "scope": "synthetic final-statistics encoding only",
        "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        "dissector": subprocess.check_output([tool, "--version"], text=True).splitlines()[0],
        "physical_pod_proven": False,
        "final_counter_source_qualified": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=FIXTURE)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    print(json.dumps(check(args.directory, args.tshark), indent=2))
