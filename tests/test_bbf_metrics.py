import copy
import runpy
from pathlib import Path

import pytest

from emosa.errors import EmosaError, Reason
from emosa.wire.bbf_metrics import (
    BSS_COUNTERS,
    ap_extended_metrics,
    collection_interval_tlv,
    radio_metrics,
    station_link_metrics,
)

pytestmark = pytest.mark.unit
BSSID = bytes.fromhex("020000000001")
RUID = bytes.fromhex("020000000002")
STA = bytes.fromhex("020000000003")
REVIEW = runpy.run_path(str(Path(__file__).parents[1] / "scripts/review-bbf-data-elements.py"))


def bss_values():
    return dict(zip(BSS_COUNTERS, (0x01020304, 2, 3, 4, 5, 0xFFFFFFFF), strict=True))


def link_values():
    return {
        "LastDataDownlinkRate": 65000,
        "LastDataUplinkRate": 32000,
        "UtilizationReceive": 101,
        "UtilizationTransmit": 0xFFFFFFFF,
    }


def link(values):
    return station_link_metrics(
        values, earliest_measurement=9.875, downlink_mac_mbps=61, uplink_mac_mbps=30, rcpi=120
    )


def test_bbf_units_and_field_order_reach_actual_tlvs():
    assert collection_interval_tlv(1000).encode().hex() == "c50004000003e8"
    radio = radio_metrics(
        RUID, {"Noise": 160, "Transmit": 255, "ReceiveSelf": 21, "ReceiveOther": 0}
    )
    assert radio.tlv().encode().hex() == "c6000a020000000002a0ff1500"
    ap = ap_extended_metrics(BSSID, bss_values(), byte_units=0)
    assert ap.tlv().encode().hex() == (
        "c7001e0200000000010102030400000002000000030000000400000005ffffffff"
    )
    base, extended = link(link_values()).tlvs(STA, BSSID, 10)
    # 125 ms age, estimated MAC rates in Mbps, RCPI; separate last PHY rates
    # remain 65000/32000 kbps and STA durations remain 101/4294967295 ms.
    assert base.encode().hex() == "96001a020000000003010200000000010000007d0000003d0000001e78"
    assert extended.encode().hex() == (
        "c8001d020000000003010200000000010000fde800007d0000000065ffffffff"
    )


@pytest.mark.parametrize("value", [0, 0xFFFFFFFF])
def test_interval_unsigned_boundaries_are_not_statistics_sentinels(value):
    assert int.from_bytes(collection_interval_tlv(value).value) == value


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "1000", 2**32])
def test_bad_collection_interval(value):
    with pytest.raises(EmosaError):
        collection_interval_tlv(value)


@pytest.mark.parametrize("units,scale", [(1, 1024), (2, 1048576)])
def test_scale_wide_bss_bytes_before_width_check(units, scale):
    values = dict.fromkeys(BSS_COUNTERS, 0xFFFFFFFF * scale + scale - 1)
    assert ap_extended_metrics(BSSID, values, byte_units=units).byte_counts == (0xFFFFFFFF,) * 6


@pytest.mark.parametrize("field", BSS_COUNTERS)
@pytest.mark.parametrize("value", [None, 2**64 - 1, 2**32])
def test_bss_unavailable_or_overflow_withholds_whole_record(field, value):
    values = bss_values()
    values[field] = value
    with pytest.raises(EmosaError) as error:
        ap_extended_metrics(BSSID, values, byte_units=0)
    assert error.value.code == Reason.NOT_READY


@pytest.mark.parametrize("units", [True, None, -1, 3])
def test_no_implicit_advertised_counter_units(units):
    with pytest.raises(EmosaError):
        ap_extended_metrics(BSSID, bss_values(), byte_units=units)


def test_missing_fields_are_not_zero_and_no_input_mutation():
    values = bss_values()
    before = copy.deepcopy(values)
    ap_extended_metrics(BSSID, values, byte_units=0)
    assert values == before
    del values["UnicastBytesReceived"]
    with pytest.raises(EmosaError) as error:
        ap_extended_metrics(BSSID, values, byte_units=0)
    assert error.value.code == Reason.NOT_READY


@pytest.mark.parametrize("field", ["UtilizationReceive", "UtilizationTransmit"])
@pytest.mark.parametrize("value", [2**32, 2**64 - 1])
def test_station_duration_does_not_borrow_table58_rollover(field, value):
    values = link_values()
    values[field] = value
    with pytest.raises(EmosaError) as error:
        link(values)
    assert error.value.code == Reason.NOT_READY


def test_base_metrics_keep_existing_age_and_rcpi_guards():
    with pytest.raises(EmosaError):
        link(link_values()).tlvs(STA, BSSID, 30)
    result = station_link_metrics(
        link_values(), earliest_measurement=9, downlink_mac_mbps=61, uplink_mac_mbps=30, rcpi=221
    )
    with pytest.raises(EmosaError):
        result.tlvs(STA, BSSID, 10)


@pytest.mark.parametrize("value", [-92, 256, True, None])
def test_radio_requires_encoded_values_without_fabricated_conversion(value):
    with pytest.raises(EmosaError):
        radio_metrics(RUID, {"Noise": value, "Transmit": 1, "ReceiveSelf": 2, "ReceiveOther": 3})


def test_review_rejects_altered_local_source_without_overwriting_it(tmp_path):
    source = tmp_path / "usp-tr-181-2-17-0-usp-full.xml"
    source.write_bytes(b"<altered/>")
    with pytest.raises(ValueError, match="source hash mismatch"):
        REVIEW["review"](tmp_path, offline=True)
    assert source.read_bytes() == b"<altered/>"


def test_review_cannot_accept_a_different_unit_definition():
    left = {"parameters": {"UtilizationReceive": {"units": "milliseconds"}}}
    right = copy.deepcopy(left)
    right["parameters"]["UtilizationReceive"]["units"] = "percent"
    with pytest.raises(ValueError, match="definitions differ"):
        REVIEW["compare"](left, right)
