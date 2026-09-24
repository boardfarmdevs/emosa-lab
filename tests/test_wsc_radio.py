"""Authenticated radio payload semantics; no IEEE procedure or pod authority."""

import pytest

from emosa.errors import EmosaError, Reason
from emosa.wsc import authenticate_message, encode_attribute
from emosa.wsc_messages import WFA_ID, M1Transcript
from emosa.wsc_radio import decode_radio_payloads
from test_wsc_messages import (
    altered_settings,
    device,
    pair,
    raw,
    rewrite,
    second_m2,
    signed,
    transcript,
)

pytestmark = pytest.mark.unit


def role_value(flags):
    return WFA_ID + bytes([6, 1, flags])


def with_role(flags):
    return altered_settings(rewrite(raw("ap_settings"), 0x1049, role_value(flags)))


def request(message=None):
    return decode_radio_payloads(
        transcript(), (raw("m2") if message is None else message,), max_bss=1
    )


def test_independent_native_fronthaul_and_teardown_payloads():
    result = request()
    assert result.action == "configure"
    assert result.bsses[0].multi_ap_flags == 0x20
    candidate = result.existing_fronthaul_candidate()
    assert candidate.ssid == "EMOSA-payload-vector"
    assert candidate.passphrase == "public-vector-passphrase"
    assert candidate.bss_index == 1
    teardown = request(raw("teardown_m2"))
    assert teardown.action == "teardown" and teardown.bsses == ()
    with pytest.raises(EmosaError) as error:
        teardown.existing_fronthaul_candidate()
    assert error.value.code == Reason.UNSUPPORTED_OPERATION
    for secret in (candidate.ssid, candidate.passphrase):
        assert secret not in repr(result) + repr(result.bsses[0]) + repr(candidate) + str(
            error.value
        )


@pytest.mark.parametrize("reserved", [0, 1, 2, 3])
def test_reserved_role_bits_are_ignored(reserved):
    result = request(with_role(0x20 | reserved))
    assert result.bsses[0].multi_ap_flags == 0x20
    assert result.existing_fronthaul_candidate().ssid == "EMOSA-payload-vector"


@pytest.mark.parametrize("flags", [0, 0x40, 0x60, 0x80, 0xA0, 0xE0, 0x24, 0x28, 0x2C])
def test_other_roles_are_preserved_and_never_partially_mapped(flags):
    result = request(with_role(flags))
    assert result.bsses[0].multi_ap_flags == flags
    with pytest.raises(EmosaError) as error:
        result.existing_fronthaul_candidate()
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


@pytest.mark.parametrize(
    "value",
    [
        WFA_ID + b"\x00\x01\x20",
        WFA_ID + b"\x06\x00",
        WFA_ID + b"\x06\x02\x20\x20",
        role_value(0x20) + b"\x06\x01\x20",
        b"\x00\x11\x22\x06\x01\x20",
    ],
)
def test_missing_malformed_ambiguous_or_wrong_vendor_role_is_rejected(value):
    with pytest.raises(EmosaError):
        request(altered_settings(rewrite(raw("ap_settings"), 0x1049, value)))


def test_role_must_be_in_encrypted_config_data_and_unique_across_extensions():
    outer = rewrite(raw("m2")[:-12], 0x1049, role_value(0x20))
    duplicate = raw("ap_settings") + encode_attribute(0x1049, role_value(0x20))
    for message in (signed(outer), altered_settings(duplicate)):
        with pytest.raises(EmosaError):
            request(message)


def test_optional_extensions_and_legacy_key_index_remain_ignored():
    plain = raw("ap_settings") + encode_attribute(0x1028, b"\xff")
    plain += encode_attribute(0x1049, WFA_ID + b"\xfe\x01\x99")
    plain += encode_attribute(0x1049, b"\x00\x11\x22\x06\x01\x40")
    assert (
        request(altered_settings(plain)).existing_fronthaul_candidate().ssid
        == "EMOSA-payload-vector"
    )


@pytest.mark.parametrize("flags", [0x10, 0x30, 0xF0, 0xFF])
def test_teardown_ignores_other_setting_values_after_authentication(flags):
    plain = encode_attribute(0x1049, role_value(flags))
    plain += encode_attribute(0x1045, b"invalid-too-long-ssid" * 4)
    plain += encode_attribute(0x102A, b"ignored-password-without-id")
    result = request(altered_settings(plain))
    assert result.action == "teardown" and not result.bsses
    with pytest.raises(EmosaError):
        transcript().authenticate_m2(altered_settings(plain))  # ordinary AP parser still strict


