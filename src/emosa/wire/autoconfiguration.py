"""Bounded discovery and WSC exchange components, not a qualified agent endpoint.

The caller supplies a trusted-link/peer binding; neither MAC matching nor WSC
authentication establishes that trust. No backend, secret store or operation
engine is called here. See doc/protocol/autoconfiguration.md for the contract.
"""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from emosa.easymesh_payloads import (
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    MultiAPProfile,
    Profile2APCapability,
    SearchedServices,
    SupportedServices,
    decode_value,
    encode_value,
)
from emosa.errors import EmosaError, Reason
from emosa.wire.cmdu import MULTICAST, Message, MidSequence, Tlv, fragment_message, mac

if TYPE_CHECKING:
    from emosa.wsc_messages import M1Device
    from emosa.wsc_radio import ExistingBssCandidate

SEARCH, RESPONSE, WSC = 0x0007, 0x0008, 0x0009
# EasyMesh 6.1 17.1.3: these add configuration we cannot discard or apply partly.
CONFIGURATION_COMPANIONS = frozenset((0xB5, 0xB6, 0xE0, 0xE1, 0xEB, 0xEC))
SECURITY_ENVELOPES = frozenset((0xAB, 0xAC))


def _invalid(detail):
    raise EmosaError(Reason.INVALID_INPUT, detail)


def _unicast(value):
    mac(value)
    if value == bytes(6) or value[0] & 1:
        _invalid("expected a nonzero unicast identity")
    return value


def _one(message, kind):
    values = [tlv.value for tlv in message.tlvs if tlv.kind == kind]
    if len(values) != 1:
        _invalid("missing or repeated required EasyMesh TLV")
    return values[0]


def _base(message, kind, length):
    # IEEE 1905.1 6.2 aggregates repeated base TLV values. Fixed-size fields
    # cannot thereby acquire extra bytes; ignore unrelated TLVs per that clause.
    value = b"".join(tlv.value for tlv in message.tlvs if tlv.kind == kind)
    if len(value) != length:
        _invalid("missing or malformed required IEEE TLV")
    return value


def _tlv(payload):
    return Tlv(payload.kind, encode_value(payload))


def _header(message, kind, *, multicast=False):
    if (
        message.message_type != kind
        or message.relay != multicast
        or (multicast and message.destination != MULTICAST)
    ):
        _invalid("unexpected autoconfiguration message addressing or type")
    _unicast(message.source)
    if not multicast:
        _unicast(message.destination)
    if any(t.kind in SECURITY_ENVELOPES for t in message.tlvs):
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "DPP security processing is not implemented")


@dataclass(frozen=True)
class Search:
    al_mac: bytes
    band: int
    profile: int


def parse_search(message: Message) -> Search:
    """Selected fields only; no profile qualification, relay or link authentication."""
    _header(message, SEARCH, multicast=True)
    al_mac = _unicast(_base(message, 0x01, 6))
    if _base(message, 0x0D, 1) != b"\0":
        _invalid("Search does not request Registrar")
    band = _base(message, 0x0E, 1)[0]
    if band not in (0, 1, 2, 3):
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "reserved autoconfiguration band")
    if 1 not in decode_value(0x80, _one(message, 0x80)).known_services:
        _invalid("Search does not advertise Agent service")
    if 0 not in decode_value(0x81, _one(message, 0x81)).known_services:
        _invalid("Search does not request Controller service")
    profile = decode_value(0xB3, _one(message, 0xB3)).profile
    if profile == 1:
        decode_value(0xB4, _one(message, 0xB4))
    return Search(al_mac, band, profile)


@dataclass(frozen=True)
class ControllerAdvertisement:
    band: int
    profile: int
    controller_flags: bytes | None
    security_capability_present: bool
    security_capability: bytes | None = None

    @property
    def selected_response_issues(self):
        """Known field gaps for the non-DPP Search; not full profile admission.

        EasyMesh 6.1 6.1/17.1.2, Tables 90 and 117. Bit 6 is described by
        name but overlaps the reserved range: expose its observed absence while
        retaining that conflict as a separate, unconditional admission blocker.
        Reserved algorithms are unsupported, not inferred to mean algorithm 0.
        """
        issues = []
        if self.controller_flags is None:
            issues.append("controller_capability_absent")
        else:
            if not self.controller_flags[0] & 0x80:
                issues.append("kib_mib_support_absent")
            if not self.controller_flags[0] & 0x40:
                issues.append("early_ap_capability_bit_absent_for_non_dpp_search")
        if not self.security_capability_present:
            issues.append("security_capability_absent")
        elif self.security_capability is None or len(self.security_capability) != 3:
            issues.append("security_capability_length_invalid")
        elif self.security_capability != bytes(3):
            issues.append("security_capability_reserved_algorithm")
        return tuple(issues)

    @property
    def pending_requirements(self):
        # Preserve the Table 117 bit-6/reserved overlap and feature applicability
        # as unresolved, rather than equating discovery with admission to M1.
        gaps = ["profile_qualification", "security_capability_applicability"]
        if not self.security_capability_present:
            gaps.append("security_capability_absent")
        if self.controller_flags is None:
            gaps.append("controller_capability_absent")
        else:
            if not self.controller_flags[0] & 0x80:
                gaps.append("kib_mib_support_absent")
            gaps.append("early_ap_capability_procedure_and_table117_review")
        return tuple(gaps)


