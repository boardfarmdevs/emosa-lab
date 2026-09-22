"""Selected EasyMesh 6.1 TLV *values*, without TLV headers or IEEE 1905 frames.

Selected fields in sections 3.1.2 and 17.2 define this component. SSID octets use
IEEE 802.11-2024 section 9.4.2.2. See doc/protocol/easymesh-payloads.md.
These objects establish neither trusted identities nor a qualified profile.
"""

from dataclasses import dataclass, field
from typing import ClassVar

from emosa.errors import EmosaError, Reason
from emosa.operating_classes import GLOBAL_OPERATING_CLASSES

# A local parser/encoder resource budget, not a normative TLV or CMDU limit.
MAX_VALUE_BYTES = 16_384


def _invalid():
    return EmosaError(Reason.INVALID_INPUT, "invalid EasyMesh TLV value")


def _octet(value):
    if type(value) is not int or not 0 <= value <= 255:
        raise _invalid()
    return bytes((value,))


def _octets(value, *, length=None):
    if type(value) is not bytes or (length is not None and len(value) != length):
        raise _invalid()
    return value


def _count(items):
    if type(items) is not tuple:
        raise _invalid()
    return _octet(len(items))


def _bounded(value):
    if len(value) > MAX_VALUE_BYTES:
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "EasyMesh value exceeds local byte budget")
    return value


@dataclass(frozen=True)
class SupportedServices:
    kind: ClassVar[int] = 0x80
    services: tuple[int, ...]

    @property
    def known_services(self) -> tuple[int, ...]:
        """Ignore reserved identifiers for role interpretation; retain raw values."""
        return tuple(service for service in self.services if service in (0, 1))


@dataclass(frozen=True)
class SearchedServices:
    kind: ClassVar[int] = 0x81
    services: tuple[int, ...]

    @property
    def known_services(self) -> tuple[int, ...]:
        return tuple(service for service in self.services if service == 0)


@dataclass(frozen=True)
class RadioIdentifier:
    kind: ClassVar[int] = 0x82
    ruid: bytes


@dataclass(frozen=True)
class OperationalBss:
    # AP_MAC is an affiliated AP address for MLD operation, otherwise the BSSID.
    # This value alone cannot determine which interpretation applies.
    ap_mac: bytes
    ssid: bytes = field(repr=False)


@dataclass(frozen=True)
class OperationalRadio:
    ruid: bytes
    bsses: tuple[OperationalBss, ...]


@dataclass(frozen=True)
class APOperationalBss:
    kind: ClassVar[int] = 0x83
    radios: tuple[OperationalRadio, ...]


@dataclass(frozen=True)
class BasicOperatingClass:
    operating_class: int
    max_eirp_dbm: int
    non_operable_channels: tuple[int, ...]


@dataclass(frozen=True)
class APRadioBasicCapabilities:
    kind: ClassVar[int] = 0x85
    ruid: bytes
    max_bss: int
    operating_classes: tuple[BasicOperatingClass, ...]


AP_FEATURE_BITS = {
    "unassociated_metrics_on_channel": 7,
    "unassociated_metrics_off_channel": 6,
    "agent_initiated_rcpi_steering": 5,
    "m8_backhaul_sta_reconfiguration": 4,
    "rsn_overriding": 3,
}
PROFILE2_FEATURE_BITS = {"prioritization": 5, "dpp_onboarding": 4, "traffic_separation": 3}
ADVANCED_FEATURE_BITS = {
    "combined_fronthaul_backhaul": 7,
    "combined_profile1_profile2": 6,
    "mscs": 5,
    "scs": 4,
    "qos_map": 3,
    "dscp_policy": 2,
    "qm_scs_traffic_description": 1,
}


@dataclass(frozen=True)
class APCapability:
    kind: ClassVar[int] = 0xA1
    flags: int


@dataclass(frozen=True)
class Profile2APCapability:
    kind: ClassVar[int] = 0xB4
    max_prioritization_rules: int
    reserved_octet: int
    flags: int
    max_vids: int

    @property
    def byte_counter_units(self):
        return (self.flags >> 6) & 3


@dataclass(frozen=True)
class APRadioAdvancedCapabilities:
    kind: ClassVar[int] = 0xBE
    ruid: bytes
    flags: int


@dataclass(frozen=True)
class MultiAPProfile:
    kind: ClassVar[int] = 0xB3
    profile: int

    def effective_profile(self, receiver_profile: int) -> int:
        """Table 70's reserved-value rule, with an explicit qualified receiver input.

        This is not profile negotiation or proof that either peer implements it.
        """
        _octet(self.profile)
        if type(receiver_profile) is not int or receiver_profile not in (1, 2, 3):
            raise _invalid()
        return self.profile if self.profile in (1, 2, 3) else receiver_profile


type Payload = (
    SupportedServices
    | SearchedServices
    | RadioIdentifier
    | APOperationalBss
    | APRadioBasicCapabilities
    | APCapability
    | Profile2APCapability
    | APRadioAdvancedCapabilities
    | MultiAPProfile
)


class _Reader:
    def __init__(self, value):
        self.value = _bounded(_octets(value))
        self.offset = 0

    def take(self, length):
        end = self.offset + length
        if end > len(self.value):
            raise _invalid()
        value = self.value[self.offset : end]
        self.offset = end
        return value

    def octet(self):
        return self.take(1)[0]

    def ssid(self):
        length = self.octet()
        if length > 32:
            raise _invalid()
        return self.take(length)

    def finish(self):
        if self.offset != len(self.value):
            raise _invalid()


