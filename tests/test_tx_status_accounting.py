"""Counter suppression and trace corruption must remain visible."""

import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
audit = runpy.run_path(str(ROOT / "scripts/check-tx-status-accounting.py"))
collector = runpy.run_path(str(ROOT / "deploy/radio-manager/tx-status-trace.py"))
pytestmark = pytest.mark.unit
EVIDENCE = ROOT / "doc/evidence/tx-status/native-tx-status-01"


def test_retained_trace_explains_the_retry_delta_without_qualifying_passthrough():
    result = audit["check"](EVIDENCE)
    assert result["kernel_retry_discrepancy_explained"]
    assert len(result["sessions"]) == 10
    assert sum(s["medium_status_retries"] for s in result["sessions"]) == 2421
    assert (
        sum(s["retries_suppressed_by_missing_aggregate_status"] for s in result["sessions"]) == 2094
    )
    assert sum(s["predicted_and_observed_kernel_retries"] for s in result["sessions"]) == 327
    assert not result["raw_counter_passthrough_valid"]
    assert not result["online_measurement_source_qualified"]


@pytest.mark.parametrize("change", ["ampdu_flag", "ack_flag", "trace_loss", "missing_record"])
def test_trace_corruption_cannot_explain_a_counter_by_accident(tmp_path, change):
    changed = "tx-status-provenance.json" if change == "trace_loss" else "tx-status.jsonl"
    for path in EVIDENCE.iterdir():
        if path.name != changed:
            (tmp_path / path.name).symlink_to(path)
    if change == "trace_loss":
        data = json.loads((EVIDENCE / changed).read_text())
        cpu = next(iter(data["per_cpu_statistics"]))
        data["per_cpu_statistics"][cpu] = data["per_cpu_statistics"][cpu].replace(
            "overrun: 0", "overrun: 1"
        )
        (tmp_path / changed).write_text(json.dumps(data))
    else:
        rows = [json.loads(line) for line in (EVIDENCE / changed).read_text().splitlines()]
        if change == "missing_record":
            rows.pop()
        else:
            # Change every relevant flag, including the accounted lifetimes.
            for row in rows:
                row["flags"] ^= 64 if change == "ampdu_flag" else 512
        (tmp_path / changed).write_text("\n".join(map(json.dumps, rows)) + "\n")
    with pytest.raises(AssertionError):
        audit["check"](tmp_path)


@pytest.mark.parametrize(
    ("flags", "maximum", "expected"),
    [(0, 4, 6), (64, 4, 0), (64 | 1024, 4, 6), (512, 4, 6), (0, 1, 0)],
)
def test_original_aggregation_flag_and_report_limit_control_retry_bookkeeping(
    flags, maximum, expected
):
    assert audit["expected_retries"](flags, [(8, 1), (0, 2), (0, 2), (0, 2)], maximum) == expected


def test_packed_mac80211_rate_counts_are_distinct_from_driver_rate_flags():
    # Four packed three-byte entries: count in low five bits, rate flags above.
    raw = bytes.fromhex("082103004201004201004201")
    assert audit["decode_rates"](raw) == [(8, 1), (0, 2), (0, 2), (0, 2)]


def trace_line():
    header = bytes.fromhex("88020000020000000200020000ec0200020000ec02001000")
    rates = bytes.fromhex("080100ff1f00ff1f00ff1f00")

    def array(data):
        return "{" + ",".join(hex(b) for b in data) + "}"

    return (
        " ksoftirqd/1-24 [001] ..s.. 123.456789: tx_test: "
        "(ieee80211_tx_status_ext+0x0/0x630 [mac80211]) "
        "station=0xffffdeadbeef0000 flags=0x240 mpdu_len=142 "
        f"header={array(header)} rates={array(rates)} max_rates=4 "
        "ra_lo=0x2 ra_hi=0x2 ta_lo=0xec000002 ta_hi=0x2"
    )


def test_projection_retains_status_flags_but_not_kernel_pointer():
    records = collector["decode"](trace_line(), "tx_test")
    assert len(records) == 1 and records[0]["flags"] == 0x240
    assert records[0]["station_present"] is True
    assert "deadbeef" not in json.dumps(records)
    assert (
        collector["decode"](trace_line().replace("0xffffdeadbeef0000", "0x0"), "tx_test")[0][
            "station_present"
        ]
        is False
    )


@pytest.mark.parametrize("change", ["event", "fault", "width"])
def test_unexpected_trace_format_is_not_silently_skipped(change):
    line = trace_line()
    if change == "event":
        line = line.replace("tx_test:", "different:")
    elif change == "fault":
        line = line.replace("mpdu_len=142", "mpdu_len=(fault)")
    else:
        line = line.replace("rates={0x8,0x1,", "rates={")
    with pytest.raises(ValueError):
        collector["decode"](line, "tx_test")
