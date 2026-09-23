"""Complete bounded report construction and guarded delivery, without a daemon.

Facts and freshness tokens must come from a qualified coordinator. Synthetic
callers exercise this component; creating one does not qualify a profile, trust
an Ethernet peer or authorize pod operations. See doc/protocol/reports.md.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from emosa.easymesh_payloads import (
    AKMSuiteCapabilities,
    APCapability,
    APHTCapabilities,
    APOperationalBss,
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    AssociatedClients,
    BssConfigurationReport,
    MultiAPProfile,
    Profile2APCapability,
    SupportedCipherSuites,
    SupportedServices,
    decode_value,
    encode_value,
)
from emosa.errors import EmosaError, Reason
from emosa.wire.autoconfiguration import PeerBinding
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
        _require(
            radio.basic.operating_classes
            and all(op.operating_class in (81, 82, 83, 84) for op in radio.basic.operating_classes),
            "early report currently requires explicit non-DFS 2.4 GHz capability",
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


def _topology(facts, binding):
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
        all(b.flags == 0x40 for r in facts.configuration.radios for b in r.bsses),
        "topology report currently supports pure fronthaul non-MBSSID BSSs",
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
    tlvs.extend((_em(SupportedServices((1,))), _em(facts.operational), _em(facts.configuration)))
    if clients:
        tlvs.append(_em(facts.clients))
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
    profiles = [t.value for t in query.tlvs if t.kind == 0xB3]
    _require(len(profiles) == 1, "Topology Query requires one Multi-AP Profile")
    # EasyMesh 6.1 §6.2: Query advertises the sender's highest profile,
    # unlike discovery Response, which echoes the searching agent's profile.
    # Decode it without upgrading our own Profile-1 response or capabilities.
    decode_value(0xB3, profiles[0]).effective_profile(1)
    _require(
        not any(t.kind in (0xAB, 0xAC) for t in query.tlvs),
        "DPP security processing is required before query dispatch",
    )
    frames = fragment_message(
        binding.controller_al, binding.local_al, 3, query.mid, _topology(facts, binding)
    )
    return PreparedReport(3, query.mid, stamp, received_at + 1, frames)