def decode_value(kind: int, value: bytes) -> Payload:
    """Decode exactly one value. Unknown types are outside this component's scope.

    Reserved service/profile identifiers remain available for diagnostics. No
    inference about unknown TLVs in complete messages or their IEEE rules is made.
    """
    _octet(kind)
    reader = _Reader(value)
    match kind:
        case 0x80 | 0x81:
            services = tuple(reader.take(reader.octet()))
            result = SupportedServices(services) if kind == 0x80 else SearchedServices(services)
        case 0x82:
            result = RadioIdentifier(reader.take(6))
        case 0x83:
            radios = []
            for _ in range(reader.octet()):
                ruid = reader.take(6)
                bsses = tuple(
                    OperationalBss(reader.take(6), reader.ssid()) for _ in range(reader.octet())
                )
                radios.append(OperationalRadio(ruid, bsses))
            result = APOperationalBss(tuple(radios))
        case 0xB3:
            result = MultiAPProfile(reader.octet())
        case 0xA1:
            result = APCapability(reader.octet())
        case 0xB4:
            result = Profile2APCapability(*(reader.octet() for _ in range(4)))
        case 0xBE:
            result = APRadioAdvancedCapabilities(reader.take(6), reader.octet())
        case 0x85:
            ruid, max_bss = reader.take(6), reader.octet()
            if max_bss == 0:
                raise _invalid()
            classes = []
            for _ in range(reader.octet()):
                identifier = reader.octet()
                power = int.from_bytes(reader.take(1), signed=True)
                channels = tuple(reader.take(reader.octet()))
                classes.append(BasicOperatingClass(identifier, power, channels))
            result = APRadioBasicCapabilities(ruid, max_bss, tuple(classes))
        case _:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "EasyMesh value type is not implemented")
    reader.finish()
    return result


def encode_value(payload: Payload) -> bytes:
    """Build value bytes only; never add a header or send a message.

    Unlike the tolerant receiver, an encoder must not send reserved service or
    profile codes (section 3.1.2). Thus not every decoded value can be re-encoded.
    All counts are derived, identities are opaque six-octet values, and SSIDs
    preserve their original octets, including non-UTF-8 and embedded NULs.
    """
    if type(payload) in (SupportedServices, SearchedServices):
        value = _count(payload.services)
        allowed = (0, 1) if type(payload) is SupportedServices else (0,)
        for service in payload.services:
            value += _octet(service)
            if service not in allowed:
                raise _invalid()
    elif type(payload) is RadioIdentifier:
        value = _octets(payload.ruid, length=6)
    elif type(payload) is MultiAPProfile:
        value = _octet(payload.profile)
        if payload.profile not in (1, 2, 3):
            raise _invalid()
    elif type(payload) in (APCapability, Profile2APCapability, APRadioAdvancedCapabilities):
        flags = _octet(payload.flags)
        reserved_mask = 1 if type(payload) is APRadioAdvancedCapabilities else 7
        if payload.flags & reserved_mask:
            raise _invalid()
        if type(payload) is APCapability:
            value = flags
        elif type(payload) is APRadioAdvancedCapabilities:
            value = _octets(payload.ruid, length=6) + flags
        else:
            value = (
                _octet(payload.max_prioritization_rules)
                + _octet(payload.reserved_octet)
                + flags
                + _octet(payload.max_vids)
            )
            if payload.reserved_octet != 0 or payload.byte_counter_units == 3:
                raise _invalid()
    elif type(payload) is APRadioBasicCapabilities:
        value = bytearray(_octets(payload.ruid, length=6) + _octet(payload.max_bss))
        if payload.max_bss == 0:
            raise _invalid()
        value.extend(_count(payload.operating_classes))
        for op in payload.operating_classes:
            if type(op) is not BasicOperatingClass:
                raise _invalid()
            _octet(op.operating_class)
            if op.operating_class not in GLOBAL_OPERATING_CLASSES:
                raise EmosaError(
                    Reason.UNSUPPORTED_OPERATION, "operating class is outside the audited subset"
                )
            if type(op.max_eirp_dbm) is not int or not -128 <= op.max_eirp_dbm <= 127:
                raise _invalid()
            value.extend(_octet(op.operating_class) + op.max_eirp_dbm.to_bytes(1, signed=True))
            value.extend(_count(op.non_operable_channels))
            for channel in op.non_operable_channels:
                value.extend(_octet(channel))
                if channel not in GLOBAL_OPERATING_CLASSES[op.operating_class][1]:
                    raise _invalid()
            _bounded(value)
        value = bytes(value)
    elif type(payload) is APOperationalBss:
        value = bytearray(_count(payload.radios))
        for radio in payload.radios:
            if type(radio) is not OperationalRadio:
                raise _invalid()
            value.extend(_octets(radio.ruid, length=6) + _count(radio.bsses))
            for bss in radio.bsses:
                if type(bss) is not OperationalBss:
                    raise _invalid()
                mac = _octets(bss.ap_mac, length=6)
                ssid = _octets(bss.ssid)
                if len(ssid) > 32:
                    raise _invalid()
                value.extend(mac + _octet(len(ssid)) + ssid)
                _bounded(value)
        value = bytes(value)
    else:
        raise _invalid()
    return _bounded(value)
