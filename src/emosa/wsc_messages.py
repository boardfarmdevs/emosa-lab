"""WPS 2.0.10 M1/M2 payloads, without IEEE transport or configuration authority.

See doc/protocol/wsc-messages.md for the selected rules and remaining procedure checks.
Authenticated settings are secret data, not an instruction to write OVSDB.
"""

import hmac
import secrets
from dataclasses import dataclass, field

from emosa.errors import EmosaError, Reason
from emosa.wsc import (
    AUTHENTICATOR,
    Attribute,
    KeyPair,
    decode_attributes,
    decrypt_settings,
    encode_attribute,
    verify_message,
)

# WPS 2.0.10 Tables 8, 9, 20, 28 and 29; EasyMesh 6.1 Tables 10 and 19.
VERSION = 0x104A
MESSAGE_TYPE = 0x1022
ENROLLEE_NONCE = 0x101A
REGISTRAR_NONCE = 0x1039
PUBLIC_KEY = 0x1032
VENDOR_EXTENSION = 0x1049
ENCRYPTED_SETTINGS = 0x1018
BSS_INDEX = 0x1BBC
WFA_ID = b"\x00\x37\x2a"
MAX_M2_PAYLOADS = 16  # Local admission budget, not a claimed radio capability.
MAX_VENDOR_SUBELEMENTS = 4096

COMMON_LENGTHS = {
    VERSION: 1,
    MESSAGE_TYPE: 1,
    ENROLLEE_NONCE: 16,
    PUBLIC_KEY: 192,
    0x1004: 2,  # Authentication Type Flags
    0x1010: 2,  # Encryption Type Flags
    0x100D: 1,  # Connection Type Flags
    0x1008: 2,  # Configuration Methods
    0x1054: 8,  # Primary Device Type
    0x103C: 1,  # RF Bands
    0x1002: 2,  # Association State
    0x1012: 2,  # Device Password ID
    0x1009: 2,  # Configuration Error
    0x102D: 4,  # OS Version
}
DEVICE_STRINGS = {0x1021: 64, 0x1023: 32, 0x1024: 32, 0x1042: 32, 0x1011: 32}


def _invalid():
    return EmosaError(Reason.INVALID_INPUT, "invalid WSC message payload")


def _uint(value, size):
    if type(value) is not int or not 0 <= value < 1 << (8 * size):
        raise _invalid()
    return value.to_bytes(size, "big")


def _singletons(attributes, required, optional=None):
    """Validate consumed fields; retain, rather than discard, all other attributes."""
    fields = {}
    lengths = required | (optional or {})
    for attribute in attributes:
        if attribute.kind not in lengths:
            continue
        if attribute.kind in fields:
            raise _invalid()
        length = lengths[attribute.kind]
        if isinstance(length, tuple):
            if not length[0] <= len(attribute.value) <= length[1]:
                raise _invalid()
        elif len(attribute.value) != length:
            raise _invalid()
        fields[attribute.kind] = attribute.value
    if required.keys() - fields.keys():
        raise _invalid()
    return fields


def vendor_subelements(attributes):
    """Parse WFA extension structure; unknown vendor/subelement data stays intact."""
    result = []
    for attribute in attributes:
        if attribute.kind != VENDOR_EXTENSION:
            continue
        value = attribute.value
        if not 4 <= len(value) <= 1024:
            raise _invalid()
        if value[:3] != WFA_ID:
            continue
        offset = 3
        while offset < len(value):
            if len(value) - offset < 2:
                raise _invalid()
            kind, length = value[offset : offset + 2]
            offset += 2
            if length > len(value) - offset:
                raise _invalid()
            if len(result) == MAX_VENDOR_SUBELEMENTS:
                raise _invalid()
            result.append(Attribute(kind, value[offset : offset + length]))
            offset += length
    return tuple(result)


