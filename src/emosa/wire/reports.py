# SPDX-License-Identifier: Apache-2.0
"""Complete bounded report construction and guarded delivery, without a daemon.

Facts and freshness tokens must come from a qualified coordinator. Synthetic
callers exercise this component; creating one does not qualify a profile, trust
an Ethernet peer or authorize pod operations. See docs/reference/protocol/reports.md.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from emosa.easymesh_payloads import (
    AKMSuiteCapabilities,
    APCapability,
    APHTCapabilities,
    APOperationalBss,
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    AssociatedClient,
    AssociatedClients,
    BssClients,
    BssConfigurationReport,
    MultiAPProfile,
    Profile2APCapability,
    SupportedCipherSuites,
    SupportedServices,
    decode_value,
    encode_value,
)
from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import EASYMESH_61, R1, PeerBinding, check_message_set
from emosa.wire.cmdu import MidSequence, Tlv, fragment_message, invalid
from emosa.wire.topology_values import (
    BridgingCapability,
    DeviceInformation,
    Neighbors1905,
    Non1905Neighbors,
    encode_topology,
)

EARLY_AP_REPORT = 0x8043
PSK = bytes.fromhex("000fac02")
CCMP128 = bytes.fromhex("000fac04")


def _require(condition, detail):
    if not condition:
        raise EmosaError(Reason.NOT_READY, detail)


def _identity(value):
    if not isinstance(value, bytes) or len(value) != 6 or value == bytes(6) or value[0] & 1:
        invalid("report identity must be a nonzero unicast MAC")


def _unique(values):
    for value in values:
        _identity(value)
    _require(len(set(values)) == len(values), "duplicate identity in report inventory")


def _time(value):
    if type(value) not in (float, int) or not math.isfinite(value):
        invalid("invalid report clock value")
    return value


@dataclass(frozen=True)
class ReportStamp:
    """Coordinator-issued token; equality is correlation, not attestation.

    token must cover adapter instance, pod, binding generation, snapshot revision
    and relevant input hashes. Advancing any source must change the token.
    """

    token: str
    observed_at: float
    valid_until: float

    def __post_init__(self):
        if not isinstance(self.token, str) or not 1 <= len(self.token) <= 256:
            invalid("invalid report source token")
        if not 0 < _time(self.valid_until) - _time(self.observed_at) <= 2:
            invalid("report freshness budget must be positive and at most two seconds")

    def check(self, current, now):
        _require(isinstance(current, ReportStamp), "report source is unavailable")
        _require(
            current.token == self.token
            and self.observed_at <= now < self.valid_until
            and current.observed_at <= now < current.valid_until,
            "report source changed or expired before delivery",
        )


@dataclass(frozen=True)
class PreparedReport:
    message_type: int
    mid: int
    stamp: ReportStamp
    deadline: float
    frames: tuple[bytes, ...] = field(repr=False)

    def send(self, send_frame: Callable, current_stamp: Callable, *, clock=time.monotonic):
        """Synchronous bounded writer; check every fragment, including after send.

        A late/partial transmission raises and must not be reported as on-time
        success. Emitted Ethernet bytes cannot be recalled. The caller owns
        admission/rate limits and reports this failure separately from pod state.
        """
        for frame in self.frames:
            current = current_stamp()
            now = clock()
            self.stamp.check(current, now)
            _require(now < self.deadline, "report response deadline expired")
            send_frame(frame)
            current = current_stamp()
            now = clock()
            self.stamp.check(current, now)
            _require(now < self.deadline, "report transmission exceeded its response deadline")
        return len(self.frames)


@dataclass(frozen=True)
class EarlyRadio:
    basic: APRadioBasicCapabilities
    ht: APHTCapabilities | bool
    advanced: APRadioAdvancedCapabilities
    vht: bool
    he: bool
    eht: bool


@dataclass(frozen=True)
class EarlyCapabilities:
    radios: tuple[EarlyRadio, ...]
    ap: APCapability
    profile2: Profile2APCapability
    akm: AKMSuiteCapabilities
    ciphers: SupportedCipherSuites
    inventory_complete: bool


def _em(value):
    return Tlv(value.kind, encode_value(value))


def capabilities_identity(facts: EarlyCapabilities):
    """The capabilities apart from their operable channels (spec 2.5): a session is kept across
    its pod's reconnection, and across a radio's channel move, while this stays the same. Every
    radio's RUID, maximum BSS, operating classes and their maximum EIRP, its HT and advanced
    capabilities and the device's own are kept; which channels are operable is left out (each
    radio is operable on its current channel only, and that channel follows the pod's uplink)."""
    return replace(
        facts,
        radios=tuple(
            replace(
                radio,
                basic=replace(
                    radio.basic,
                    operating_classes=tuple(
                        replace(c, non_operable_channels=()) for c in radio.basic.operating_classes
                    ),
                ),
            )
            for radio in facts.radios
        ),
    )


