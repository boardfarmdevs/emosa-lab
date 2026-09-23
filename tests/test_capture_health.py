import runpy
import struct
from pathlib import Path

import pytest

check = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/check-capture-health.py")
)["check_file"]
pytestmark = pytest.mark.unit


def fixture(tmp_path, *, captured=1, received=1, dropped=0, size=60, original=60, payload=60):
    capture, log = tmp_path / "input.pcap", tmp_path / "capture.log"
    capture.write_bytes(
        bytes.fromhex("d4c3b2a1")
        + struct.pack("<HHiiII", 2, 4, 0, 0, 262144, 1)
        + struct.pack("<IIII", 123, 0, size, original)
        + bytes(payload)
    )
    log.write_text(
        f"{captured} packets captured\n{received} packets received by filter\n"
        f"{dropped} packets dropped by kernel\n"
    )
    return capture, log


def test_capture_health_matches_records_and_reports_zero_loss(tmp_path):
    capture, log = fixture(tmp_path)
    assert check(capture, log, 1) == {
        "passed": True,
        "linktype": 1,
        "pcap_records": 1,
        "captured": 1,
        "received_by_filter": 1,
        "dropped_by_kernel": 0,
    }


@pytest.mark.parametrize(
    "options",
    [
        {"dropped": 1},
        {"captured": 2},
        {"received": 2},
        {"size": 60, "original": 80},
        {"payload": 50},
        {"payload": 65},
        {"size": 0, "original": 0, "payload": 0},
    ],
)
def test_loss_count_mismatch_or_truncation_cannot_pass(tmp_path, options):
    capture, log = fixture(tmp_path, **options)
    with pytest.raises(AssertionError):
        check(capture, log, 1)


@pytest.mark.parametrize("content", ["", "1 packets captured\n", "twice"])
def test_missing_or_duplicate_capture_statistics_rejected(tmp_path, content):
    capture, log = fixture(tmp_path)
    log.write_text(log.read_text() * 2 if content == "twice" else content)
    with pytest.raises(AssertionError):
        check(capture, log, 1)


def test_wrong_capture_type_cannot_count_as_ethernet(tmp_path):
    capture, log = fixture(tmp_path)
    with pytest.raises(AssertionError):
        check(capture, log, 127)
