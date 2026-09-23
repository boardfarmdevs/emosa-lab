import runpy
import struct

import pytest

pytestmark = pytest.mark.unit
CHECKER = runpy.run_path("scripts/check-backhaul-accounting.py")


def test_counter_read_boundaries_keep_uncertainty_explicit():
    frames = [
        (101_001_000_000, "tx", 80),
        (101_010_000_000, "tx", 100),
        (101_050_000_000, "rx", 120),
        (101_101_000_000, "tx", 80),
    ]
    result = CHECKER["reconcile"](
        frames,
        (1_000_000_000, 1_005_000_000),
        (1_100_000_000, 1_105_000_000),
        [100_000_000_000],
        {"tx_packets": 2, "rx_packets": 1, "tx_bytes": 180, "rx_bytes": 120},
    )
    assert result["tx_packets"] == {"minimum": 1, "observed": 2, "maximum": 3}
    assert result["rx_bytes"] == {"minimum": 120, "observed": 120, "maximum": 120}


@pytest.mark.parametrize(
    "case", ["valid", "missing_frame", "extra_frame", "strip_header", "clock_jump"]
)
def test_reconciliation_rejects_unexplained_counts_and_clock_changes(case):
    frames = [(101_020_000_000, "tx", 98), (101_040_000_000, "rx", 98)]
    offsets = [100_000_000_000]
    if case == "missing_frame":
        frames.pop()
    elif case == "extra_frame":
        frames.append((101_060_000_000, "rx", 98))
    elif case == "strip_header":
        frames[0] = (*frames[0][:2], 84)
    elif case == "clock_jump":
        offsets.append(100_005_000_000)

    def verify():
        return CHECKER["reconcile"](
            frames,
            (1_000_000_000, 1_005_000_000),
            (1_100_000_000, 1_105_000_000),
            offsets,
            {"tx_packets": 1, "rx_packets": 1, "tx_bytes": 98, "rx_bytes": 98},
        )

    if case == "valid":
        assert verify()["tx_bytes"]["observed"] == 98
    else:
        with pytest.raises(AssertionError):
            verify()


@pytest.mark.parametrize("case", ["valid", "truncated", "partial_header", "partial_record"])
def test_counter_audit_rejects_incomplete_capture(case, tmp_path):
    data = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    data += struct.pack("<IIII", 100, 1000, 14, 14 if case != "truncated" else 98) + b"\0" * 14
    if case == "partial_header":
        data += b"\0"
    if case == "partial_record":
        data = data[:-1]
    p = tmp_path / "capture.pcap"
    p.write_bytes(data)
    if case == "valid":
        assert len(CHECKER["packets"](p)) == 1
    else:
        with pytest.raises(AssertionError):
            CHECKER["packets"](p)