RADIO_CAPABILITIES = {"ht": "HT", "vht": "VHT", "he": "HE", "eht": "EHT", "advanced": "advanced"}


def identity_difference(before: EarlyCapabilities, after: EarlyCapabilities):
    """What differs between two capability reports apart from their operable channels (spec
    2.5): the first radio field or TLV, for the log of a fresh session (capabilities_changed);
    "" when their identities are the same."""
    old, new = capabilities_identity(before), capabilities_identity(after)
    if len(old.radios) != len(new.radios):
        return f"radios {len(old.radios)} -> {len(new.radios)}"
    for a, b in zip(old.radios, new.radios, strict=True):
        ruid = a.basic.ruid.hex(":")
        if a.basic.ruid != b.basic.ruid:
            return f"radio {ruid} -> {b.basic.ruid.hex(':')}"
        if a.basic.max_bss != b.basic.max_bss:
            return f"radio {ruid} max BSS {a.basic.max_bss} -> {b.basic.max_bss}"
        classes = [c.operating_class for c in a.basic.operating_classes]
        others = [c.operating_class for c in b.basic.operating_classes]
        if classes != others:
            listed = ",".join(map(str, classes)), ",".join(map(str, others))
            return f"radio {ruid} operating classes {listed[0]} -> {listed[1]}"
        for c, d in zip(a.basic.operating_classes, b.basic.operating_classes, strict=True):
            if c.max_eirp_dbm != d.max_eirp_dbm:
                return (
                    f"radio {ruid} class {c.operating_class} max EIRP "
                    f"{c.max_eirp_dbm} -> {d.max_eirp_dbm} dBm"
                )
        for part, name in RADIO_CAPABILITIES.items():
            if getattr(a, part) != getattr(b, part):
                return f"radio {ruid} {name} capabilities"
    for part in ("ap", "profile2", "akm", "ciphers"):
        if getattr(old, part) != getattr(new, part):
            return f"TLV 0x{getattr(old, part).kind:02x}"
    return ""


def operable_channels_moved(before: EarlyCapabilities, after: EarlyCapabilities):
    """The RUIDs of the radios whose operable channels differ between two capability reports of
    the same identity (spec 2.5), in report order."""
    return tuple(
        a.basic.ruid
        for b, a in zip(before.radios, after.radios, strict=True)
        if b.basic.operating_classes != a.basic.operating_classes
    )


def capability_tlvs(facts: EarlyCapabilities):
    """Validate the complete restricted capability inventory before encoding."""
    _require(facts.inventory_complete is True, "complete capability inventory is required")
    _require(
        type(facts.radios) is tuple and 1 <= len(facts.radios) <= 32, "invalid radio inventory"
    )
    _require(
        facts.ap == APCapability(0)
        and facts.profile2.max_prioritization_rules == 0
        and facts.profile2.max_vids == 0
        and facts.profile2.flags in (0, 0x40, 0x80)
        and facts.akm == AKMSuiteCapabilities((), (PSK,))
        and facts.ciphers == SupportedCipherSuites((CCMP128,)),
        "capabilities exceed the implemented fronthaul report contract",
    )
    _unique([radio.basic.ruid for radio in facts.radios])
    tlvs = [_em(facts.ap)]
    for radio in facts.radios:
        _require(
            radio.vht is False and radio.he is False and radio.eht is False,
            "unknown or unsupported technology needs its complete companion report",
        )
        # 2.4 GHz, or the non-DFS 5 GHz UNII-1 and UNII-3 classes (115, 124, 125) of a pod's
        # second radio (spec 9); wider or DFS 5 GHz classes and 6 GHz need their companion reports
        _require(
            radio.basic.operating_classes
            and all(
                op.operating_class in (81, 82, 83, 84, 115, 124, 125)
                for op in radio.basic.operating_classes
            ),
            "early report currently requires explicit non-DFS 2.4 GHz, UNII-1 or UNII-3 capability",
        )
        _require(
            radio.advanced.ruid == radio.basic.ruid and radio.advanced.flags == 0,
            "advanced capability does not match the supported radio scope",
        )
        tlvs.append(_em(radio.basic))
        if radio.ht is not False:
            _require(
                type(radio.ht) is APHTCapabilities and radio.ht.ruid == radio.basic.ruid,
                "HT support must be explicit and bound to the same radio",
            )
            tlvs.append(_em(radio.ht))
        tlvs.append(_em(radio.advanced))
    tlvs.extend((_em(facts.akm), _em(facts.profile2), _em(facts.ciphers)))
    return tuple(tlvs)