# Message sets for Profile-1 onboarding. EASYMESH_61 is EasyMesh 6.1 as selected in
# the protocol matrix: a Profile-1 device's Search carries one Multi-AP Profile TLV
# (value 1) and the Profile-2 AP Capability TLV, and its M1 carries the Profile-2
# AP Capability and AP Radio Advanced Capabilities TLVs (§6.1, §17.1.1). R1 is the
# EasyMesh R1 form, for controllers that validate those TLVs as not allowed for a
# Profile-1 peer (for example RDK unified-wifi-mesh, which checks EasyMesh 5.0
# profile-gated presence): none of the three in Search or M1, a Response without a
# Profile TLV means Profile 1, and the R3 Controller Capability fields and Early
# AP Capability Report do not apply. R1 is a per-controller compatibility choice,
# never a default.
EASYMESH_61 = "easymesh-6.1"
R1 = "r1"
MESSAGE_SETS = (EASYMESH_61, R1)


def check_message_set(value):
    if value not in MESSAGE_SETS:
        _invalid("unknown EasyMesh message set")
    return value


def parse_response(message: Message, *, message_set=EASYMESH_61) -> ControllerAdvertisement:
    _header(message, RESPONSE)
    if _base(message, 0x0F, 1) != b"\0":
        _invalid("Response does not advertise Registrar")
    band = _base(message, 0x10, 1)[0]
    if band not in (0, 1, 2, 3):
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "reserved autoconfiguration band")
    if 0 not in decode_value(0x80, _one(message, 0x80)).known_services:
        _invalid("Response does not advertise Controller service")
    profiles = [t.value for t in message.tlvs if t.kind == 0xB3]
    if message_set == R1 and not profiles:
        profile = 1  # an R1 Response has no Multi-AP Profile TLV
    else:
        profile = decode_value(0xB3, _one(message, 0xB3)).profile
    flags = [t.value for t in message.tlvs if t.kind == 0xDD]
    security = [t.value for t in message.tlvs if t.kind == 0xA9]
    if len(flags) > 1 or (flags and not flags[0]) or len(security) > 1:
        _invalid("ambiguous or empty discovery capability field")
    # 0xDD permits reserved future octets. 0xA9 semantics remain a profile gate;
    # mere presence (including an empty value) never establishes security support.
    return ControllerAdvertisement(
        band,
        profile,
        flags[0] if flags else None,
        bool(security),
        security[0] if security else None,
    )


@dataclass(frozen=True)
class PeerBinding:
    """Operator/coordinator input, not a claim of authenticated Ethernet identity.

    generation must change after a link, controller, pod or radio binding change.
    source_macs explicitly maps controller interface addresses to its AL identity.
    """

    ingress: str
    generation: int
    local_al: bytes
    controller_al: bytes
    source_macs: tuple[bytes, ...]

    def __post_init__(self):
        if (
            not isinstance(self.ingress, str)
            or not 1 <= len(self.ingress) <= 64
            or type(self.generation) is not int
            or self.generation < 0
            or type(self.source_macs) is not tuple
            or not 1 <= len(self.source_macs) <= 16
        ):
            _invalid("invalid explicit peer binding")
        _unicast(self.local_al)
        _unicast(self.controller_al)
        for address in self.source_macs:
            _unicast(address)
        if self.local_al == self.controller_al or self.local_al in self.source_macs:
            _invalid("peer binding aliases the local identity")

    def check(self, message, *, ingress, generation):
        if (
            ingress != self.ingress
            or generation != self.generation
            or message.destination != self.local_al
            or message.source not in self.source_macs
        ):
            _invalid("message does not match the selected peer/link generation")


