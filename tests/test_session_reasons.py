import copy
import struct

import pytest

from emosa.simulation.session_reasons import (
    BSSID,
    RAW_FIELDS,
    STATION,
    ClockBounds,
    ReasonJoin,
    reason_frame,
)

pytestmark = pytest.mark.unit
SECOND = 1_000_000_000
WALL = 100 * SECOND
MONO = 10 * SECOND


def test_clock_read_retries_preemption_without_inventing_a_clock_step():
    monotonic = iter((0, 2_000_000, 2_000_000, 2_000_100))
    wall = iter((WALL, WALL + 2_000_050))
    clock = ClockBounds(monotonic=monotonic.__next__, wall=wall.__next__)
    assert clock.lower == WALL - 50 and clock.upper == WALL + 50
    assert clock.check(
        {
            "monotonic_ns": 3_000_000,
            "monotonic_after_ns": 3_000_100,
            "wall_ns": WALL + 3_000_050,
        }
    ) == {"lower_offset_ns": WALL - 50, "upper_offset_ns": WALL + 50}


def test_uncertain_clock_reads_exhaust_a_fixed_retry_budget():
    monotonic = iter((0, 200_000, 300_000, 500_000, 600_000, 800_000))
    with pytest.raises(ValueError, match="clock_read_uncertainty"):
        ClockBounds(monotonic=monotonic.__next__, wall=lambda: WALL)


def test_actual_clock_step_still_fails_the_original_one_millisecond_limit():
    clock = ClockBounds(monotonic=lambda: 0, wall=lambda: WALL)
    with pytest.raises(ValueError, match="clock_domain_changed"):
        clock.check({"monotonic_ns": 0, "monotonic_after_ns": 0, "wall_ns": WALL + 1_000_001})
    with pytest.raises(ValueError, match="clock_read_uncertainty"):
        clock.check({"monotonic_ns": 0, "monotonic_after_ns": 200_000, "wall_ns": WALL})


def packet(*, reason=3, retry=False, sender=STATION, protected=False, seq=16, subtype=0xC0):
    radiotap = bytes.fromhex("000016000f000000000000000000000000028509a000")
    fc = subtype | (0x800 if retry else 0) | (0x4000 if protected else 0)
    receiver = BSSID if sender == STATION else STATION
    return (
        radiotap
        + struct.pack("<HH", fc, 0)
        + receiver
        + sender
        + BSSID
        + struct.pack("<HH", seq, reason)
    )


def kernel(kind, elapsed, lifetime=1):
    return {
        "event": kind,
        "station": STATION.hex(":"),
        "ifindex": 17,
        "observed_lifetime": lifetime,
        "received_monotonic_ns": MONO + int(elapsed * SECOND),
        "received_wall_ns": WALL + int(elapsed * SECOND),
        "observed_fields": {name: i + 1 for i, name in enumerate(RAW_FIELDS)},
    }


def reason(**kwargs):
    return reason_frame(packet(**kwargs), WALL + 2 * SECOND)


@pytest.mark.parametrize("radio_first", [True, False])
@pytest.mark.parametrize("sender", [STATION, BSSID])
def test_actual_reason_and_raw_final_counters_join_in_either_delivery_order(radio_first, sender):
    join = ReasonJoin("collector-epoch")
    join.kernel(kernel("new_station", 1))
    event = kernel("del_station", 2.01)
    if radio_first:
        join.radio(reason(sender=sender), WALL + 2 * SECOND)
    join.kernel(event)
    if not radio_first:
        join.radio(reason(sender=sender), WALL + int(2.05 * SECOND))
    assert join.poll(WALL + int(2.1 * SECOND), MONO + int(2.1 * SECOND)) is None
    result = join.poll(WALL + int(2.4 * SECOND), MONO + int(2.4 * SECOND))
    assert result["disconnect_frame"]["reason"] == 3
    assert result["raw_counters"] == event["observed_fields"]
    assert result["collector_epoch"] == "collector-epoch"
    assert not result["final_counter_source_qualified"]
    assert not result["native_final_statistics_delivery_proven"]
    assert join.poll(WALL + 3 * SECOND, MONO + 3 * SECOND) is None


def test_identical_radio_retry_does_not_create_two_final_records():
    join = ReasonJoin("epoch")
    join.kernel(kernel("new_station", 1))
    for retry in (False, True):
        join.radio(reason(retry=retry), WALL + 2 * SECOND)
    join.kernel(kernel("del_station", 2.01))
    assert join.poll(WALL + int(2.4 * SECOND), MONO + int(2.4 * SECOND))
    assert join.joined == 1


