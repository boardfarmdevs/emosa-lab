"""Generate explicitly synthetic final-statistics encoding vectors; no pod use."""

import argparse
import json
from pathlib import Path

from emosa.wire.cmdu import fragment_message
from emosa.wire.disassociation import FinalSession, TrafficCounters
from emosa_lab.simulation.wire_reports import pcap


def generate(output):
    output.mkdir(parents=True, exist_ok=False)
    station = bytes.fromhex("020000004020")
    counters = TrafficCounters(2**42 + 2049, 2**32 + 1, 2**32 + 3, 4, 5, 6, 7)
    rows, frames = [], []
    for profile, units, reason in ((1, 0, 3), (2, 1, 8), (3, 2, 71)):
        event = FinalSession(
            "synthetic", "fixture", bytes.fromhex("020000004011"), station, 0.0, reason, counters
        )
        frames.extend(
            fragment_message(
                bytes.fromhex("020000000002"),
                bytes.fromhex("020000000001"),
                0x8022,
                100 + units,
                event.tlvs(profile=profile, byte_units=units),
            )
        )
        rows.append(
            {
                "frame": len(frames),
                "profile": profile,
                "byte_units": units,
                "reason": reason,
                "source_counters": list(counters.values()),
            }
        )
    (output / "synthetic-final-statistics.pcap").write_bytes(pcap(frames))
    (output / "inputs.json").write_text(
        json.dumps(
            {
                "scope": "synthetic encoding vectors, not measured radio/session statistics",
                "sustained_operation_proven": False,
                "physical_pod_proven": False,
                "vectors": rows,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    generate(args.output)
