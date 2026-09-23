"""Explain the medium/kernel retry delta using passively captured status flags.

No EMOSA or collector implementation is imported. A match explains this kernel's
bookkeeping; it does not qualify that counter for EasyMesh or physical telemetry.
"""

import argparse
import json
import re
import runpy
import struct
from collections import defaultdict
from pathlib import Path

MEDIUM = runpy.run_path(str(Path(__file__).with_name("check-medium-accounting.py")))
BTF = {
    "/sys/kernel/btf/vmlinux": "6b316ce238ad8dc2686af1a0a7fd5e0d8f454e74ec82e6422194971e03aa2ea7",
    "/sys/kernel/btf/mac80211": "6731e6b80c80a9a5832e131c619d1c45e88495ff4d4673277bdba8a98067ebbe",
}


def decode_rates(raw):
    assert len(raw) == 12, "invalid traced rate array"
    result = []
    for offset in range(0, 12, 3):
        index, bits = struct.unpack_from("<bH", raw, offset)
        if index == -1:
            break
        assert index >= 0 and bits & 31, "invalid traced rate/count"
        result.append((index, bits & 31))
    assert result, "empty traced rate chain"
    return result


def expected_retries(flags, rates, maximum):
    assert type(flags) is int and 0 <= flags <= 0xFFFFFFFF
    assert 1 <= maximum <= 4
    # Exact source: ieee80211_tx_get_rates(), Ubuntu 6.8.0-139.139. A flagged
    # A-MPDU submission without aggregate status contributes zero retries.
    if flags & (1 << 6) and not flags & (1 << 10):
        return 0
    return max(0, sum(count for _index, count in rates[:maximum]) - 1)


def check(directory):
    accounting = MEDIUM["check"](directory)
    submitted, statuses, _replies, _rejections = MEDIUM["medium"](directory / "netlink.pcap")
    provenance = json.loads((directory / "tx-status-provenance.json").read_text())
    assert provenance["kernel"] == "6.8.0-139-generic" and provenance["architecture"] == "x86_64"
    assert provenance["btf_sha256"] == BTF and provenance["clock"] == "mono"
    assert "ieee80211_tx_status_ext" in provenance["probe"]
    assert not provenance["counter_conversion_qualified"]
    traces = [json.loads(line) for line in (directory / "tx-status.jsonl").read_text().splitlines()]
    assert traces
    entries = 0
    for text in provenance["per_cpu_statistics"].values():
        for field in ("overrun", "commit overrun", "dropped events"):
            assert re.findall(r"^" + field + r":\s*(\d+)\s*$", text, re.M) == ["0"], (
                "incomplete trace capture"
            )
        values = re.findall(r"^entries:\s*(\d+)\s*$", text, re.M)
        assert len(values) == 1
        entries += int(values[0])
    assert entries == len(traces), "trace record-count mismatch"
    start = None
    sessions = []
    consumed = set()
    events = [
        json.loads(line) for line in (directory / "station-events.jsonl").read_text().splitlines()
    ]
    for event in events[1:-1]:
        if event["event"] == "new_station":
            start = event
            continue
        assert start is not None
        wall_start, wall_stop = (e["received_wall_ns"] / 1e9 for e in (start, event))
        mono_start, mono_stop = (e["received_monotonic_ns"] / 1e9 for e in (start, event))
        original = []
        for key, value in submitted.items():
            packet = value["mpdu"]
            if (
                wall_start - 0.1 <= value["time"] < wall_stop
                and len(packet) >= 24
                and packet[4:10] == MEDIUM["STA"]
                and packet[10:16] == MEDIUM["AP"]
                and packet[0] >> 4 != 5
            ):
                original.append((key, value))
        by_frame = defaultdict(list)
        overlap = 0
        for index, row in enumerate(traces):
            if mono_start - 0.1 <= row["monotonic_seconds"] < mono_stop:
                # A rapid failed association can end less than 100 ms before
                # the next one. That pre-event allowance must not count its
                # already-correlated completion again in the next lifetime.
                if index in consumed:
                    overlap += 1
                    continue
                header = bytes.fromhex(row["header_hex"])
                assert len(header) == 24
                if header[0] >> 4 != 5:
                    by_frame[(header, row["mpdu_length"])].append((index, row))
        expected = suppressed = flags_missing_status = status_retry_sum = 0
        for key, value in original:
            packet = value["mpdu"]
            matches = by_frame.pop((packet[:24], len(packet)), [])
            assert len(matches) == 1, "missing/ambiguous status trace for captured frame"
            index, row = matches[0]
            consumed.add(index)
            assert row["station_present"], "completion lacks a station context"
            assert row["max_report_rates"] == 4, "review changed hardware report limits"
            chain = decode_rates(bytes.fromhex(row["rates_hex"]))
            assert chain == statuses[key]["rates"], "kernel/medium rate chains differ"
            assert bool(row["flags"] & (1 << 9)) == bool(statuses[key]["flags"] & 4), (
                "kernel/medium ACK flags differ"
            )
            # Compare capture clock offsets through the independently observed
            # station event. Require matching header/length and close completion
            # times; event reception does not pretend to be an RF timestamp.
            trace_wall = row["monotonic_seconds"] + wall_start - mono_start
            assert abs(trace_wall - statuses[key]["time"]) < 0.02, "status times differ"
            medium_count = sum(count for _, count in chain) - 1
            kernel_count = expected_retries(row["flags"], chain, row["max_report_rates"])
            expected += kernel_count
            status_retry_sum += medium_count
            if row["flags"] & 64 and not row["flags"] & 1024:
                flags_missing_status += 1
                suppressed += medium_count
        assert not by_frame, "unmatched traced station completions"
        assert expected == event["observed_fields"]["tx_retries"], (
            "retry discrepancy still unexplained"
        )
        assert expected + suppressed == status_retry_sum
        sessions.append(
            {
                "lifetime": event["observed_lifetime"],
                "traced_completions": len(original),
                "ampdu_flag_without_aggregate_status": flags_missing_status,
                "medium_status_retries": status_retry_sum,
                "retries_suppressed_by_missing_aggregate_status": suppressed,
                "predicted_and_observed_kernel_retries": expected,
                "previous_lifetime_completions_in_pre_event_window": overlap,
            }
        )
        start = None
    assert len(sessions) == len(accounting["sessions"]) >= 3
    assert sum(s["retries_suppressed_by_missing_aggregate_status"] for s in sessions) > 0
    return {
        "kernel_retry_discrepancy_explained": True,
        "scope": "kernel completion flags and retry bookkeeping; not an EasyMesh conversion",
        "capture_health": accounting["capture_health"],
        "loss_checked_trace_records": entries,
        "sessions": sessions,
        "raw_retry_counter_counts_all_medium_attempts": False,
        "raw_counter_passthrough_valid": False,
        "online_measurement_source_qualified": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), indent=2))
