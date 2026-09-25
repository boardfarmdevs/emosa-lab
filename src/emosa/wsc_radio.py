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


def _unsupported(detail=None):
    """Non-secret reason: role bits, type codes or limits, never SSID/key values."""
    message = "radio WSC payload set exceeds existing fronthaul PSK scope"
    if detail is None:
        return EmosaError(Reason.UNSUPPORTED_OPERATION, message)
    return EmosaError(Reason.UNSUPPORTED_OPERATION, f"{message}: {detail}", detail=detail)


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
        if self.bsses[0].multi_ap_flags != FRONTHAUL_BSS:
            raise _unsupported()
        return self._psk_candidate(self.bsses[0])

    def radio_candidates(self) -> tuple[tuple[str, ExistingBssCandidate], ...]:
        """Every BSS of the set with its role, for a radio that maps several BSSes.

        Roles: exactly Fronthaul BSS, or Backhaul BSS (its Profile-1/Profile-2
        backhaul STA disallowed bits are accepted and not applied; so is the Backhaul
        STA bit, which prplMesh sets on its backhaul BSS: the same credentials serve
        the agent's backhaul station, see emosa.agent.uplink). A combined
        fronthaul+backhaul BSS, teardown or any other role is unsupported, as is
        any BSS outside the WPA2-PSK/AES shape. At least one fronthaul BSS.
        """
        if self.action != "configure" or not self.bsses:
            raise _unsupported()
        pairs = []
        for bss in self.bsses:
            flags = bss.multi_ap_flags
            if flags == FRONTHAUL_BSS:
                role = "fronthaul"
            elif (
                flags & ~(PROFILE1_DISALLOWED | PROFILE2_DISALLOWED | BACKHAUL_STA) == BACKHAUL_BSS
            ):
                role = "backhaul"
            else:
                raise _unsupported(f"BSS {len(pairs)}: Multi-AP role 0x{flags:02x}")
            try:
                pairs.append((role, self._psk_candidate(bss)))
            except EmosaError as exc:
                detail = exc.details.get("detail", "unsupported settings")
                raise _unsupported(f"BSS {len(pairs)} ({role}): {detail}") from None
        if not any(role == "fronthaul" for role, _ in pairs):
            raise _unsupported()
        return tuple(pairs)

    def _psk_candidate(self, bss) -> ExistingBssCandidate:
        settings = bss.settings
        if (
            settings.authentication_type != 0x20
            or settings.encryption_type != 0x08
            or not self.advertised_authentication_types & 0x20
            or not self.advertised_encryption_types & 0x08
        ):
            raise _unsupported(
                f"authentication 0x{settings.authentication_type:04x}"
                f" encryption 0x{settings.encryption_type:04x}"
            )
        extra = sorted(a.kind for a in settings.attributes if a.kind in {0x102A, 0x1012})
        if extra:
            raise _unsupported("settings attribute " + ",".join(f"0x{k:04x}" for k in extra))
        # WPS 2.0.10 Network Key definition requires compatibility with one
        # trailing NUL on legacy passphrases. Never truncate a raw 64-hex PSK.
        key = settings.network_key.removesuffix(b"\0")
        # Likewise one trailing NUL on the SSID is a C-string terminator some
        # registrars include (RDK unified-wifi-mesh); any other NUL is refused.
        raw_ssid = settings.ssid.removesuffix(b"\0")
        try:
            ssid = raw_ssid.decode("utf-8")
            passphrase = key.decode("ascii")
        except UnicodeError:
            raise _unsupported("SSID or key encoding") from None
        # Existing EMOSA mapping limits, not general SSID/network-key validity.
        for failed, reason in (
            (not 1 <= len(raw_ssid) <= 32, "SSID length"),
            ("\0" in ssid, "SSID with an embedded NUL"),
            (b"\0" in key, "passphrase with NUL padding"),
            (not 8 <= len(key) <= 63, "passphrase length"),
            (any(not 0x20 <= value <= 0x7E for value in key), "passphrase characters"),
        ):
            if failed:
                raise _unsupported(f"{reason} outside the mapping limits")
        return ExistingBssCandidate(ssid, passphrase, bss.bss_index)


def decode_radio_payloads(
    transcript: M1Transcript, messages: tuple[bytes, ...], *, max_bss: int, shared_session=False
) -> RadioPayloadSet:
    """Authenticate every M2 before interpreting the encrypted Multi-AP roles.

    `max_bss` is a caller-supplied bound and cannot establish a radio capability.
    Reserved bits 1:0 are ignored per EasyMesh 6.1 section 3.1.2. All other bits
    remain explicit. Ambiguous/misplaced role fields fail before returning data.
    """
    envelopes = transcript.authenticate_m2_envelopes(
        messages, max_bss=max_bss, shared_session=shared_session
    )
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