def early_report(
    binding: PeerBinding,
    facts: EarlyCapabilities,
    stamp,
    mids: MidSequence,
    *,
    clock=time.monotonic,
):
    """Initial non-DFS 2.4 GHz, non-HE/EHT, pure-fronthaul PSK/CCMP scope.

    This is the complete Early report for the declared restricted conditions,
    not an AP Capability Report (0x8002) or a complete Profile-1 implementation.
    """
    stamp.check(stamp, clock())
    tlvs = capability_tlvs(facts)
    mid = mids.next()
    frames = fragment_message(binding.controller_al, binding.local_al, EARLY_AP_REPORT, mid, tlvs)
    return PreparedReport(EARLY_AP_REPORT, mid, stamp, stamp.valid_until, frames)


@dataclass(frozen=True)
class TopologyFacts:
    device: DeviceInformation
    bridges: BridgingCapability
    non1905: tuple[Non1905Neighbors, ...]
    neighbors1905: tuple[Neighbors1905, ...]
    operational: APOperationalBss
    configuration: BssConfigurationReport
    clients: AssociatedClients
    inventory_complete: bool
    powered_off_interfaces_absent: bool
    l2_neighbor_records_absent: bool
    mld_backhaul_vbss_tid_policy_absent: bool
    # (RUID, backhaul STA MAC) of each radio whose station is the agent's EasyMesh
    # backhaul: the station a Backhaul Steering Request names. Empty over GRE.
    backhaul_stations: tuple = ()
    # (RUID, backhaul STA MAC or None) of each radio of the pod with a backhaul station,
    # enabled or not, the one in use first: the Backhaul STA Capability Report (0x8028),
    # one TLV per backhaul STA radio (EasyMesh 6.1, 9.3).
    backhaul_radios: tuple = ()
    # (station MAC, monotonic association time) of associated stations: a Topology
    # Response reports each station's time since association as of the response
    # (the controller compares it with the age of every link metric sample).
    associated_at: tuple = ()


def aged_clients(facts, now):
    """The Associated Clients TLV with each station's age as of ``now``."""
    since = dict(facts.associated_at)
    return AssociatedClients(
        tuple(
            BssClients(
                bss.bssid,
                tuple(
                    AssociatedClient(c.mac, min(65535, max(0, int(now - since[c.mac]))))
                    if c.mac in since
                    else c
                    for c in bss.clients
                ),
            )
            for bss in facts.clients.bsses
        )
    )


