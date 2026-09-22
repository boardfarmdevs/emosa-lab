import json

import pytest

from emosa.counter_units import select_counter_units
from emosa.easymesh_payloads import (
    APCapability,
    APRadioAdvancedCapabilities,
    Profile2APCapability,
    decode_value,
    encode_value,
)
from emosa.errors import EmosaError, Reason
from emosa.evaluation.cli import main
from emosa.evaluation.payloads import describe

pytestmark = pytest.mark.unit
RUID = bytes.fromhex("020000014001")


@pytest.mark.parametrize(
    "payload,expected",
    [
        (APCapability(0), "00"),
        (APCapability(0xF8), "f8"),
        (Profile2APCapability(1, 0, 0x78, 2), "01007802"),
        (Profile2APCapability(255, 0, 0x80, 255), "ff0080ff"),
        (APRadioAdvancedCapabilities(RUID, 0), "02000001400100"),
        (APRadioAdvancedCapabilities(RUID, 0xFE), "020000014001fe"),
    ],
)
def test_hand_calculated_feature_table_values(payload, expected):
    value = bytes.fromhex(expected)
    assert encode_value(payload) == value
    assert decode_value(payload.kind, value) == payload
    for invalid in [value[:n] for n in range(len(value))] + [value + b"\0"]:
        with pytest.raises(EmosaError):
            decode_value(payload.kind, invalid)


@pytest.mark.parametrize(
    "bit,name",
    [
        (7, "unassociated_metrics_on_channel"),
        (6, "unassociated_metrics_off_channel"),
        (5, "agent_initiated_rcpi_steering"),
        (4, "m8_backhaul_sta_reconfiguration"),
        (3, "rsn_overriding"),
    ],
)
def test_ap_capability_feature_bit_meanings_from_table_29(bit, name):
    features = describe(decode_value(0xA1, bytes((1 << bit,))))["features"]
    assert {k for k, v in features.items() if v} == {name}


@pytest.mark.parametrize(
    "kind,prefix,bits,mask",
    [
        (0xA1, b"", 5, 7),
        (0xBE, RUID, 7, 1),
    ],
)
def test_all_flag_values_preserve_reserved_octets_but_do_not_interpret_or_emit_them(
    kind, prefix, bits, mask
):
    for flags in range(256):
        payload = decode_value(kind, prefix + bytes((flags,)))
        result = describe(payload)
        assert result["flags"] == flags and result["reserved_bits"] == flags & mask
        assert sum(result["features"].values()) == (flags & ~mask).bit_count()
        assert len(result["features"]) == bits
        if flags & mask:
            with pytest.raises(EmosaError):
                encode_value(payload)
        else:
            assert encode_value(payload) == prefix + bytes((flags,))


def test_profile2_units_reserved_values_and_octet_are_never_coerced():
    for flags in range(256):
        value = bytes((1, 0, flags, 2))
        payload = decode_value(0xB4, value)
        result = describe(payload)
        units = flags >> 6
        assert payload.byte_counter_units == units
        assert result["counter_unit"] == {0: "bytes", 1: "KiB", 2: "MiB"}.get(units)
        assert result["bytes_per_counter_unit"] == {0: 1, 1: 1024, 2: 1048576}.get(units)
        assert result["features"] == {
            "prioritization": bool(flags & 0x20),
            "dpp_onboarding": bool(flags & 0x10),
            "traffic_separation": bool(flags & 0x08),
        }
        if flags & 7 or units == 3:
            with pytest.raises(EmosaError):
                encode_value(payload)
        else:
            assert encode_value(payload) == value
    for reserved in range(1, 256):
        payload = decode_value(0xB4, bytes((0, reserved, 0, 0)))
        assert describe(payload)["reserved_octet"] == reserved
        assert describe(payload)["features"] == dict.fromkeys(
            ("prioritization", "dpp_onboarding", "traffic_separation"), False
        )
        with pytest.raises(EmosaError):
            encode_value(payload)


@pytest.mark.parametrize(
    "bit,name",
    [
        (7, "combined_fronthaul_backhaul"),
        (6, "combined_profile1_profile2"),
        (5, "mscs"),
        (4, "scs"),
        (3, "qos_map"),
        (2, "dscp_policy"),
        (1, "qm_scs_traffic_description"),
    ],
)
def test_advanced_bits_use_61_meanings_even_when_old_dissector_calls_them_reserved(bit, name):
    result = describe(decode_value(0xBE, RUID + bytes((1 << bit,))))
    assert {k for k, v in result["features"].items() if v} == {name}


@pytest.mark.parametrize(
    "payload",
    [
        APCapability(True),
        APCapability(-1),
        APCapability(256),
        APCapability(1.0),
        APRadioAdvancedCapabilities(b"short", 0),
        APRadioAdvancedCapabilities(RUID, None),
        Profile2APCapability(True, 0, 0, 0),
        Profile2APCapability(0, False, 0, 0),
        Profile2APCapability(256, 0, 0, 0),
        Profile2APCapability(0, 0, 0, 256),
    ],
)
def test_invalid_outbound_feature_values_are_rejected(payload):
    with pytest.raises(EmosaError) as error:
        encode_value(payload)
    assert error.value.code == Reason.INVALID_INPUT


@pytest.mark.parametrize(
    "profile,kib_mib,preferred,expected",
    [
        (1, False, None, 0),
        (1, False, 2, 0),
        (1, True, 1, 1),
        (1, True, 2, 2),
        (2, None, 1, 1),
        (2, False, 2, 2),
        (3, None, 1, 1),
        (3, True, 2, 2),
    ],
)
def test_counter_unit_rule_requires_explicit_peer_facts(profile, kib_mib, preferred, expected):
    assert (
        select_counter_units(
            controller_profile=profile, controller_kib_mib=kib_mib, preferred_scaled_unit=preferred
        )
        == expected
    )


@pytest.mark.parametrize(
    "profile,kib_mib,preferred",
    [
        (None, None, 1),
        (True, False, None),
        (4, True, 1),
        (1.0, False, None),
        (1, None, 1),
        (1, "false", 1),
        (2, 1, 1),
        (2, None, None),
        (3, True, 0),
        (1, True, 3),
        (2, True, True),
        (2, True, 1.0),
    ],
)
def test_unknown_peer_or_invalid_unit_choice_has_no_default(profile, kib_mib, preferred):
    with pytest.raises(EmosaError):
        select_counter_units(
            controller_profile=profile, controller_kib_mib=kib_mib, preferred_scaled_unit=preferred
        )


def test_offline_feature_cli_keeps_units_and_proof_boundaries(capsys):
    assert main(["payload", "--type", "0xb4", "--value-hex", "01007802"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decoded"]["counter_unit"] == "KiB"
    assert result["decoded"]["features"]["dpp_onboarding"]
    assert not result["wire_envelope_validated"]
    assert not result["controller_onboarding_by_emosa_proven"]
    assert not result["physical_pod_proven"]
