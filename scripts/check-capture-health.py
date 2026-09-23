"""Check retained tcpdump statistics against complete native pcap records.

This detects collector/kernel loss and file truncation. It cannot establish
capture coverage before the observation point, nor qualify counter semantics.
Standard library only, independent of EMOSA and the capture runner.
"""

import argparse
import json
import re
import struct
from pathlib import Path


def check_file(capture, log, linktype):
    data = capture.read_bytes()
    assert len(data) >= 24 and data[:4] == bytes.fromhex("d4c3b2a1"), "expected little-endian pcap"
    major, minor, _zone, _sigfigs, snaplen, network = struct.unpack_from("<HHiiII", data, 4)
    assert (major, minor) == (2, 4) and network == linktype and snaplen >= 65535
    count, offset = 0, 24
    while offset < len(data):
        assert offset + 16 <= len(data), "truncated record header"
        _sec, usec, size, original = struct.unpack_from("<IIII", data, offset)
        assert 0 <= usec < 1000000 and 0 < size == original <= snaplen, "truncated captured packet"
        offset += 16 + size
        assert offset <= len(data), "truncated packet data"
        count += 1
    values = {}
    text = log.read_text()
    for name, suffix in (
        ("captured", "packets captured"),
        ("received_by_filter", "packets received by filter"),
        ("dropped_by_kernel", "packets dropped by kernel"),
    ):
        found = re.findall(r"^(\d+) " + suffix + r"$", text, re.M)
        assert len(found) == 1, "missing or repeated tcpdump statistics"
        values[name] = int(found[0])
    assert count > 0 and count == values["captured"], "pcap/log record-count mismatch"
    # This checker targets the retained Linux captures. A larger filter count
    # can also mean packets remained unread at shutdown, even with zero drops.
    assert values["received_by_filter"] == count, "unreconciled filter/capture counts"
    assert values["dropped_by_kernel"] == 0, "kernel dropped captured packets"
    return {"passed": True, "linktype": network, "pcap_records": count, **values}


def check(directory):
    return {
        name: check_file(
            directory / (name + ".pcap"), directory / (name + "-capture.log"), linktype
        )
        for name, linktype in (("ethernet", 1), ("radio", 127))
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), indent=2))
