"""Independently reconcile station TX counters with captured hwsim status.

This audits a simulated failure/status path, not RF attempts, airtime, successful
delivery semantics or an EasyMesh counter conversion. No EMOSA decoder is used.
"""

import argparse
import json
import runpy
import struct
from collections import Counter
from pathlib import Path

HEALTH = runpy.run_path(str(Path(__file__).with_name("check-capture-health.py")))
AP, STA = bytes.fromhex("020000ec0200"), bytes.fromhex("020000000200")


def attributes(raw):
    result, offset = {}, 0
    while offset < len(raw):
        assert offset + 4 <= len(raw), "truncated attribute header"
        size, kind = struct.unpack_from("<HH", raw, offset)
        assert size >= 4 and offset + size <= len(raw), "truncated attribute"
        assert not kind & 0x4000, "unexpected byte order"
        kind &= 0x3FFF
        assert kind not in result, "duplicate attribute"
        result[kind] = raw[offset + 4 : offset + size]
        offset += (size + 3) & ~3
    assert offset == len(raw), "missing attribute padding"
    return result


def records(path):
    # check_file() validates format, lengths, totals and capture loss first.
    raw, offset = path.read_bytes(), 24
    while offset < len(raw):
        sec, usec, size, _ = struct.unpack_from("<IIII", raw, offset)
        offset += 16
        yield sec + usec / 1e6, raw[offset : offset + size]
        offset += size


def scalar(raw, fmt):
    assert len(raw) == struct.calcsize("<" + fmt), "invalid scalar width"
    return struct.unpack("<" + fmt, raw)[0]


def messages(raw):
    offset = 0
    while offset < len(raw):
        assert offset + 16 <= len(raw), "truncated netlink header"
        size, kind, flags, seq, port = struct.unpack_from("<IHHII", raw, offset)
        assert size >= 16 and offset + size <= len(raw), "truncated netlink message"
        assert kind != 4, "netlink overrun"
        yield kind, flags, seq, port, raw[offset + 16 : offset + size]
        offset += (size + 3) & ~3
    assert offset == len(raw), "missing netlink padding"


def rates(raw):
    assert len(raw) == 8, "expected four hwsim rate entries"
    used = []
    ended = False
    for index, count in struct.iter_unpack("<bB", raw):
        if index == -1:
            ended = True
        else:
            assert not ended and index >= 0 and 0 < count <= 31, "invalid retry chain"
            used.append((index, count))
    assert used, "empty retry chain"
    return used


def medium(path):
    family = None
    submitted, statuses, requests, replies = {}, {}, {}, {}
    for timestamp, raw in records(path):
        assert len(raw) >= 16 and raw[2:4] == b"\x03\x38", "expected NETLINK header"
        if raw[14:16] != b"\x00\x10":  # only NETLINK_GENERIC
            continue
        for kind, flags, seq, port, body in messages(raw[16:]):
            if kind == 16 and len(body) >= 4:  # nlctrl resolves the dynamic family ID
                a = attributes(body[4:])
                if a.get(2) == b"MAC80211_HWSIM\0" and 1 in a:
                    found = scalar(a[1], "H")
                    assert family in (None, found), "family changed during capture"
                    family = found
            elif kind == 2:  # NLMSG_ERROR includes success acknowledgments
                assert len(body) >= 20
                error = scalar(body[:4], "i")
                _size, original_kind, _flags, original_seq, original_port = struct.unpack_from(
                    "<IHHII", body, 4
                )
                if family and original_kind == family:
                    key = (original_port, original_seq)
                    assert key not in replies, "duplicate netlink reply"
                    replies[key] = error
            elif family and kind == family:
                assert len(body) >= 4 and body[1:4] == b"\x01\x00\x00"
                cmd, a = body[0], attributes(body[4:])
                if flags & 1:
                    key = (port, seq)
                    assert key not in requests and flags & 4, "request lacks Ack or repeats"
                    requests[key] = (cmd, a)
                if cmd == 2 and 2 in a and 3 in a:  # kernel TX submission
                    assert flags == 0 and port == 0 and len(a[2]) == 6
                    key = (a[2], scalar(a[8], "Q"))
                    assert key not in submitted, "repeated radio/cookie"
                    assert len(a[21]) == 12, "missing per-rate HT/VHT flag metadata"
                    submitted[key] = {"time": timestamp, "mpdu": a[3], "attrs": a}
                elif cmd == 3:  # medium TX completion submitted to kernel
                    key = (a[2], scalar(a[8], "Q"))
                    assert key in submitted and key not in statuses, "unmatched/repeated status"
                    statuses[key] = {
                        "time": timestamp,
                        "flags": scalar(a[4], "I"),
                        "rates": rates(a[7]),
                        "request": (port, seq),
                    }
    assert family is not None and submitted and statuses
    rejected = Counter()
    for key, error in replies.items():
        assert key in requests, "unmatched kernel reply"
        cmd, a = requests[key]
        if error:
            assert cmd == 2 and error == -22 and 1 in a, "registration/status/other error"
            rejected[a[1].hex(":")] += 1
    registration = [k for k, (cmd, _a) in requests.items() if cmd == 1]
    assert len(registration) == 1 and replies.get(registration[0]) == 0
    return submitted, statuses, replies, dict(rejected)