class _Lifetime:
    def __init__(self, clock, timeout, max_transmissions):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
            or type(max_transmissions) is not int
            or not 1 <= max_transmissions <= 16
        ):
            _invalid("invalid local exchange budget")
        self.clock = clock
        self.deadline = clock() + timeout
        self.max_transmissions = max_transmissions
        self.transmissions = 0
        self.state = "waiting"

    def close(self):
        self.state = "closed"

    def _live(self):
        if self.clock() >= self.deadline:
            self.close()
        if self.state == "closed":
            raise EmosaError(Reason.NOT_READY, "exchange closed; restart discovery with fresh M1")

    def _transmit(self):
        self._live()
        if self.state != "waiting" or self.transmissions >= self.max_transmissions:
            raise EmosaError(Reason.NOT_READY, "exchange transmission budget exhausted")
        self.transmissions += 1


class DiscoveryExchange(_Lifetime):
    """Correlate a bounded Search/Response; result still requires profile admission."""

    def __init__(
        self,
        binding: PeerBinding,
        *,
        band: int,
        profile: int,
        profile2: Profile2APCapability,
        mids: MidSequence,
        clock=time.monotonic,
        timeout=5,
        max_transmissions=3,
        message_set=EASYMESH_61,
    ):
        super().__init__(clock, timeout, max_transmissions)
        self.message_set = check_message_set(message_set)
        # Narrow WSC experiment: no 60 GHz, DPP/6 GHz, or newer profile claim.
        if type(band) is not int or band not in (0, 1) or type(profile) is not int or profile != 1:
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "discovery emitter supports Profile-1 WSC"
            )
        self.binding, self.band, self.profile, self.mids = binding, band, profile, mids
        self.tlvs = (
            Tlv(0x01, binding.local_al),
            Tlv(0x0D, b"\0"),
            Tlv(0x0E, bytes([band])),
            _tlv(SupportedServices((1,))),
            _tlv(SearchedServices((0,))),
        ) + (() if self.message_set == R1 else (_tlv(MultiAPProfile(profile)), _tlv(profile2)))
        self.sent_mids = set()
        self._response = None

    def request(self):
        self._transmit()
        mid = self.mids.next()
        self.sent_mids.add(mid)
        return fragment_message(
            MULTICAST, self.binding.local_al, SEARCH, mid, self.tlvs, relay=True
        )

    def receive(self, message, *, ingress, generation):
        self._live()
        self.binding.check(message, ingress=ingress, generation=generation)
        result = parse_response(message, message_set=self.message_set)
        if message.mid not in self.sent_mids or result.band != self.band:
            _invalid("Response does not match a live Search MID and band")
        # EasyMesh 6.1: the Response echoes the searching agent's profile. An R1
        # agent predates the Multi-AP Profile TLV and does not interpret it, so in
        # the R1 set a present Profile TLV is decoded but not matched.
        effective = MultiAPProfile(result.profile).effective_profile(self.profile)
        if self.message_set != R1 and effective != self.profile:
            _invalid("controller profile does not match the discovery request")
        if self._response is not None and result != self._response:
            _invalid("conflicting discovery advertisement in the same exchange")
        self._response = result
        self.state = "received"
        return result


@dataclass(frozen=True)
class ExchangeResult:
    """A component result, never a write capability or an onboarding verdict."""

    duplicate: bool
    ruid: bytes
    candidate: ExistingBssCandidate = field(repr=False)
    # multi_bss exchanges: the other BSSes of the set, ((role, candidate), ...)
    additional: tuple = field(default=(), repr=False)


