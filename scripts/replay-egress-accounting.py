"""Replay retained collector observations through the production accounting source.

This is component replay, not evidence of a live OVSDB/native-controller session.
The independent checker must also compare the original counters and captures.
"""

import argparse
import json
from pathlib import Path

from emosa.simulation.egress_accounting import EgressAccountingSource


def replay(directory):
    rows = [
        json.loads(line)
        for line in (directory / "egress-observations.jsonl").read_text().splitlines()
    ]
    first = rows[0]
    now = [first["heartbeat_ns"]]
    source = EgressAccountingSource(first["run_label"], clock=lambda: now[0])
    result = []
    for value in rows:
        now[0] = value["heartbeat_ns"]
        source.refresh(
            value,
            generation=1,
            ifindex=first["ifindex"],
            address=first["mac"],
            boot_id=first["boot_id"],
            netns_inode=first["netns_inode"],
        )
        result.append({"heartbeat_ns": now[0], **source.status()})
    return {
        "scope": "component replay of retained observations; not a live OVSDB result",
        "observations": result,
        "sustained_operation_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.directory), indent=2))