def _topology(facts, binding, message_set=EASYMESH_61, now=None):
    if facts.associated_at and now is not None:
        facts = replace(facts, clients=aged_clients(facts, now))
    _require(
        all(
            flag is True
            for flag in (
                facts.inventory_complete,
                facts.powered_off_interfaces_absent,
                facts.l2_neighbor_records_absent,
                facts.mld_backhaul_vbss_tid_policy_absent,
            )
        ),
        "topology inventory or conditional procedure facts are unavailable",
    )
    _require(facts.device.al_mac == binding.local_al, "topology AL differs from represented agent")
    interfaces = [i.mac for i in facts.device.interfaces]
    _require(1 <= len(interfaces) <= 64, "local interface inventory exceeds selected scope")
    _unique(interfaces)
    bridge_macs = [mac for group in facts.bridges.tuples for mac in group]
    _unique(bridge_macs)
    _require(set(bridge_macs) <= set(interfaces), "bridging tuple references an absent interface")
    for lists in (facts.non1905, facts.neighbors1905):
        _unique([row.local_interface for row in lists])
        for row in lists:
            _require(row.local_interface in interfaces, "neighbor list uses an absent interface")
            addresses = [getattr(n, "al_mac", n) for n in row.neighbors]
            _unique(addresses)
            _require(len(addresses) <= 256, "neighbor inventory exceeds local budget")
    _require(
        1 <= len(facts.operational.radios) <= 32
        and len(facts.configuration.radios) == len(facts.operational.radios),
        "complete operational/configured radio inventories are required",
    )
    _unique([r.ruid for r in facts.operational.radios])
    _unique([r.ruid for r in facts.configuration.radios])
    operational = {(r.ruid, b.ap_mac): b.ssid for r in facts.operational.radios for b in r.bsses}
    configured = {(r.ruid, b.bssid): b.ssid for r in facts.configuration.radios for b in r.bsses}
    bsses = [b.ap_mac for r in facts.operational.radios for b in r.bsses]
    _unique(bsses)
    _unique([b.bssid for r in facts.configuration.radios for b in r.bsses])
    _require(
        operational == configured
        and {r.ruid for r in facts.operational.radios}
        == {r.ruid for r in facts.configuration.radios},
        "operational and BSS configuration reports disagree",
    )
    _require(
        all(b.flags in (0x40, 0x80) for r in facts.configuration.radios for b in r.bsses),
        "topology report supports pure fronthaul or backhaul non-MBSSID BSSs",
    )
    for bssid in bsses:
        matches = [i for i in facts.device.interfaces if i.mac == bssid]
        _require(
            len(matches) == 1
            and 0x100 <= matches[0].media_type <= 0x107
            and len(matches[0].media_specific) == 10
            and matches[0].media_specific[:6] == bssid
            and matches[0].media_specific[6] == 0,
            "active BSS is missing its matching local AP interface",
        )
    _unique([b.bssid for b in facts.clients.bsses])
    _require(
        {b.bssid for b in facts.clients.bsses} == set(bsses),
        "explicit client inventory required for every active BSS (including empty lists)",
    )
    clients = [client.mac for bss in facts.clients.bsses for client in bss.clients]
    _unique(clients)
    _require(len(clients) <= 256, "client inventory exceeds local report budget")
    values = [facts.device]
    if len(interfaces) > 1 or facts.bridges.tuples:
        values.append(facts.bridges)
    values.extend((*facts.non1905, *facts.neighbors1905))
    tlvs = [Tlv(value.kind, encode_topology(value)) for value in values]
    tlvs.extend((_em(SupportedServices((1,))), _em(facts.operational)))
    if message_set != R1:  # R1 has no BSS Configuration Report and no Profile TLV
        tlvs.append(_em(facts.configuration))
    # Associated Clients always, every active BSS with its stations, none included, as RDK's own
    # agent sends it: the controller takes it as the agent's complete association snapshot, so a
    # station whose leave it missed goes at the next response (spec 2.6; a pod's BSS emptied by
    # that leave kept a stale association, 28 October)
    tlvs.append(_em(facts.clients))
    if facts.backhaul_radios:
        # the Backhaul STA Radio Capabilities (spec 8.6): the in-use bSTA MAC on its radio (the
        # in-use radio comes first), reported in the Topology Response so the controller models a
        # station-mode backhaul BSS and can steer the pod -- a standard TLV, not the RDK vendor
        # operational BSS. The controller reads the first, matching the C's single in-use uplink.
        ruid, mac = facts.backhaul_radios[0]
        tlvs.append(Tlv(0xCB, ruid + (bytes([0x80]) + mac if mac else bytes([0x00]))))
    if message_set != R1:
        tlvs.append(_em(MultiAPProfile(1)))
    return tlvs


def topology_response(
    query,
    binding: PeerBinding,
    facts: TopologyFacts,
    stamp,
    *,
    ingress,
    generation,
    received_at,
    clock=time.monotonic,
    message_set=EASYMESH_61,
):
    """Reply to an admitted Query with its MID, within IEEE 8.2.2.2's one second.

    `received_at` is the coordinator's monotonic complete-message receive time,
    never a value taken from the network. The source callback is rechecked on send.
    """
    now = clock()
    stamp.check(stamp, now)
    _require(_time(received_at) <= now < received_at + 1, "Topology Query response window expired")
    binding.check(query, ingress=ingress, generation=generation)
    _require(query.message_type == 2 and not query.relay, "expected a unicast Topology Query")
    check_message_set(message_set)
    profiles = [t.value for t in query.tlvs if t.kind == 0xB3]
    _require(
        len(profiles) == 1 or (message_set == R1 and not profiles),
        "Topology Query requires one Multi-AP Profile",
    )
    # EasyMesh 6.1 §6.2: Query advertises the sender's highest profile,
    # unlike discovery Response, which echoes the searching agent's profile.
    # Decode it without upgrading our own Profile-1 response or capabilities.
    if profiles:
        decode_value(0xB3, profiles[0]).effective_profile(1)
    _require(
        not any(t.kind in (0xAB, 0xAC) for t in query.tlvs),
        "DPP security processing is required before query dispatch",
    )
    frames = fragment_message(
        binding.controller_al,
        binding.local_al,
        3,
        query.mid,
        _topology(facts, binding, message_set, now),
    )
    return PreparedReport(3, query.mid, stamp, received_at + 1, frames)
