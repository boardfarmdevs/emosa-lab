"""Selected EasyMesh 6.1 section 7.1 semantics of one radio's M2 payload set.

No IEEE framing, controller trust, radio identity binding, external TLV parsing,
operation submission or pod writes. See doc/protocol/wsc-radio.md for the exact boundary.
"""

from dataclasses import dataclass, field

from emosa.errors import EmosaError, Reason
from emosa.wsc import decode_attributes
from emosa.wsc_messages import APSettings, M1Transcript, vendor_subelements

MULTI_AP = 0x06
BACKHAUL_STA = 0x80
BACKHAUL_BSS = 0x40
FRONTHAUL_BSS = 0x20
TEARDOWN = 0x10
PROFILE1_DISALLOWED = 0x08
PROFILE2_DISALLOWED = 0x04


def _invalid():
    return EmosaError(Reason.INVALID_INPUT, "invalid radio WSC payload set")


def _unsupported():
    return EmosaError(
        Reason.UNSUPPORTED_OPERATION, "radio WSC payload set exceeds existing fronthaul PSK scope"
    )


@dataclass(frozen=True)
class BssPayload:
    bss_index: int | None
    multi_ap_flags: int
    settings: APSettings = field(repr=False)


@dataclass(frozen=True)
class ExistingBssCandidate:
    """Secret desired values, without a pod/radio/VIF binding or write authority."""

    ssid: str = field(repr=False)
    passphrase: str = field(repr=False)
    bss_index: int | None


@dataclass(frozen=True)
class RadioPayloadSet:
    action: str
    bsses: tuple[BssPayload, ...] = field(repr=False)
    advertised_authentication_types: int
    advertised_encryption_types: int

    def existing_fronthaul_candidate(self) -> ExistingBssCandidate:
        """Return values only when the complete set fits the narrow mapping shape.

        The caller still needs validated complete CMDU semantics and a qualified
        sole-BSS radio binding. A single received M2 does not prove sole-BSS scope.
        No interpretation of the M2 MAC attribute as a pod/VIF identity is made.
        """
        if self.action != "configure" or len(self.bsses) != 1:
            raise _unsupported()
        bss = self.bsses[0]
        settings = bss.settings
        if (
            bss.multi_ap_flags != FRONTHAUL_BSS
            or settings.authentication_type != 0x20
            or settings.encryption_type != 0x08
            or not self.advertised_authentication_types & 0x20
            or not self.advertised_encryption_types & 0x08
            or any(a.kind in {0x102A, 0x1012} for a in settings.attributes)
        ):
            raise _unsupported()
        # WPS 2.0.10 Network Key definition requires compatibility with one
        # trailing NUL on legacy passphrases. Never truncate a raw 64-hex PSK.
        key = settings.network_key.removesuffix(b"\0")
        try:
            ssid = settings.ssid.decode("utf-8")
            passphrase = key.decode("ascii")
        except UnicodeError:
            raise _unsupported() from None
        # Existing EMOSA mapping limits, not general SSID/network-key validity.
        if (
            not 1 <= len(settings.ssid) <= 32
            or "\0" in ssid
            or not 8 <= len(key) <= 63
            or any(not 0x20 <= value <= 0x7E for value in key)
        ):
            raise _unsupported()
        return ExistingBssCandidate(ssid, passphrase, bss.bss_index)


def decode_radio_payloads(
    transcript: M1Transcript, messages: tuple[bytes, ...], *, max_bss: int
) -> RadioPayloadSet:
    """Authenticate every M2 before interpreting the encrypted Multi-AP roles.

    `max_bss` is a caller-supplied bound and cannot establish a radio capability.
    Reserved bits 1:0 are ignored per EasyMesh 6.1 section 3.1.2. All other bits
    remain explicit. Ambiguous/misplaced role fields fail before returning data.
    """
    envelopes = transcript.authenticate_m2_envelopes(messages, max_bss=max_bss)
    m1 = {a.kind: a.value for a in decode_attributes(transcript.message)}
    auth = int.from_bytes(m1[0x1004], "big")
    encr = int.from_bytes(m1[0x1010], "big")
    roles = []
    for envelope in envelopes:
        if any(a.kind == MULTI_AP for a in vendor_subelements(envelope.attributes)):
            raise _invalid()  # The role must be inside encrypted ConfigData.
        values = [a.value for a in vendor_subelements(envelope.config_data) if a.kind == MULTI_AP]
        if len(values) != 1 or len(values[0]) != 1:
            raise _invalid()
        roles.append(values[0][0] & 0xFC)
    if any(flags & TEARDOWN for flags in roles):
        if len(envelopes) != 1:
            raise _invalid()  # Controller teardown consists of one M2, section 7.1.
        # Ignore other settings only after the entire envelope and KWA passed.
        return RadioPayloadSet("teardown", (), auth, encr)
    bsses = tuple(
        BssPayload(envelope.bss_index, flags, envelope.ap_configuration().settings)
        for envelope, flags in zip(envelopes, roles, strict=True)
    )
    return RadioPayloadSet("configure", bsses, auth, encr)
