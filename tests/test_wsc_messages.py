"""Independent payload vectors and authenticated hostile inputs; no wire or pod I/O."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import dh

from emosa import wsc_messages
from emosa.errors import EmosaError, Reason
from emosa.wsc import (
    MODP_1536,
    KeyPair,
    SessionKeys,
    authenticate_message,
    decode_attributes,
    encode_attribute,
    encrypt_settings,
)
from emosa.wsc_messages import M1Device, M1Transcript

pytestmark = pytest.mark.unit
CASE = json.loads(
    (Path(__file__).parent / "fixtures/protocol/wsc-messages/vectors.json").read_text()
)["cases"][0]


def raw(name):
    return bytes.fromhex(CASE[name])


def pair():
    parameters = dh.DHParameterNumbers(MODP_1536, 2, (MODP_1536 - 1) // 2)
    public = dh.DHPublicNumbers(int.from_bytes(raw("enrollee_public"), "big"), parameters)
    private = int.from_bytes(raw("enrollee_private"), "big")
    return KeyPair(dh.DHPrivateNumbers(private, public).private_key())


def transcript():
    return M1Transcript(raw("m1"), pair())


def keys():
    return SessionKeys(*(raw(name) for name in ("auth_key", "key_wrap_key", "emsk")))


def device():
    # Facts from the synthetic independent harness, never a qualified pod profile.
    return M1Device(
        uuid=bytes(range(0x40, 0x50)),
        al_mac=bytes.fromhex("020000000001"),
        authentication_types=0x23,
        encryption_types=0x0D,
        connection_types=1,
        configuration_methods=0x0280,
        wps_state=2,
        manufacturer=b"EMOSA synthetic laboratory",
        model_name=b"Payload reference",
        model_number=b"1",
        serial_number=b"public-vector-1",
        primary_device_type=bytes.fromhex("00060050f2040001"),
        device_name=b"Synthetic represented AP",
        rf_band=1,
        association_state=0,
        device_password_id=4,
        configuration_error=0,
        os_version=1,
    )


def rewrite(data, kind, value):
    """Attacker changes one attribute; None removes it. Only used in negative tests."""
    return b"".join(
        encode_attribute(a.kind, value if a.kind == kind else a.value)
        for a in decode_attributes(data)
        if a.kind != kind or value is not None
    )


def signed(body):
    return authenticate_message(keys(), raw("m1"), body)


def altered_settings(plaintext):
    return signed(rewrite(raw("m2")[:-12], 0x1018, encrypt_settings(keys(), plaintext)))


def test_m1_matches_unmodified_hostap_builder(monkeypatch):
    monkeypatch.setattr(KeyPair, "generate", lambda: pair())
    monkeypatch.setattr(wsc_messages.secrets, "token_bytes", lambda size: bytes(range(size)))
    request = M1Transcript.create(device())
    assert request.message == raw("m1")
    assert repr(request) == "M1Transcript()"


def test_fresh_m1_material_and_no_synthetic_defaults():
    first, second = M1Transcript.create(device()), M1Transcript.create(device())
    a, b = (dict((x.kind, x.value) for x in decode_attributes(t.message)) for t in (first, second))
    assert a[0x101A] != b[0x101A] and a[0x1032] != b[0x1032]
    assert a[0x1020] == device().al_mac
    assert a[0x102D] == bytes.fromhex("80000001")
    with pytest.raises(TypeError):
        M1Device()


def test_native_m2_authentication_and_secret_output_handling():
    result = transcript().authenticate_m2(raw("m2"))
    assert result.bss_index == 1
    assert result.registrar_nonce == bytes(range(0x10, 0x20))
    settings = result.settings
    assert settings.ssid == b"EMOSA-payload-vector"
    assert settings.network_key == b"public-vector-passphrase"
    assert settings.authentication_type == 0x20 and settings.encryption_type == 8
    assert settings.mac_address == bytes.fromhex("020000001001")
    assert settings.attributes == decode_attributes(raw("ap_settings"))
    assert "public-vector" not in repr(settings) + repr(result)
    assert "EMOSA-payload" not in repr(settings) + repr(result)


@pytest.mark.parametrize(
    "kind",
    [
        0x104A,
        0x1022,
        0x101A,
        0x1039,
        0x1048,
        0x1032,
        0x1004,
        0x1010,
        0x100D,
        0x1008,
        0x1021,
        0x1023,
        0x1024,
        0x1042,
        0x1054,
        0x1011,
        0x103C,
        0x1002,
        0x1009,
        0x1012,
        0x102D,
    ],
    ids=lambda kind: f"{kind:04x}",
)
def test_required_m2_fields_cannot_be_omitted_even_with_valid_authenticator(kind):
    message = signed(rewrite(raw("m2")[:-12], kind, None))
    with pytest.raises(EmosaError):
        transcript().authenticate_m2(message)


@pytest.mark.parametrize("kind", [0x1045, 0x1003, 0x100F, 0x1027, 0x1020], ids=lambda k: f"{k:04x}")
def test_required_ap_settings_cannot_be_omitted_even_with_valid_crypto(kind):
    with pytest.raises(EmosaError):
        transcript().authenticate_m2(altered_settings(rewrite(raw("ap_settings"), kind, None)))


def test_authentication_precedes_decryption(monkeypatch):
    def unexpected_decryption(*args):
        raise AssertionError("Unauthenticated data reached decryption")

    monkeypatch.setattr(wsc_messages, "decrypt_settings", unexpected_decryption)
    corrupted = raw("m2")[:-1] + bytes([raw("m2")[-1] ^ 1])
    with pytest.raises(EmosaError):
        transcript().authenticate_m2(corrupted)


def test_nonce_and_original_m1_identity_binding():
    wrong_nonce = signed(rewrite(raw("m2")[:-12], 0x101A, bytes(16)))
    for request, message in (
        (transcript(), wrong_nonce),
        (
            M1Transcript(rewrite(raw("m1"), 0x1020, bytes.fromhex("020000000002")), pair()),
            raw("m2"),
        ),
        (M1Transcript(rewrite(raw("m1"), 0x1011, b"Other identity"), pair()), raw("m2")),
        (M1Transcript.create(device()), raw("m2")),
    ):
        with pytest.raises(EmosaError):
            request.authenticate_m2(message)


def test_optional_and_unknown_data_is_preserved_in_authenticated_bytes():
    body = rewrite(raw("m2")[:-12], 0x1BBC, None)
    optional = encode_attribute(0x1074, b"future-data") * 2
    body += optional + encode_attribute(0x1049, bytes.fromhex("001122aabb"))
    result = transcript().authenticate_m2(signed(body))
    assert [a.value for a in result.attributes if a.kind == 0x1074] == [b"future-data"] * 2
    plaintext = raw("ap_settings") + optional
    assert (
        transcript().authenticate_m2(altered_settings(plaintext)).settings.attributes[-1].value
        == b"future-data"
    )


@pytest.mark.parametrize(
    "version", [None, b"\x00\x37\x2a\x00\x01\x21", b"\x00\x37\x2a\x00\x01\x30"]
)
def test_version_negotiation_does_not_reject_only_a_version_mismatch(version):
    body = rewrite(raw("m2")[:-12], 0x1049, version)
    result = transcript().authenticate_m2(signed(body))
    assert result.settings.ssid == b"EMOSA-payload-vector"


def test_legacy_padded_device_strings_preserve_original_authenticator_input():
    body = rewrite(raw("m2")[:-12], 0x1023, b"Reference\0\0")
    result = transcript().authenticate_m2(signed(body))
    assert next(a.value for a in result.attributes if a.kind == 0x1023) == b"Reference\0\0"


def test_conditional_password_fields_are_preserved_but_not_applied():
    plain = raw("ap_settings") + encode_attribute(0x102A, b"public-new-password")
    with pytest.raises(EmosaError):
        transcript().authenticate_m2(altered_settings(plain))
    plain += encode_attribute(0x1012, b"\x00\x00")
    result = transcript().authenticate_m2(altered_settings(plain))
    assert result.settings.attributes == decode_attributes(plain)


def test_unknown_vendor_subelements_have_a_bounded_admission_budget():
    value = b"\x00\x37\x2a" + b"\x09\x00" * 500
    attribute = decode_attributes(encode_attribute(0x1049, value))[0]
    assert len(wsc_messages.vendor_subelements((attribute,) * 8)) == 4000
    with pytest.raises(EmosaError):
        wsc_messages.vendor_subelements((attribute,) * 9)


def test_overlong_settings_and_invalid_wfa_substructure_are_rejected():
    for plain in (
        rewrite(raw("ap_settings"), 0x1045, bytes(33)),
        rewrite(raw("ap_settings"), 0x1027, bytes(65)),
        raw("ap_settings") + encode_attribute(0x1028, bytes(2)),
        rewrite(raw("ap_settings"), 0x1049, b"\x00\x37\x2a\x06\x02\x20"),
    ):
        with pytest.raises(EmosaError):
            transcript().authenticate_m2(altered_settings(plain))


def test_bss_index_placement_and_duplicate_singletons_are_rejected():
    body = raw("m2")[:-12]
    for changed in (
        encode_attribute(0x1BBC, b"\x01") + rewrite(body, 0x1BBC, None),
        encode_attribute(0x101A, bytes(range(16))) + body,
        rewrite(body, 0x1BBC, b"\x01\x02"),
        rewrite(body, 0x1049, b"\x00\x37\x2a\x00\x01\x20\x00\x01\x20"),
        rewrite(body, 0x1049, b"\x00\x37\x2a\x00\x02\x20"),
        rewrite(body, 0x1049, b"\x00\x37\x2a\x00\x00"),
        rewrite(body, 0x1032, bytes(192)),
    ):
        with pytest.raises(EmosaError):
            transcript().authenticate_m2(signed(changed))
    with pytest.raises(EmosaError):
        transcript().authenticate_m2(
            altered_settings(raw("ap_settings") + encode_attribute(0x1027, b"another-key"))
        )


def test_non_configuration_m2_is_not_misreported_as_provisioned():
    with pytest.raises(EmosaError) as error:
        transcript().authenticate_m2(signed(rewrite(raw("m2")[:-12], 0x1018, None)))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


def second_m2(*, index=2):
    # A distinct authenticated registrar response for batch semantics; the
    # positive baseline above is checked independently by the C reference.
    nonce = bytes(range(0x20, 0x30))
    session_keys = pair().derive(raw("registrar_public"), bytes(range(16)), device().al_mac, nonce)
    body = rewrite(raw("m2")[:-12], 0x1039, nonce)
    body = rewrite(body, 0x1018, encrypt_settings(session_keys, raw("ap_settings")))
    body = rewrite(body, 0x1BBC, bytes([index]))
    return authenticate_message(session_keys, raw("m1"), body)


def test_batch_checks_all_responses_capacity_and_unique_nonce_and_index():
    request = transcript()
    results = request.authenticate_m2_batch((raw("m2"), second_m2()), max_bss=2)
    assert [r.bss_index for r in results] == [1, 2]
    for messages, capacity in (
        ((raw("m2"), raw("m2")), 2),
        ((raw("m2"), second_m2(index=1)), 2),
        ((raw("m2"), second_m2()), 1),
        ((raw("m2"), b"bad second payload"), 2),
        ((), 1),
    ):
        with pytest.raises(EmosaError):
            request.authenticate_m2_batch(messages, max_bss=capacity)


@pytest.mark.parametrize("capacity", [0, 17, True, "1"])
def test_invalid_capacity_is_not_used_as_a_radio_capability(capacity):
    with pytest.raises(EmosaError):
        transcript().authenticate_m2_batch((raw("m2"),), max_bss=capacity)


@pytest.mark.parametrize(
    "changes",
    [
        {"uuid": bytes(15)},
        {"al_mac": bytes(5)},
        {"manufacturer": b"a" * 65},
        {"device_name": b"padded\0"},
        {"rf_band": 3},
        {"wps_state": 0},
        {"authentication_types": True},
        {"os_version": 1 << 32},
        {"serial_number": "not bytes"},
    ],
    ids=["uuid", "mac", "length", "padding", "band", "state", "bool", "integer", "type"],
)
def test_m1_input_facts_must_have_supported_representation(changes):
    with pytest.raises(EmosaError):
        M1Transcript.create(replace(device(), **changes))