def _message(data, message_type):
    attributes = decode_attributes(data)
    required = COMMON_LENGTHS | {k: (0, v) for k, v in DEVICE_STRINGS.items()}
    required |= (
        {0x1047: 16, 0x1020: 6, 0x1044: 1}
        if message_type == 4
        else {
            REGISTRAR_NONCE: 16,
            0x1048: 16,
            AUTHENTICATOR: 8,
        }
    )
    fields = _singletons(attributes, required, {BSS_INDEX: 1, ENCRYPTED_SETTINGS: (32, 65535)})
    if fields[MESSAGE_TYPE] != bytes([message_type]):
        raise _invalid()
    versions = [a.value for a in vendor_subelements(attributes) if a.kind == 0]
    # WPS 7.9: do not reject solely on a version mismatch. Absent Version2
    # denotes 1.0h; unknown attributes remain covered by the original-byte MAC.
    if len(versions) > 1 or (versions and len(versions[0]) != 1):
        raise _invalid()
    if BSS_INDEX in fields and (
        message_type != 5 or len(attributes) < 2 or attributes[-2].kind != BSS_INDEX
    ):
        raise _invalid()
    return attributes, fields


@dataclass(frozen=True, repr=False)
class M1Device:
    """Explicit represented-device facts. No synthetic identity/capability defaults."""

    uuid: bytes
    al_mac: bytes
    authentication_types: int
    encryption_types: int
    connection_types: int
    configuration_methods: int
    wps_state: int
    manufacturer: bytes
    model_name: bytes
    model_number: bytes
    serial_number: bytes
    primary_device_type: bytes
    device_name: bytes
    rf_band: int
    association_state: int
    device_password_id: int
    configuration_error: int
    os_version: int

    def attributes(self):
        os_version = _uint(self.os_version, 4)
        os_version = bytes([os_version[0] | 0x80]) + os_version[1:]
        values = (
            (0x1047, self.uuid),
            (0x1020, self.al_mac),
            (0x1004, _uint(self.authentication_types, 2)),
            (0x1010, _uint(self.encryption_types, 2)),
            (0x100D, _uint(self.connection_types, 1)),
            (0x1008, _uint(self.configuration_methods, 2)),
            (0x1044, _uint(self.wps_state, 1)),
            (0x1021, self.manufacturer),
            (0x1023, self.model_name),
            (0x1024, self.model_number),
            (0x1042, self.serial_number),
            (0x1054, self.primary_device_type),
            (0x1011, self.device_name),
            (0x103C, _uint(self.rf_band, 1)),
            (0x1002, _uint(self.association_state, 2)),
            (0x1012, _uint(self.device_password_id, 2)),
            (0x1009, _uint(self.configuration_error, 2)),
            (0x102D, os_version),
        )
        if self.rf_band not in {1, 2, 4, 8} or self.wps_state not in {1, 2}:
            raise _invalid()
        for kind, value in values:
            if not isinstance(value, bytes) or (kind in DEVICE_STRINGS and b"\0" in value):
                raise _invalid()
        return tuple(Attribute(kind, value) for kind, value in values)


@dataclass(frozen=True)
class APSettings:
    """Authenticated AP settings with all optional/unknown fields preserved."""

    ssid: bytes = field(repr=False)
    authentication_type: int
    encryption_type: int
    network_key: bytes = field(repr=False)
    mac_address: bytes = field(repr=False)
    attributes: tuple[Attribute, ...] = field(repr=False)

    @classmethod
    def parse(cls, attributes):
        settings = _singletons(
            attributes,
            {0x1045: (0, 32), 0x1003: 2, 0x100F: 2, 0x1027: (0, 64), 0x1020: 6},
            {0x1028: 1, 0x102A: (0, 64), 0x1012: 2},
        )
        if 0x102A in settings and 0x1012 not in settings:
            raise _invalid()
        return cls(
            settings[0x1045],
            int.from_bytes(settings[0x1003], "big"),
            int.from_bytes(settings[0x100F], "big"),
            settings[0x1027],
            settings[0x1020],
            attributes,
        )


@dataclass(frozen=True)
class AuthenticatedM2:
    registrar_nonce: bytes = field(repr=False)
    registrar_uuid: bytes = field(repr=False)
    bss_index: int | None
    settings: APSettings = field(repr=False)
    attributes: tuple[Attribute, ...] = field(repr=False)


@dataclass(frozen=True)
class AuthenticatedM2Envelope:
    """Verified M2 and ConfigData; their procedure semantics are still unvalidated.

    Separating authentication from AP parsing permits EasyMesh teardown to ignore
    other settings, as required by section 7.1, without bypassing authentication.
    Like the other Python result records, this is not a configuration capability.
    """

    registrar_nonce: bytes = field(repr=False)
    registrar_uuid: bytes = field(repr=False)
    bss_index: int | None
    config_data: tuple[Attribute, ...] = field(repr=False)
    attributes: tuple[Attribute, ...] = field(repr=False)

    def ap_configuration(self):
        return AuthenticatedM2(
            self.registrar_nonce,
            self.registrar_uuid,
            self.bss_index,
            APSettings.parse(self.config_data),
            self.attributes,
        )


