"""Source-derived asymmetric cases; native negative is independently extracted."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from emosa.easymesh_payloads import APWifi6Capabilities, Wifi6Role, decode_value, encode_value
from emosa.errors import EmosaError
from emosa.he_mcs import HEMCSPair, HESupportedMCS, decode_he_mcs, encode_he_mcs
from emosa_lab.evaluation.cli import main
from emosa_lab.evaluation.payloads import describe
from emosa_lab.evaluation.profile_audit import audit

pytestmark = pytest.mark.unit
RUID = bytes.fromhex("020000014001")
# Each literal follows Figure 9-901 and Table 9-378, calculated independently
# of the encoder. Distinct Rx/Tx words, octets and widths expose swaps.
BASE = HEMCSPair((0, 1, 2, 3, 3, 2, 1, 0), (2, 1, 0, 3, 0, 1, 2, 3))
W160 = HEMCSPair((2, 2, 3, 3, 3, 3, 3, 3), (1, 3, 3, 3, 3, 3, 3, 3))
W8080 = HEMCSPair((0, 0, 0, 3, 3, 3, 3, 3), (2, 1, 3, 3, 3, 3, 3, 3))
VECTORS = [
    (HESupportedMCS(BASE), "e41bc6e4", "04"),
    (HESupportedMCS(BASE, W160), "e41bc6e4fafffdff", "28"),
    (HESupportedMCS(BASE, None, W8080), "e41bc6e4c0fff6ff", "18"),
    (HESupportedMCS(BASE, W160, W8080), "e41bc6e4fafffdffc0fff6ff", "3c"),
]


DEFAULT_MCS = HESupportedMCS(BASE)


def role(mcs=DEFAULT_MCS, identifier=0):
    return Wifi6Role(identifier, mcs, 0xA5, 0x39, 0x12, 0x34, 0x5A)


@pytest.mark.parametrize("mcs,raw,flags", VECTORS)
def test_ieee_asymmetric_width_pairs_and_full_wifi6_value(mcs, raw, flags):
    expected = bytes.fromhex(raw)
    assert encode_he_mcs(mcs) == expected
    assert (
        decode_he_mcs(expected, he160=mcs.mhz160 is not None, he8080=mcs.mhz80plus80 is not None)
        == mcs
    )
    payload = APWifi6Capabilities(RUID, (role(mcs),))
    value = RUID + bytes.fromhex("01" + flags + raw + "a53912345a")
    assert encode_value(payload) == value
    assert decode_value(0xAA, value) == payload
    for bad in [value[:n] for n in range(len(value))] + [value + b"\0"]:
        with pytest.raises(EmosaError):
            decode_value(0xAA, bad)


def test_octet_order_rx_tx_and_width_identity_are_not_interchangeable():
    value = bytes.fromhex("e41bc6e4")
    for wrong in (value[::-1], value[2:] + value[:2], bytes.fromhex("1be4e4c6")):
        assert decode_he_mcs(wrong, he160=False, he8080=False).up_to_80 != BASE
    # Equal length is insufficient to determine which optional width is present.
    value = bytes.fromhex("e41bc6e4fafffdff")
    assert decode_he_mcs(value, he160=False, he8080=True) == HESupportedMCS(BASE, None, W160)


def test_each_nss_map_code_and_unsupported_are_preserved():
    for nss in range(8):
        for code in range(4):
            word = (0xFFFF & ~(3 << (2 * nss))) | (code << (2 * nss))
            value = word.to_bytes(2, "little") + b"\xff\xff"
            result = decode_he_mcs(value, he160=False, he8080=False)
            assert result.up_to_80.rx == tuple(code if n == nss else 3 for n in range(8))
            assert result.up_to_80.tx == (3,) * 8
            assert encode_he_mcs(result) == value


@pytest.mark.parametrize(
    "he160,he8080", [(False, False), (False, True), (True, False), (True, True)]
)
def test_every_length_must_match_widths(he160, he8080):
    expected = 4 + 4 * he160 + 4 * he8080
    for length in range(17):
        if length != expected:
            with pytest.raises(EmosaError):
                decode_he_mcs(bytes(length), he160=he160, he8080=he8080)
    for length in range(16):
        flags = 32 * he160 + 16 * he8080 + length
        raw = RUID + bytes((1, flags)) + bytes(length + 5)
        if length != expected:
            with pytest.raises(EmosaError):
                decode_value(0xAA, raw)
        else:
            assert encode_value(decode_value(0xAA, raw)) == raw


@pytest.mark.parametrize("identifier", [0, 1, 2, 3])
def test_reserved_roles_remain_diagnostic_and_cannot_be_sent(identifier):
    raw = RUID + bytes((1, (identifier << 6) | 4)) + bytes.fromhex("e41bc6e4a53912345a")
    result = decode_value(0xAA, raw)
    info = describe(result)["roles"][0]
    assert result.roles[0].role == identifier
    assert info["known_role"] == {0: "ap", 1: "non_ap_sta"}.get(identifier)
    assert info["user_limits_apply_to_ap_role"] == (identifier == 0)
    if identifier in (2, 3):
        with pytest.raises(EmosaError):
            encode_value(result)
    else:
        assert encode_value(result) == raw


def test_roles_have_independent_widths_counts_and_limits():
    payload = APWifi6Capabilities(RUID, (role(), role(HESupportedMCS(BASE, None, W8080), 1)))
    info = describe(decode_value(0xAA, encode_value(payload)))
    assert [r["mcs_length"] for r in info["roles"]] == [4, 8]
    ap = info["roles"][0]
    assert ap["max_dl_mu_mimo_tx_users"] == 3 and ap["max_ul_mu_mimo_rx_users"] == 9
    assert ap["max_dl_ofdma_tx_users"] == 18 and ap["max_ul_ofdma_rx_users"] == 52
    assert ap["features"]["su_beamformer"] and not ap["features"]["su_beamformee"]
    assert ap["features"]["spatial_reuse"] and not ap["features"]["anticipated_channel_usage"]
    assert ap["mcs"]["up_to_80_mhz"]["rx_codes_nss_1_to_8"] == list(BASE.rx)
    # Table 95's count alone does not qualify any actual role inventory.
    assert encode_value(decode_value(0xAA, RUID + b"\0")) == RUID + b"\0"
    with pytest.raises(EmosaError):
        decode_value(0xAA, RUID + b"\x02" + encode_value(APWifi6Capabilities(RUID, (role(),)))[7:])


@pytest.mark.parametrize(
    "value",
    [None, HESupportedMCS(None), HESupportedMCS(BASE, False), HESupportedMCS(BASE, mhz80plus80=3)],
)
def test_invalid_he_objects(value):
    with pytest.raises(EmosaError):
        encode_he_mcs(value)


@pytest.mark.parametrize(
    "codes", [(), (0,) * 7, (0,) * 9, [0] * 8, (True,) * 8, (1.0,) * 8, (-1,) * 8, (4,) * 8]
)
def test_invalid_nss_codes(codes):
    with pytest.raises(EmosaError):
        encode_he_mcs(HESupportedMCS(HEMCSPair(codes, BASE.tx)))


@pytest.mark.parametrize("bad", [1, 0, None, "false"])
def test_width_inputs_are_explicit_booleans(bad):
    with pytest.raises(EmosaError):
        decode_he_mcs(b"\0" * 4, he160=bad, he8080=False)
    with pytest.raises(EmosaError):
        decode_he_mcs(b"\0" * 4, he160=False, he8080=bad)


@pytest.mark.parametrize(
    "field",
    [
        "role",
        "beamforming_flags",
        "mu_mimo_users",
        "max_dl_ofdma_tx",
        "max_ul_ofdma_rx",
        "feature_flags",
    ],
)
def test_role_numeric_fields_reject_wrong_types_and_ranges(field):
    for bad in (True, 1.0, -1, 256):
        with pytest.raises(EmosaError):
            encode_value(APWifi6Capabilities(RUID, (replace(role(), **{field: bad}),)))


def test_bad_container_and_count_inputs():
    for payload in (
        APWifi6Capabilities(b"short", (role(),)),
        APWifi6Capabilities(RUID, [role()]),
        APWifi6Capabilities(RUID, (None,)),
        APWifi6Capabilities(RUID, (role(),) * 256),
    ):
        with pytest.raises(EmosaError):
            encode_value(payload)
    with pytest.raises(EmosaError):
        decode_he_mcs(bytearray(4), he160=False, he8080=False)


def test_native_zero_length_is_rejected_without_silently_repairing_capture():
    fixture = Path(__file__).parent / "fixtures/protocol/easymesh/native-values.json"
    cases = json.loads(fixture.read_text())["rejected_cases"]
    assert len(cases) == 1
    case = cases[0]
    assert case["frame"] == 22 and case["type"] == "0xaa"
    assert case["observed"]["role_flags"] == [0, 64]
    assert all(flags & 15 == 0 for flags in case["observed"]["role_flags"])
    with pytest.raises(EmosaError):
        decode_value(0xAA, bytes.fromhex(case["value_hex"]))


def test_cli_and_audit_do_not_promote_standalone_codec_to_profile_readiness(capsys):
    value = "0200000140010104e41bc6e4a53912345a"
    assert main(["payload", "--type", "0xaa", "--value-hex", value]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decoded"]["roles"][0]["mcs_length"] == 4
    assert not result["wire_envelope_validated"]
    assert not result["controller_onboarding_by_emosa_proven"] and not result["physical_pod_proven"]
    result = audit()
    row = next(r for r in result["ap_capability_report"]["obligations"] if r["type"] == "0xaa")
    assert row["value_component_available"] and not row["qualified_value_available"]
    assert not result["ap_capability_report"]["ready"] and not result["profile_advertisement_ready"]
