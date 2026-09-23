"""Reject evidence corruption rather than converting plausible raw counters."""

import json
import runpy
import struct
from pathlib import Path

import pytest

audit = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/check-medium-accounting.py")
)
pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).resolve().parents[1] / "doc/evidence/medium-loss/native-medium-loss-05"


def test_retained_corpus_preserves_retry_gap_separately_from_failure_accounting():
    result = audit["check"](EVIDENCE)
    assert result["tx_submission_and_failure_accounting_passed"]
    assert not result["retry_counter_reconciled"]
    assert sum(s["status_failed"] for s in result["sessions"]) == 195
    assert sum(s["status_retries"] for s in result["sessions"]) == 2405
    assert not result["final_counter_source_qualified"]


@pytest.mark.parametrize("field", ["tx_packets", "tx_bytes64", "tx_failed"])
def test_changed_final_counter_cannot_pass_captured_status_accounting(tmp_path, field):
    for path in EVIDENCE.iterdir():
        if path.name != "station-events.jsonl":
            (tmp_path / path.name).symlink_to(path)
    events = [
        json.loads(line) for line in (EVIDENCE / "station-events.jsonl").read_text().splitlines()
    ]
    removal = next(e for e in events if e["event"] == "del_station")
    removal["observed_fields"][field] += 1
    (tmp_path / "station-events.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n")
    with pytest.raises(AssertionError, match="accounting differs"):
        audit["check"](tmp_path)


def test_capture_loss_invalidates_accounting_before_plausible_counter_comparison(tmp_path):
    for path in EVIDENCE.iterdir():
        if path.name != "netlink-capture.log":
            (tmp_path / path.name).symlink_to(path)
    text = (EVIDENCE / "netlink-capture.log").read_text()
    (tmp_path / "netlink-capture.log").write_text(
        text.replace("0 packets dropped by kernel", "1 packets dropped by kernel")
    )
    with pytest.raises(AssertionError, match="kernel dropped"):
        audit["check"](tmp_path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2 packets transmitted, 2 received, 0% packet loss, time 1001ms", True),
        ("2 packets transmitted, 2 received, 0.0% packet loss", True),
        ("2 packets transmitted, 1 received, 50% packet loss", False),
        ("2 packets transmitted, 0 received, 100% packet loss", False),
        ("10% packet loss", False),
        ("0% packet loss\n50% packet loss", False),
        ("no statistics", False),
    ],
)
def test_zero_loss_gate_does_not_accept_percentage_suffix(text, expected):
    baseline = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/check-native-onboarding.py")
    )
    assert baseline["zero_packet_loss"](text) is expected


@pytest.mark.parametrize(
    "payload",
    [b"\0", b"\x03\0\x01\0", b"\x05\0\x01\0\x00", b"\x04\0\x01\0" * 2],
)
def test_rejects_truncated_and_duplicate_attributes(payload):
    with pytest.raises(AssertionError):
        audit["attributes"](payload)


@pytest.mark.parametrize(
    "payload",
    [b"\0", struct.pack("<IHHII", 99, 39, 0, 0, 0), struct.pack("<IHHII", 16, 4, 0, 0, 0)],
)
def test_rejects_truncated_netlink_and_overrun(payload):
    with pytest.raises(AssertionError):
        list(audit["messages"](payload))


def test_retry_chain_counts_attempts_across_rates_and_stops_at_sentinel():
    # Real loss status from the first owned trial: 1+2+2+2 attempts = 6 retries.
    chain = audit["rates"](bytes.fromhex("0801000200020002"))
    assert sum(count for _, count in chain) - 1 == 6
    assert audit["rates"](bytes.fromhex("000bff00ff00ff00")) == [(0, 11)]


@pytest.mark.parametrize(
    "payload", ["0000ff00ff00ff00", "0001ff000001ff00", "0001", "ff00ff00ff00ff00"]
)
def test_unusable_rate_chain_cannot_silently_mean_zero_retries(payload):
    with pytest.raises(AssertionError):
        audit["rates"](bytes.fromhex(payload))