def check(directory):
    health = HEALTH["check"](directory)
    health["netlink"] = HEALTH["check_file"](
        directory / "netlink.pcap", directory / "netlink-capture.log", 253
    )
    submitted, statuses, replies, rejected = medium(directory / "netlink.pcap")
    radio = []
    for timestamp, raw in records(directory / "radio.pcap"):
        header = struct.unpack_from("<H", raw, 2)[0]
        assert 8 <= header <= len(raw)
        packet = raw[header:]
        if (
            len(packet) >= 24
            and packet[4:10] == STA
            and packet[10:16] == AP
            and packet[0] >> 4 != 5  # probe responses predate station creation
        ):
            radio.append((timestamp, packet))
    events = [
        json.loads(line) for line in (directory / "station-events.jsonl").read_text().splitlines()
    ]
    assert events[0]["event"] == "ready" and events[0]["kernel"] == "6.8.0-139-generic"
    assert events[-1]["event"] == "finished" and events[-1]["errors"] == []
    start, sessions = None, []
    for event in events[1:-1]:
        assert event["station"] == STA.hex(":")
        if event["event"] == "new_station":
            assert start is None, "overlapping association lifetime"
            start = event["received_wall_ns"] / 1e9
            continue
        assert event["event"] == "del_station" and start is not None
        stop = event["received_wall_ns"] / 1e9
        selected = [
            (key, value)
            for key, value in submitted.items()
            if start - 0.1 <= value["time"] < stop
            and len(value["mpdu"]) >= 24
            and value["mpdu"][4:10] == STA
            and value["mpdu"][10:16] == AP
            and value["mpdu"][0] >> 4 != 5
        ]
        captured = [p for t, p in radio if start - 0.1 <= t < stop]
        assert selected and Counter(v["mpdu"] for _, v in selected) == Counter(captured), (
            "radio/netlink frame coverage differs"
        )
        failed = retries = protected = octets = 0
        for key, value in selected:
            assert key in statuses, "station TX has no captured completion"
            status = statuses[key]
            assert replies.get(status["request"]) == 0, "kernel did not accept TX completion"
            assert status["time"] < stop and status["time"] >= value["time"]
            p = value["mpdu"]
            assert not p[1] & (4 | 8), "fragmentation/retry frame needs separate qualification"
            assert not scalar(p[22:24], "H") & 15, "nonzero fragment number"
            encrypted = bool(p[1] & 64)
            assert not encrypted or p[0] == 0x88, "unreviewed protected frame form"
            if p[0] == 0x88:
                assert not p[24] & 0x80, "A-MSDU not qualified"
            protected += encrypted
            octets += len(p) - 16 * encrypted
            assert not status["flags"] & 2, "unexpected NO_ACK station frame"
            failed += not bool(status["flags"] & 4)
            retries += sum(count for _, count in status["rates"]) - 1
        observed = event["observed_fields"]
        assert observed["tx_packets"] == len(selected), "kernel TX packet accounting differs"
        assert observed["tx_bytes64"] == octets, "kernel TX byte accounting differs"
        assert observed["tx_failed"] == failed, "kernel failed-frame accounting differs"
        sessions.append(
            {
                "lifetime": event["observed_lifetime"],
                "tx_submissions": len(selected),
                "kernel_tx_bytes": octets,
                "protected_frames": protected,
                "status_acked": len(selected) - failed,
                "status_failed": failed,
                "status_retries": retries,
                "kernel_tx_retries": observed["tx_retries"],
                "retry_counter_matches_medium_attempts": observed["tx_retries"] == retries,
            }
        )
        start = None
    assert len(sessions) >= 3 and len(sessions) == events[-1]["counts"]["del_station"]
    assert sum(s["status_failed"] for s in sessions) > 0
    assert sum(s["status_retries"] for s in sessions) > 0
    return {
        "tx_submission_and_failure_accounting_passed": True,
        "retry_counter_reconciled": all(
            s["retry_counter_matches_medium_attempts"] for s in sessions
        ),
        "scope": "kernel TX submission/status bookkeeping, not actual RF retransmissions",
        "capture_health": health,
        "sessions": sessions,
        "kernel_rejected_rx_clones": rejected,
        "all_radio_submissions": len(submitted),
        "all_captured_statuses": len(statuses),
        "uncompleted_submissions_outside_checked_sessions": len(submitted.keys() - statuses.keys()),
        "medium_airtime_and_rate_qualified": False,
        "raw_counter_passthrough_valid": False,
        "final_counter_source_qualified": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), indent=2))