def test_conflict_arriving_after_join_invalidates_the_source():
    join = ReasonJoin("epoch")
    join.kernel(kernel("new_station", 1))
    join.radio(reason(), WALL + 2 * SECOND)
    join.kernel(kernel("del_station", 2.01))
    assert join.poll(WALL + int(2.4 * SECOND), MONO + int(2.4 * SECOND))
    join.radio(reason(retry=True), WALL + int(2.5 * SECOND))
    assert not join.frames
    with pytest.raises(ValueError, match="late_conflicting_disconnect_reason"):
        join.radio(reason(reason=8), WALL + int(2.5 * SECOND))


@pytest.mark.parametrize("change", [{"reason": 8}, {"seq": 32}, {"sender": BSSID}])
def test_ambiguous_reason_is_never_selected_arbitrarily(change):
    join = ReasonJoin("epoch")
    join.kernel(kernel("new_station", 1))
    join.radio(reason(), WALL + 2 * SECOND)
    join.radio(reason(**change), WALL + 2 * SECOND)
    join.kernel(kernel("del_station", 2.01))
    with pytest.raises(ValueError, match="ambiguous_disconnect_reason"):
        join.poll(WALL + int(2.4 * SECOND), MONO + int(2.4 * SECOND))


def test_missing_reason_expires_without_inventing_one():
    join = ReasonJoin("epoch")
    join.kernel(kernel("new_station", 1))
    join.kernel(kernel("del_station", 2.01))
    assert join.poll(WALL + int(2.4 * SECOND), MONO + int(2.4 * SECOND)) is None
    with pytest.raises(ValueError, match="deadline_expired"):
        join.poll(WALL + 4 * SECOND, MONO + 4 * SECOND)


@pytest.mark.parametrize("case", ["missing", "negative", "zero_epoch", "different_interface"])
def test_incomplete_or_misbound_final_kernel_sample_is_rejected(case):
    join = ReasonJoin("epoch")
    join.kernel(kernel("new_station", 1))
    event = kernel("del_station", 2)
    if case == "missing":
        del event["observed_fields"]["tx_retries"]
    elif case == "negative":
        event["observed_fields"]["tx_bytes64"] = -1
    elif case == "zero_epoch":
        event["observed_fields"]["assoc_at_boottime_ns"] = 0
    else:
        event["ifindex"] = 18
    with pytest.raises(ValueError):
        join.kernel(event)


def test_reassociation_cannot_relabel_a_pending_final_sample():
    join = ReasonJoin("epoch")
    join.kernel(kernel("new_station", 1))
    join.kernel(kernel("del_station", 2))
    with pytest.raises(ValueError, match="reassociation_before_final_join"):
        join.kernel(kernel("new_station", 2.1, lifetime=2))


def test_replayed_lifetime_and_orphan_removal_are_rejected():
    join = ReasonJoin("epoch")
    with pytest.raises(ValueError, match="unbound_station_removal"):
        join.kernel(kernel("del_station", 2))
    join = ReasonJoin("epoch")
    with pytest.raises(ValueError, match="missing_or_replayed_station_lifetime"):
        join.kernel(kernel("new_station", 1, lifetime=2))


@pytest.mark.parametrize("offset", [-1, 1_000_000_000])
def test_stale_or_future_radio_input_cannot_renew_an_event(offset):
    join = ReasonJoin("epoch")
    with pytest.raises(ValueError, match="late_or_future"):
        join.radio(reason(), WALL + 2 * SECOND + offset)


@pytest.mark.parametrize(
    "change", [{"protected": True}, {"seq": 17}, {"reason": 0}, {"reason": 40}]
)
def test_unsupported_or_reserved_wire_reason_is_rejected(change):
    with pytest.raises(ValueError):
        reason_frame(packet(**change), WALL)


def test_foreign_bss_and_unrelated_traffic_are_not_a_disconnect():
    frame = bytearray(packet())
    frame[22 + 16] ^= 4
    assert reason_frame(bytes(frame), WALL) is None
    assert reason_frame(packet(subtype=0x80), WALL) is None
    with pytest.raises(ValueError, match="unsupported_hwsim_radiotap"):
        reason_frame(b"\x01" + packet()[1:], WALL)
    with pytest.raises(ValueError, match="truncated_monitor_packet"):
        reason_frame(packet()[:20], WALL)
    ack = bytes.fromhex("00000e000a000000000085098000d4000000020000ec0100")
    assert reason_frame(ack, WALL) is None


def test_reason_before_this_association_cannot_be_reused():
    join = ReasonJoin("epoch")
    old = copy.deepcopy(reason())
    old["wall_ns"] = WALL
    join.radio(old, WALL)
    join.kernel(kernel("new_station", 1))
    join.kernel(kernel("del_station", 2.01))
    assert join.poll(WALL + int(2.4 * SECOND), MONO + int(2.4 * SECOND)) is None
