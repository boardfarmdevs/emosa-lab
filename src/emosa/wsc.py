"""WPS 2.0.10 cryptographic component; NOT a complete M1/M2 procedure.

See docs/wsc-component.md. There is deliberately no network or OVSDB dependency.
Successful cryptographic verification alone never authorizes a configuration.
"""

import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass, field
from functools import lru_cache

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import dh
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from emosa.errors import EmosaError, Reason

# WPS 2.0.10 section 7.3 / RFC 3526 section 2. Never negotiate arbitrary groups.
MODP_1536 = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA237327FFFFFFFFFFFFFFFF",
    16,
)
AUTHENTICATOR = 0x1005
KEY_WRAP_AUTHENTICATOR = 0x101E
MAX_COMPONENT_BYTES = 1024 * 1024
MAX_COMPONENT_ATTRIBUTES = 4096


def _invalid():
    # Do not expose secret bytes, library exceptions, or padding/KWA distinctions.
    return EmosaError(Reason.INVALID_INPUT, "invalid WSC cryptographic input")


def _bytes(value, length=None):
    if not isinstance(value, bytes) or (length is not None and len(value) != length):
        raise _invalid()
    return value


def _bounded(value):
    _bytes(value)
    if len(value) > MAX_COMPONENT_BYTES:
        raise _invalid()
    return value


@dataclass(frozen=True)
class Attribute:
    kind: int
    value: bytes = field(repr=False)


def encode_attribute(kind: int, value: bytes) -> bytes:
    _bytes(value)
    if type(kind) is not int or not 0 <= kind <= 0xFFFF or len(value) > 0xFFFF:
        raise _invalid()
    return struct.pack("!HH", kind, len(value)) + value


def decode_attributes(data: bytes) -> tuple[Attribute, ...]:
    """Check TLV structure only. Preserve unknown fields; do not interpret a message."""
    _bounded(data)
    result = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4 or len(result) == MAX_COMPONENT_ATTRIBUTES:
            raise _invalid()
        kind, size = struct.unpack_from("!HH", data, offset)
        offset += 4
        if size > len(data) - offset:
            raise _invalid()
        result.append(Attribute(kind, data[offset : offset + size]))
        offset += size
    return tuple(result)


@dataclass(frozen=True)
class SessionKeys:
    auth_key: bytes = field(repr=False)
    key_wrap_key: bytes = field(repr=False)
    emsk: bytes = field(repr=False)

    def __post_init__(self):
        _bytes(self.auth_key, 32)
        _bytes(self.key_wrap_key, 16)
        _bytes(self.emsk, 32)