def test_invalid_authentication_or_conflicting_batch_cannot_become_teardown():
    bad = raw("teardown_m2")[:-1] + bytes([raw("teardown_m2")[-1] ^ 1])
    for messages in ((bad,), (raw("teardown_m2"), second_m2()), (raw("m2"), b"bad")):
        with pytest.raises(EmosaError):
            decode_radio_payloads(transcript(), messages, max_bss=2)


def test_multiple_valid_bsses_cannot_be_reduced_to_a_single_patch():
    result = decode_radio_payloads(transcript(), (raw("m2"), second_m2()), max_bss=2)
    assert len(result.bsses) == 2
    with pytest.raises(EmosaError) as error:
        result.existing_fronthaul_candidate()
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


@pytest.mark.parametrize(
    "kind,value",
    [(0x1003, b"\x00\x60"), (0x100F, b"\x00\x0c"), (0x1003, b"\x02\x00"), (0x100F, b"\x00\x01")],
)
def test_unsupported_security_is_not_downgraded(kind, value):
    plain = rewrite(raw("ap_settings"), kind, value)
    with pytest.raises(EmosaError) as error:
        request(altered_settings(plain)).existing_fronthaul_candidate()
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


@pytest.mark.parametrize(
    "key",
    [b"short", b"a" * 64, b"a" * 7 + b"\x7f", b"a" * 7 + b"\x80", b"password\0\0", b"pass\0word"],
)
def test_keys_outside_existing_mapping_are_never_truncated_or_substituted(key):
    plain = rewrite(raw("ap_settings"), 0x1027, key)
    with pytest.raises(EmosaError) as error:
        request(altered_settings(plain)).existing_fronthaul_candidate()
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


def test_legacy_passphrase_terminator_and_utf8_mapping_boundary():
    plain = rewrite(raw("ap_settings"), 0x1027, b"public-vector-passphrase\0")
    assert (
        request(altered_settings(plain)).existing_fronthaul_candidate().passphrase
        == "public-vector-passphrase"
    )
    for ssid in (b"", b"embedded\0ssid", b"\xff"):
        plain = rewrite(raw("ap_settings"), 0x1045, ssid)
        with pytest.raises(EmosaError) as error:
            request(altered_settings(plain)).existing_fronthaul_candidate()
        assert error.value.code == Reason.UNSUPPORTED_OPERATION


def test_one_trailing_ssid_terminator_is_accepted_other_nuls_are_not():
    plain = rewrite(raw("ap_settings"), 0x1045, b"private_ssid\0")
    assert request(altered_settings(plain)).existing_fronthaul_candidate().ssid == "private_ssid"
    for ssid in (b"private_ssid\0\0", b"\0"):
        plain = rewrite(raw("ap_settings"), 0x1045, ssid)
        with pytest.raises(EmosaError) as error:
            request(altered_settings(plain)).existing_fronthaul_candidate()
        assert error.value.code == Reason.UNSUPPORTED_OPERATION


def test_password_change_is_not_silently_omitted_from_the_candidate():
    plain = raw("ap_settings") + encode_attribute(0x102A, b"new-public-password")
    plain += encode_attribute(0x1012, b"\x00\x00")
    with pytest.raises(EmosaError) as error:
        request(altered_settings(plain)).existing_fronthaul_candidate()
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


@pytest.mark.parametrize("field,value", [(0x1004, b"\x00\x02"), (0x1010, b"\x00\x04")])
def test_candidate_must_have_been_advertised_in_the_original_m1(field, value):
    original = transcript()
    changed = M1Transcript(rewrite(original.message, field, value), original._key_pair)
    # Registrar has the original DH material but authenticates the altered M1.
    keys = pair().derive(
        raw("registrar_public"), bytes(range(16)), device().al_mac, bytes(range(0x10, 0x20))
    )
    message = authenticate_message(keys, changed.message, raw("m2")[:-12])
    with pytest.raises(EmosaError) as error:
        decode_radio_payloads(changed, (message,), max_bss=1).existing_fronthaul_candidate()
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


def test_role_semantics_wait_until_the_entire_payload_set_is_authenticated(monkeypatch):
    def forbidden(*args):
        raise AssertionError("Semantic settings parser ran before full-batch authentication")

    from emosa.wsc_messages import AuthenticatedM2Envelope

    monkeypatch.setattr(AuthenticatedM2Envelope, "ap_configuration", forbidden)
    with pytest.raises(EmosaError):
        decode_radio_payloads(transcript(), (raw("m2"), second_m2()[:-1]), max_bss=2)