@dataclass(frozen=True)
class M1Transcript:
    """One immutable M1; the future wire procedure owns its lifetime and retries."""

    message: bytes = field(repr=False)
    _key_pair: KeyPair = field(repr=False)

    @classmethod
    def create(cls, device: M1Device):
        facts = device.attributes()
        pair = KeyPair.generate()
        attributes = (
            Attribute(VERSION, b"\x10"),
            Attribute(MESSAGE_TYPE, b"\x04"),
            *facts[:2],
            Attribute(ENROLLEE_NONCE, secrets.token_bytes(16)),
            Attribute(PUBLIC_KEY, pair.public),
            *facts[2:],
            Attribute(VENDOR_EXTENSION, WFA_ID + b"\x00\x01\x20"),
        )
        message = b"".join(encode_attribute(a.kind, a.value) for a in attributes)
        _message(message, 4)
        return cls(message, pair)

    def authenticate_m2_envelope(self, message: bytes) -> AuthenticatedM2Envelope:
        """Verify the transcript, then decrypt. This does not authenticate peer identity."""
        _, enrollee = _message(self.message, 4)
        attributes, registrar = _message(message, 5)
        if self._key_pair.public != enrollee[PUBLIC_KEY] or not hmac.compare_digest(
            enrollee[ENROLLEE_NONCE], registrar[ENROLLEE_NONCE]
        ):
            raise _invalid()
        keys = self._key_pair.derive(
            registrar[PUBLIC_KEY],
            enrollee[ENROLLEE_NONCE],
            enrollee[0x1020],
            registrar[REGISTRAR_NONCE],
        )
        verify_message(keys, self.message, message)
        if ENCRYPTED_SETTINGS not in registrar:
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "M2 without AP settings needs another WSC procedure"
            )
        plain = decrypt_settings(keys, registrar[ENCRYPTED_SETTINGS])
        settings_attributes = decode_attributes(plain)
        vendor_subelements(settings_attributes)
        return AuthenticatedM2Envelope(
            registrar[REGISTRAR_NONCE],
            registrar[0x1048],
            registrar[BSS_INDEX][0] if BSS_INDEX in registrar else None,
            settings_attributes,
            attributes,
        )

    def authenticate_m2(self, message: bytes) -> AuthenticatedM2:
        return self.authenticate_m2_envelope(message).ap_configuration()

    def authenticate_m2_envelopes(
        self, messages: tuple[bytes, ...], *, max_bss: int, shared_session=False
    ):
        """Return the entire checked payload set, or raise without returning any settings.

        This verifies nonce/index uniqueness within one received set only, not
        cross-exchange replay, controller/radio binding or complete CMDU semantics.
        Every M2 is authenticated on its own. By default each carries its own
        registrar nonce. ``shared_session`` accepts one registrar session split
        over the set (RDK unified-wifi-mesh): one nonce and one registrar public
        key for every M2, and no two M2s identical.
        """
        if type(max_bss) is not int or not 1 <= max_bss <= MAX_M2_PAYLOADS:
            raise _invalid()
        if not isinstance(messages, tuple) or not 1 <= len(messages) <= max_bss:
            raise _invalid()
        results = tuple(self.authenticate_m2_envelope(message) for message in messages)
        nonces = [r.registrar_nonce for r in results]
        indexes = [r.bss_index for r in results if r.bss_index is not None]
        if shared_session:
            publics = {a.value for r in results for a in r.attributes if a.kind == PUBLIC_KEY}
            if len(set(nonces)) != 1 or len(publics) != 1 or len(set(messages)) != len(messages):
                raise _invalid()
        elif len(set(nonces)) != len(nonces):
            raise _invalid()
        if len(set(indexes)) != len(indexes):
            raise _invalid()
        return results

    def authenticate_m2_batch(self, messages: tuple[bytes, ...], *, max_bss: int):
        return tuple(
            result.ap_configuration()
            for result in self.authenticate_m2_envelopes(messages, max_bss=max_bss)
        )