@lru_cache(maxsize=1)
def _parameters():
    # Preserve the fixed subgroup order; exchange alone does not enforce membership.
    return dh.DHParameterNumbers(MODP_1536, 2, (MODP_1536 - 1) // 2).parameters()


@dataclass(frozen=True)
class KeyPair:
    _private: dh.DHPrivateKey = field(repr=False)

    def __post_init__(self):
        if not isinstance(self._private, dh.DHPrivateKey):
            raise _invalid()
        if self._private.parameters().parameter_numbers() != _parameters().parameter_numbers():
            raise _invalid()

    @classmethod
    def generate(cls):
        """Generate a fresh ephemeral key for an exchange; never log or persist it."""
        return cls(_parameters().generate_private_key())

    @property
    def public(self) -> bytes:
        return self._private.public_key().public_numbers().y.to_bytes(192, "big")

    def derive(
        self, peer_public: bytes, enrollee_nonce: bytes, enrollee_mac: bytes, registrar_nonce: bytes
    ) -> SessionKeys:
        _bytes(peer_public, 192)
        _bytes(enrollee_nonce, 16)
        _bytes(enrollee_mac, 6)
        _bytes(registrar_nonce, 16)
        peer_number = int.from_bytes(peer_public, "big")
        if not 2 <= peer_number <= MODP_1536 - 2:
            raise _invalid()
        # RFC 2785 section 3.1 public-value admission. Only public integers enter
        # Python's built-in arithmetic; private-key operations remain in OpenSSL.
        if pow(peer_number, (MODP_1536 - 1) // 2, MODP_1536) != 1:
            raise _invalid()
        try:
            peer = dh.DHPublicNumbers(peer_number, _parameters().parameter_numbers()).public_key()
            shared = self._private.exchange(peer).rjust(192, b"\0")
        except ValueError:
            raise _invalid() from None
        dh_key = hashlib.sha256(shared).digest()
        kdk = hmac.digest(dh_key, enrollee_nonce + enrollee_mac + registrar_nonce, "sha256")
        label = b"Wi-Fi Easy and Secure Key Derivation"
        material = b"".join(
            hmac.digest(kdk, struct.pack("!I", i) + label + struct.pack("!I", 640), "sha256")
            for i in range(1, 4)
        )[:80]
        return SessionKeys(material[:32], material[32:48], material[48:80])


def _tag(keys: SessionKeys, data: bytes) -> bytes:
    return hmac.digest(keys.auth_key, data, "sha256")[:8]


def _without_trailer(data: bytes, kind: int) -> tuple[bytes, bytes]:
    attributes = decode_attributes(data)
    if (
        not attributes
        or attributes[-1].kind != kind
        or len(attributes[-1].value) != 8
        or any(a.kind == kind for a in attributes[:-1])
    ):
        raise _invalid()
    return data[:-12], attributes[-1].value


def authenticate_message(keys: SessionKeys, previous: bytes, current: bytes) -> bytes:
    """Append an authenticator; inputs still require procedure-specific validation."""
    _bounded(previous)
    attributes = decode_attributes(current)
    if len(attributes) >= MAX_COMPONENT_ATTRIBUTES or any(
        a.kind == AUTHENTICATOR for a in attributes
    ):
        raise _invalid()
    return _bounded(current + encode_attribute(AUTHENTICATOR, _tag(keys, previous + current)))


def verify_message(keys: SessionKeys, previous: bytes, current: bytes) -> bytes:
    """Verify the original message bytes and return the bytes before its trailer."""
    _bounded(previous)
    unsigned, tag = _without_trailer(current, AUTHENTICATOR)
    if not hmac.compare_digest(_tag(keys, previous + unsigned), tag):
        raise _invalid()
    return unsigned


def encrypt_settings(keys: SessionKeys, plaintext: bytes) -> bytes:
    """Return the value of an Encrypted Settings TLV with a fresh random IV."""
    attributes = decode_attributes(plaintext)
    if len(attributes) >= MAX_COMPONENT_ATTRIBUTES or any(
        a.kind == KEY_WRAP_AUTHENTICATOR for a in attributes
    ):
        raise _invalid()
    # IV + padded plaintext/KWA must fit the attribute's two-byte length field.
    if 16 + ((len(plaintext) + 12) // 16 + 1) * 16 > 0xFFFF:
        raise _invalid()
    authenticated = plaintext + encode_attribute(KEY_WRAP_AUTHENTICATOR, _tag(keys, plaintext))
    padder = padding.PKCS7(128).padder()
    padded = padder.update(authenticated) + padder.finalize()
    iv = secrets.token_bytes(16)
    encryptor = Cipher(algorithms.AES(keys.key_wrap_key), modes.CBC(iv)).encryptor()
    return _bounded(iv + encryptor.update(padded) + encryptor.finalize())


def decrypt_settings(keys: SessionKeys, encrypted: bytes) -> bytes:
    """Return plaintext TLVs only after padding, structure and KWA verification."""
    _bounded(encrypted)
    if not 32 <= len(encrypted) <= 0xFFFF or len(encrypted) % 16:
        raise _invalid()
    try:
        decryptor = Cipher(algorithms.AES(keys.key_wrap_key), modes.CBC(encrypted[:16])).decryptor()
        padded = decryptor.update(encrypted[16:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        authenticated = unpadder.update(padded) + unpadder.finalize()
        plaintext, tag = _without_trailer(authenticated, KEY_WRAP_AUTHENTICATOR)
        if not hmac.compare_digest(_tag(keys, plaintext), tag):
            raise _invalid()
    except (ValueError, EmosaError):
        raise _invalid() from None
    return plaintext
