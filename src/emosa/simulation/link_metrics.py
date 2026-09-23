"""Generate synthetic neighbor-metric vectors; never claim measured link values."""

import argparse
import json
from pathlib import Path

from emosa.simulation.wire_reports import AGENT, CONTROLLER, pcap
from emosa.wire.cmdu import Tlv, fragment_message
from emosa.wire.link_metrics import LinkMetricQuery, LinkMetrics, RxLink, TxLink


def generate(output):
    output.mkdir(parents=True, exist_ok=False)
    local = bytes.fromhex("020000005001")
    peer = bytes.fromhex("020000005002")
    tx = LinkMetrics(
        AGENT,
        CONTROLLER,
        (
            TxLink(local, peer, 1, True, 3, 513, 940, 97, 65535),
            TxLink(
                bytes.fromhex("020000005003"),
                bytes.fromhex("020000005004"),
                0x103,
                False,
                2**32 - 1,
                2**32 - 2,
                65,
                0,
                65535,
            ),
        ),
    )
    rx = LinkMetrics(
        AGENT,
        CONTROLLER,
        (
            RxLink(local, peer, 1, 2, 1027, 255),
            RxLink(
                bytes.fromhex("020000005003"), bytes.fromhex("020000005004"), 0x103, 7, 8193, 42
            ),
        ),
    )
    frames, cases = [], []
    for index, query in enumerate(
        (
            LinkMetricQuery(None, 0),
            LinkMetricQuery(None, 1),
            LinkMetricQuery(None, 2),
            LinkMetricQuery(CONTROLLER, 2),
            LinkMetricQuery(bytes.fromhex("020000009999"), 2),
        )
    ):
        mid = 400 + index
        frames.extend(fragment_message(AGENT, CONTROLLER, 5, mid, (query.tlv(),)))
        if index == 4:
            tlvs = (Tlv(12, b"\0"),)
        else:
            tlvs = (
                (tx.tlv(),)
                if query.direction == 0
                else (rx.tlv(),)
                if query.direction == 1
                else (tx.tlv(), rx.tlv())
            )
        frames.extend(fragment_message(CONTROLLER, AGENT, 6, mid, tlvs))
        cases.append(
            {
                "mid": mid,
                "query_value": query.tlv().value.hex(),
                "response_kinds": [t.kind for t in tlvs],
            }
        )
    (output / "synthetic-neighbor-metrics.pcap").write_bytes(pcap(frames))
    (output / "inputs.json").write_text(
        json.dumps(
            {
                "scope": "synthetic wire-layout vectors; no measured link or controller acceptance",
                "cases": cases,
                "measurement_source_qualified": False,
                "sustained_operation_proven": False,
                "physical_pod_proven": False,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