class WscExchange(_Lifetime):
    """One fresh M1 transcript, one represented radio, one complete M2 request.

    Does not bypass pending discovery/profile admission. A future endpoint must
    complete those procedures before starting this separately tested component.
    MID is not an authenticator and M2 need not echo the M1 or Search MID.
    """

    def __init__(
        self,
        binding: PeerBinding,
        device: M1Device,
        basic: APRadioBasicCapabilities,
        profile2: Profile2APCapability,
        advanced: APRadioAdvancedCapabilities,
        *,
        mids: MidSequence,
        clock=time.monotonic,
        timeout=5,
        max_transmissions=3,
        message_set=EASYMESH_61,
        multi_bss=False,
        m2_session="distinct",
    ):
        # Read-only discovery/capture inspection does not load WSC crypto.
        from emosa.wsc_messages import M1Transcript

        super().__init__(clock, timeout, max_transmissions)
        if device.al_mac != binding.local_al or basic.ruid != advanced.ruid:
            _invalid("M1 identity and capability radio binding differ")
        _unicast(basic.ruid)
        if device.rf_band not in (1, 2):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "WSC exchange supports 2.4 or 5 GHz")
        self.binding, self.basic, self.mids = binding, basic, mids
        self.profile2, self.advanced = profile2, advanced
        self.message_set = check_message_set(message_set)
        # multi_bss: the radio maps every BSS of one M2 set (up to basic.max_bss);
        # otherwise exactly one existing fronthaul BSS.
        self.multi_bss = multi_bss is True
        # "distinct": one registrar nonce per M2 (default); "shared": one registrar
        # session over the whole M2 set, see M1Transcript.authenticate_m2_envelopes.
        if m2_session not in ("distinct", "shared"):
            _invalid("unknown M2 session policy")
        self.shared_m2_session = m2_session == "shared"
        self.capabilities = (_tlv(basic),) + (
            () if self.message_set == R1 else (_tlv(profile2), _tlv(advanced))
        )
        self._transcript = M1Transcript.create(device)
        self.exchange_id = uuid.uuid4().hex
        self.m1_sha256 = hashlib.sha256(self._transcript.message).hexdigest()
        self._digest = None
        self._candidate = None
        self._additional = ()
        self._configuration = None

    def close(self):
        super().close()
        # Drop references; Python cannot promise cryptographic memory erasure.
        self._transcript = self._candidate = self._digest = self._configuration = None
        self._additional = ()

    def request(self):
        self._transmit()
        return fragment_message(
            self.binding.controller_al,
            self.binding.local_al,
            WSC,
            self.mids.next(),
            (self.capabilities[0], Tlv(0x11, self._transcript.message), *self.capabilities[1:]),
        )

    def receive(self, message, *, ingress, generation):
        from emosa.wsc_messages import MAX_M2_PAYLOADS
        from emosa.wsc_radio import decode_radio_payloads

        self._live()
        self.binding.check(message, ingress=ingress, generation=generation)
        _header(message, WSC)
        if not self.transmissions:
            _invalid("M2 received before this exchange sent M1")
        if _one(message, 0x82) != self.basic.ruid:
            _invalid("M2 Radio Identifier differs from the initiating radio")
        messages = tuple(t.value for t in message.tlvs if t.kind == 0x11)
        try:
            # An AP MLD Configuration listing zero AP MLDs (EasyMesh 6.x §17.2.96,
            # first octet = number of AP MLDs) configures nothing on a non-MLD
            # radio; any other companion configuration is never silently dropped.
            if any(
                t.kind in CONFIGURATION_COMPANIONS and not (t.kind == 0xE0 and t.value == b"\0")
                for t in message.tlvs
            ):
                raise EmosaError(
                    Reason.UNSUPPORTED_OPERATION,
                    "complete M2 request has unsupported configuration TLVs",
                )
            if not 1 <= len(messages) <= min(self.basic.max_bss, MAX_M2_PAYLOADS):
                _invalid("WSC payload count exceeds advertised radio scope or local budget")
        except EmosaError:
            if self.state == "waiting":
                self.close()
            raise
        # Hash length-delimited complete WSC occurrences, excluding transport MID
        # and genuinely unrelated TLVs; never publish credential-derived digests.
        digest = hashlib.sha256(
            b"".join(len(value).to_bytes(4, "big") + value for value in messages)
        ).digest()
        if self.state == "received" and digest == self._digest:
            return ExchangeResult(True, self.basic.ruid, self._candidate)
        try:
            radio = decode_radio_payloads(
                self._transcript,
                messages,
                max_bss=min(self.basic.max_bss, MAX_M2_PAYLOADS),
                shared_session=self.shared_m2_session,
            )
            if self.multi_bss:
                pairs = radio.radio_candidates()
                primary = next(i for i, (role, _) in enumerate(pairs) if role == "fronthaul")
                candidate = pairs[primary][1]
                additional = pairs[:primary] + pairs[primary + 1 :]
            else:
                candidate, additional = radio.existing_fronthaul_candidate(), ()
        except EmosaError:
            # An unsuccessful parameter-configuration phase requires discovery
            # restart (IEEE 10.1.2), never reuse this failed transcript.
            if self.state == "waiting":
                self.close()
            raise
        if self.state == "received":
            # A registrar may rebuild M2 with fresh nonce/IV. Authenticate again
            # and compare the entire decoded configuration, not just SSID/key.
            if radio != self._configuration:
                _invalid("conflicting M2 after a complete request was already accepted")
            self._digest = digest
            return ExchangeResult(True, self.basic.ruid, self._candidate, self._additional)
        self._digest, self._candidate, self._additional = digest, candidate, additional
        self._configuration = radio
        self.state = "received"
        return ExchangeResult(False, self.basic.ruid, candidate, additional)
