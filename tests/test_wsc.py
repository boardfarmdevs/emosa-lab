"""Component checks against independent hostap output; no wire/OVSDB side effects."""

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import dh

from emosa.errors import EmosaError, Reason
from emosa.wsc import (
    AUTHENTICATOR,
    KEY_WRAP_AUTHENTICATOR,
    MAX_COMPONENT_ATTRIBUTES,
    MAX_COMPONENT_BYTES,
    MODP_1536,
    KeyPair,
    SessionKeys,
    authenticate_message,
    decode_attributes,
    decrypt_settings,
    encode_attribute,
    encrypt_settings,
    verify_message,
)

pytestmark = pytest.mark.unit
FIXTURE = json.loads((Path(__file__).parent / "fixtures/protocol/wsc/vectors.json").read_text())[
    "cases"
]


def data(case, name):
    return bytes.fromhex(case[name])


def pair(case, role="enrollee"):
    params = dh.DHParameterNumbers(MODP_1536, 2, (MODP_1536 - 1) // 2)
    public = dh.DHPublicNumbers(int.from_bytes(data(case, f"{role}_public"), "big"), params)
    private = int.from_bytes(data(case, f"{role}_private"), "big")
    return KeyPair(dh.DHPrivateNumbers(private, public).private_key())


def keys(case):
    return SessionKeys(*(data(case, name) for name in ("auth_key", "key_wrap_key", "emsk")))


@pytest.mark.parametrize("case", FIXTURE, ids=lambda c: c["name"])
def test_hostap_key_derivation_and_identity_binding(case):
    expected = keys(case)
    nonces_and_mac = [
        data(case, name) for name in ("enrollee_nonce", "enrollee_mac", "registrar_nonce")
    ]
    enrollee = pair(case)
    assert enrollee.public == data(case, "enrollee_public")
    assert enrollee.derive(data(case, "registrar_public"), *nonces_and_mac) == expected
    assert (
        pair(case, "registrar").derive(data(case, "enrollee_public"), *nonces_and_mac) == expected
    )
    for index in range(3):
        changed = nonces_and_mac.copy()
        changed[index] = bytes([changed[index][0] ^ 1]) + changed[index][1:]
        assert enrollee.derive(data(case, "registrar_public"), *changed) != expected
    assert repr(enrollee) == "KeyPair()" and repr(expected) == "SessionKeys()"


@pytest.mark.parametrize("case", FIXTURE, ids=lambda c: c["name"])
def test_hostap_authenticator_includes_unknown_attribute_and_exact_previous_bytes(case):
    previous = data(case, "previous_fragment")
    unsigned = data(case, "unsigned_fragment")
    authenticated = data(case, "authenticated_fragment")
    assert authenticate_message(keys(case), previous, unsigned) == authenticated
    assert verify_message(keys(case), previous, authenticated) == unsigned
    assert decode_attributes(unsigned)[-1].kind == 0xFFFE
    # Mutating every byte covers attribute headers, the unknown field and the tag.
    for offset in range(len(authenticated)):
        changed = bytearray(authenticated)
        changed[offset] ^= 1
        with pytest.raises(EmosaError):
            verify_message(keys(case), previous, bytes(changed))
    with pytest.raises(EmosaError):
        verify_message(keys(case), previous[:-1] + b"\xff", authenticated)


@pytest.mark.parametrize("case", FIXTURE, ids=lambda c: c["name"])
def test_hostap_encrypted_settings_and_independent_failures(case, monkeypatch):
    encrypted = data(case, "encrypted_settings")
    plaintext = data(case, "plaintext_fragment")
    assert decrypt_settings(keys(case), encrypted) == plaintext
    # Deterministic IV injection exists only here; production always draws OS randomness.
    monkeypatch.setattr("emosa.wsc.secrets.token_bytes", lambda size: bytes(range(0xA0, 0xB0)))
    assert encrypt_settings(keys(case), plaintext) == encrypted
    errors = []
    for name in ("bad_kwa", "bad_padding"):
        with pytest.raises(EmosaError) as error:
            decrypt_settings(keys(case), data(case, name))
        errors.append(error.value.public())
    assert errors[0] == errors[1]
    for offset in range(len(encrypted)):
        changed = bytearray(encrypted)
        changed[offset] ^= 1
        with pytest.raises(EmosaError):
            decrypt_settings(keys(case), bytes(changed))


def test_ephemeral_randomness_and_no_secret_representations():
    one, two = KeyPair.generate(), KeyPair.generate()
    assert len(one.public) == 192 and one.public != two.public
    session = keys(FIXTURE[0])
    plaintext = data(FIXTURE[0], "plaintext_fragment")
    first, second = encrypt_settings(session, plaintext), encrypt_settings(session, plaintext)
    assert first[:16] != second[:16]
    assert decrypt_settings(session, first) == decrypt_settings(session, second) == plaintext
    assert "EMOSA" not in repr(decode_attributes(plaintext))


@pytest.mark.parametrize("public", [0, 1, MODP_1536 - 1, MODP_1536, MODP_1536 - 2])
def test_invalid_public_keys_include_non_subgroup_value(public):
    with pytest.raises(EmosaError):
        pair(FIXTURE[0]).derive(public.to_bytes(192, "big"), bytes(16), bytes(6), bytes(16))


@pytest.mark.parametrize("index", range(4))
def test_reject_incorrect_public_nonce_and_mac_lengths(index):
    inputs = [data(FIXTURE[0], "registrar_public"), bytes(16), bytes(6), bytes(16)]
    inputs[index] = inputs[index][:-1]
    with pytest.raises(EmosaError):
        pair(FIXTURE[0]).derive(*inputs)


def test_reject_ambiguous_authentication_trailers():
    case = FIXTURE[0]
    unsigned = data(case, "unsigned_fragment")
    signed = data(case, "authenticated_fragment")
    previous = data(case, "previous_fragment")
    malformed = (
        unsigned,
        signed + encode_attribute(1, b""),
        signed + signed[-12:],
        unsigned + encode_attribute(AUTHENTICATOR, b"short"),
    )
    for message in malformed:
        with pytest.raises(EmosaError):
            verify_message(keys(case), previous, message)
    with pytest.raises(EmosaError):
        authenticate_message(keys(case), previous, signed)
    with pytest.raises(EmosaError):
        encrypt_settings(keys(case), encode_attribute(KEY_WRAP_AUTHENTICATOR, bytes(8)))


@pytest.mark.parametrize(
    "encoded",
    [
        b"\x10",
        b"\x10\x4a\x00",
        b"\x10\x4a\x00\x02\x10",
        bytes(MAX_COMPONENT_BYTES + 1),
        bytes(4) * (MAX_COMPONENT_ATTRIBUTES + 1),
    ],
)
def test_truncation_and_component_admission_limits(encoded):
    with pytest.raises(EmosaError) as error:
        decode_attributes(encoded)
    assert error.value.code == Reason.INVALID_INPUT


def test_bad_settings_lengths_and_keys():
    for length in (0, 16, 31, 33, MAX_COMPONENT_BYTES + 1):
        with pytest.raises(EmosaError):
            decrypt_settings(keys(FIXTURE[0]), bytes(length))
    with pytest.raises(EmosaError):
        SessionKeys(bytes(31), bytes(16), bytes(32))
    with pytest.raises(EmosaError):
        encode_attribute(0x10000, b"")
    with pytest.raises(EmosaError):
        encode_attribute(1, bytes(0x10000))
    assert decode_attributes(encode_attribute(0xFFFF, b""))[0].value == b""


def test_reserve_attribute_budget_for_authentication_trailer():
    session = keys(FIXTURE[0])
    allowed = encode_attribute(0xFFFF, b"") * (MAX_COMPONENT_ATTRIBUTES - 1)
    assert verify_message(session, b"", authenticate_message(session, b"", allowed)) == allowed
    assert decrypt_settings(session, encrypt_settings(session, allowed)) == allowed
    exhausted = allowed + encode_attribute(0xFFFF, b"")
    with pytest.raises(EmosaError):
        authenticate_message(session, b"", exhausted)
    with pytest.raises(EmosaError):
        encrypt_settings(session, exhausted)


def test_encrypted_settings_fit_wire_attribute_length():
    session = keys(FIXTURE[0])
    # Largest plaintext whose IV/padding/KWA fit a two-byte attribute length.
    allowed = encode_attribute(0xFFFF, bytes(65487))
    encrypted = encrypt_settings(session, allowed)
    assert len(encrypted) == 65520
    assert decrypt_settings(session, encrypted) == allowed
    with pytest.raises(EmosaError):
        encrypt_settings(session, encode_attribute(0xFFFF, bytes(65488)))
    with pytest.raises(EmosaError):
        decrypt_settings(session, bytes(65536))
