# SPDX-License-Identifier: Apache-2.0
"""The OpenSync OVSDB model as the EasyMesh / IEEE 1905.1 model: one pure translation.

EMOSA's virtual agent tells an EasyMesh controller what an OpenSync pod is and
does. Everything it says comes from the pod's own State tables, through two pure
steps (no I/O, no clock):

1. :func:`device_view`: decoded OVSDB rows -> :class:`DeviceView`, a small
   EasyMesh-shaped model of the pod: device, radios, BSSes, stations.
2. :func:`radio_capabilities`, :func:`inventory` and :func:`topology`:
   a view -> the EasyMesh payloads the agent sends (AP Radio Basic
   Capabilities, Device Inventory, Topology Response contents).

The mapping (see docs/concepts/opensync-easymesh-mapping.md):

=========================================  ======================================
OpenSync (State tables)                    EasyMesh / 1905.1
=========================================  ======================================
AWLAN_Node serial_number, model,           Device Inventory: serial, software
firmware_version                           version (and the fleet's agent AL MAC)
Wifi_Radio_State (mac, freq_band,          Radio: RUID = radio MAC; operating
channel, tx_power)                         class from band; channel; max EIRP
Wifi_VIF_State mode=ap, enabled, mac,      BSS: BSSID = VIF MAC, SSID; operated
listed in its radio's vif_states           only when the pod's State shows it
Wifi_VIF_State multi_ap                    BSS Configuration Report flags:
                                           backhaul_bss 0x80, else fronthaul 0x40
Wifi_Associated_Clients state=active,      Associated Clients of that BSS
listed in the VIF's associated_clients
Wifi_VIF_State mode=sta (backhaul STA)     the pod's Wi-Fi uplink. Reported only
                                           as an EasyMesh backhaul (option 1): a
                                           non-AP STA interface on the parent
                                           BSSID, and Backhaul STA Radio
                                           Capabilities. Over GRE (option 2) it
                                           is not an EasyMesh link: declared
                                           Ethernet to EMOSA only
=========================================  ======================================

Southbound, the controller's M2 set becomes VIF rows (``pod_profile``); the
pod's managers apply them, and only this view confirms what they applied.
"""

from dataclasses import dataclass, field

from emosa.easymesh_payloads import (
    AKMSuiteCapabilities,
    APCapability,
    APHTCapabilities,
    APOperationalBss,
    APRadioAdvancedCapabilities,
    APRadioBasicCapabilities,
    AssociatedClient,
    AssociatedClients,
    BasicOperatingClass,
    BssClients,
    BssConfigurationReport,
    ConfiguredBss,
    ConfiguredRadio,
    DeviceInventory,
    InventoryRadio,
    OperationalBss,
    OperationalRadio,
    Profile2APCapability,
    SupportedCipherSuites,
)
from emosa.errors import EmosaError, Reason
from emosa.wire.reports import CCMP128, PSK, EarlyCapabilities, EarlyRadio, TopologyFacts
from emosa.wire.topology_values import (
    BridgingCapability,
    DeviceInformation,
    LocalInterface,
    Neighbor,
    Neighbors1905,
)

FRONTHAUL, BACKHAUL = "fronthaul", "backhaul"
REPORT_FLAGS = {FRONTHAUL: 0x40, BACKHAUL: 0x80}  # BSS Configuration Report
# freq_band -> (global operating class of a 20 MHz channel, its channels)
# Global operating classes (802.11 Annex E / EasyMesh). The second radio of spec 9 runs on the
# 5 GHz onboarding BSS's band, UNII-1 (opclass 115, 20 MHz, ch 36-48), which carries the pods'
# bootstrap and the GRE parent AP b-ap-50 on rdk-1010 and V54's Pis. Wider 5 GHz sub-bands (118,
# 121, 125) and 6 GHz are not mapped yet (spec 9).
OPERATING_CLASSES = {"2.4G": (81, tuple(range(1, 14))), "5G": (115, (36, 40, 44, 48))}
IEEE_802_11N_24 = 0x0103  # 1905.1 media type of an 802.11n 2.4 GHz interface
MEDIA = {"2.4G": IEEE_802_11N_24, "5G": 0x0104}  # 802.11n per band (no HT claims beyond)
AP_ROLE, STA_ROLE = 0x00, 0x40  # 1905.1 802.11 media-specific role: AP, non-AP STA


def mac(text):
    value = bytes.fromhex(text.replace(":", ""))
    if len(value) != 6:
        raise EmosaError(Reason.INVALID_INPUT, "not a MAC address")
    return value


def role(multi_ap, parent=False):
    """The EasyMesh role of an AP VIF from its OpenSync ``multi_ap`` value; a GRE parent's AP
    (spec 8.6, ``multi_ap`` none) is a backhaul BSS."""
    return BACKHAUL if parent or multi_ap == "backhaul_bss" else FRONTHAUL


@dataclass(frozen=True)
class BssView:
    if_name: str
    bssid: bytes
    ssid: str
    role: str
    stations: tuple[bytes, ...]
    vif: dict = field(compare=False, repr=False)  # its State row, for profile checks

    @property
    def report_flags(self):
        return REPORT_FLAGS[self.role]


@dataclass(frozen=True)
class UplinkView:
    """A backhaul STA: the pod's Wi-Fi link to its parent."""

    if_name: str
    mac: bytes
    ssid: str
    parent: bytes | None = None  # the BSSID it is associated with
    multi_ap: bool = False  # a Multi-AP backhaul STA (4-address), not a 3-address station


@dataclass(frozen=True)
class BackhaulView:
    """The pod's EasyMesh backhaul (option 1): its station, on which radio, to which BSS."""

    ruid: bytes
    band: str
    channel: int
    station: UplinkView


@dataclass(frozen=True)
class RadioView:
    ruid: bytes
    if_name: str
    band: str
    channel: int | None
    tx_power: int | None
    enabled: bool
    bsses: tuple[BssView, ...]
    uplinks: tuple[UplinkView, ...]

    def bss(self, if_name):
        return next((b for b in self.bsses if b.if_name == if_name), None)


@dataclass(frozen=True)
class DeviceView:
    serial: str
    node_id: str | None
    model: str | None
    firmware: str | None
    radios: tuple[RadioView, ...]

    def radio(self, ruid):
        return next((r for r in self.radios if r.ruid == ruid), None)


def device_view(decoded, parent_aps=frozenset()):
    """The pod as EasyMesh sees it, from schema-decoded OVSDB rows ({table: {uuid: row}}).
    ``parent_aps``: the VIFs that are the pod's GRE parent APs (spec 8.6), backhaul BSSes."""
    nodes = list(decoded.get("AWLAN_Node", {}).values())
    if len(nodes) != 1 or not nodes[0].get("serial_number"):
        raise EmosaError(Reason.NOT_READY, "pod identity absent")
    node = nodes[0]
    vifs = decoded.get("Wifi_VIF_State", {})
    clients = decoded.get("Wifi_Associated_Clients", {})
    radios = []
    for radio in decoded.get("Wifi_Radio_State", {}).values():
        if not radio.get("mac"):
            continue
        bsses, uplinks = [], []
        for uuid in radio.get("vif_states") or []:
            vif = vifs.get(uuid)
            if not vif or vif.get("enabled") is not True or not vif.get("mac"):
                continue
            if vif.get("mode") == "sta":
                parent = vif.get("parent")
                uplinks.append(
                    UplinkView(
                        vif["if_name"],
                        mac(vif["mac"]),
                        vif.get("ssid") or "",
                        mac(parent) if isinstance(parent, str) and parent else None,
                        vif.get("multi_ap") == "backhaul_sta" and vif.get("wds") is True,
                    )
                )
            elif vif.get("mode") == "ap":
                stations = sorted(
                    mac(clients[c]["mac"])
                    for c in vif.get("associated_clients") or []
                    if c in clients and clients[c].get("state") == "active"
                )
                bsses.append(
                    BssView(
                        vif["if_name"],
                        mac(vif["mac"]),
                        vif.get("ssid") or "",
                        role(vif.get("multi_ap"), vif["if_name"] in parent_aps),
                        tuple(stations),
                        vif,
                    )
                )
        channel, tx_power = radio.get("channel"), radio.get("tx_power")
        radios.append(
            RadioView(
                mac(radio["mac"]),
                radio.get("if_name") or "",
                radio.get("freq_band") or "",
                channel if type(channel) is int else None,
                tx_power if type(tx_power) is int else None,
                radio.get("enabled") is True,
                tuple(sorted(bsses, key=lambda b: b.if_name)),
                tuple(uplinks),
            )
        )
    return DeviceView(
        node["serial_number"],
        node.get("id"),
        node.get("model"),
        node.get("firmware_version"),
        tuple(sorted(radios, key=lambda r: r.ruid)),
    )


def backhaul_radios(decoded, stations, in_use=None):
    """(RUID, station MAC or None) of each of the pod's radios with one of ``stations``.

    A backhaul station counts whether it is enabled or not: a disabled one is still a
    backhaul STA radio of the pod (EasyMesh 6.1, 9.3), which a move to its band enables.
    Its MAC is known once owm reports its State. The station ``in_use`` comes first.
    """
    vifs = {
        u: r
        for u, r in decoded.get("Wifi_VIF_Config", {}).items()
        if r.get("if_name") in stations and r.get("mode") == "sta"
    }
    states = {
        r.get("if_name"): r
        for r in decoded.get("Wifi_VIF_State", {}).values()
        if r.get("mode") == "sta"
    }
    radio_states = {r.get("if_name"): r for r in decoded.get("Wifi_Radio_State", {}).values()}
    found = []
    for radio in decoded.get("Wifi_Radio_Config", {}).values():
        ruid = (radio_states.get(radio.get("if_name")) or {}).get("mac")
        if not ruid:
            continue
        for uuid in radio.get("vif_configs") or []:
            if uuid in vifs:
                name = vifs[uuid]["if_name"]
                station = (states.get(name) or {}).get("mac")
                found.append((name != in_use, mac(ruid), mac(station) if station else None))
    return tuple((ruid, station) for _, ruid, station in sorted(found, key=lambda f: f[:2]))


def backhaul(device, station):
    """The pod's EasyMesh backhaul through ``station``, or None.

    A connected backhaul STA on a mapped band with a known channel and a parent
    qualifies: a Multi-AP (4-address) STA, or a 3-address station under a GRE
    tunnel (spec 8.6) or on the bootstrap path. The medium is 802.11 either way;
    the reported STA keeps its own ``multi_ap`` flag, so a GRE child is reported
    honestly as a non-Multi-AP wireless backhaul, not as a Multi-AP bSTA. The
    caller decides that ``cm`` uses it.
    """
    for radio in device.radios:
        for uplink in radio.uplinks:
            if (
                uplink.if_name == station
                and uplink.parent is not None
                and radio.band in MEDIA
                and radio.channel is not None
            ):
                return BackhaulView(radio.ruid, radio.band, radio.channel, uplink)
    return None


# -- the view as EasyMesh payloads ----------------------------------------------


def _early_radio(radio, *, channel, max_bss, max_eirp):
    """One radio's AP Radio Basic Capabilities. Only the current channel is operable: EMOSA does
    not move the pod's radio (spec 9's channel rule for the second radio - it follows the station).
    Declared: no HT/VHT/HE/EHT capability claims."""
    if radio.band not in OPERATING_CLASSES:
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "radio band not mapped")
    opclass, channels = OPERATING_CLASSES[radio.band]
    basic = APRadioBasicCapabilities(
        radio.ruid,
        max_bss,
        (BasicOperatingClass(opclass, max_eirp, tuple(n for n in channels if n != channel)),),
    )
    return EarlyRadio(
        basic,
        APHTCapabilities(radio.ruid, 0),
        APRadioAdvancedCapabilities(radio.ruid, 0),
        False,
        False,
        False,
    )


def _capabilities(early_radios):
    """The device-wide AP Capability Report wrapping one or more radios' basic capabilities.
    Declared: WPA2-PSK with CCMP-128 only."""
    return EarlyCapabilities(
        tuple(early_radios),
        APCapability(0),
        Profile2APCapability(0, 0, 0, 0),
        AKMSuiteCapabilities((), (PSK,)),
        SupportedCipherSuites((CCMP128,)),
        True,
    )


def radio_capabilities(radio, *, channel, max_bss, max_eirp):
    """AP Capability Report contents for one radio operated on one channel."""
    return _capabilities(
        (_early_radio(radio, channel=channel, max_bss=max_bss, max_eirp=max_eirp),)
    )


def radios_capabilities(specs):
    """AP Capability Report for the several radios EMOSA binds (spec 9, the second radio of 8.6).
    ``specs`` is a tuple of ``(radio, channel, max_bss, max_eirp)``, the fronthaul radio first; each
    radio is operable only on its own current channel."""
    return _capabilities(
        _early_radio(radio, channel=channel, max_bss=max_bss, max_eirp=max_eirp)
        for radio, channel, max_bss, max_eirp in specs
    )


def bound_radios(device_radios, fronthaul, channel, *, additional, slots, slot_bands):
    """The radios EMOSA binds and reports (spec 9): the fronthaul on ``channel``, then each of the
    profile's ``additional`` radios found among ``device_radios`` by band, on its observed channel.
    Returns ``(radio, channel, max_bss)`` each. ``max_bss`` is split by ``slot_bands``
    ((if_name, band) pairs): the fronthaul takes one plus the slots that name no other radio, a
    second radio takes the slots that name its band (a parent's ``b-ap-<band>``), and at least one:
    an AP Radio Basic Capabilities TLV may not advertise none, and a plain child's radio (its
    station only) can carry an AP; EMOSA maps none of that radio's M2 set (no slot), as it leaves
    out the BSSes beyond any radio's slots (spec 3.4). A second radio the pod has not brought up
    yet (absent, or no channel) is left out and reported once it appears."""
    on_a_second = dict(slot_bands)
    fronthaul_bss = 1 + sum(1 for name, _, _ in slots if name not in on_a_second)
    bound = [(fronthaul, channel, fronthaul_bss)]
    for extra in additional:
        band = extra["band"]
        radio = next(
            (r for r in device_radios if r.band == band and r.ruid != fronthaul.ruid), None
        )
        if radio is None or radio.channel is None:
            continue
        on_band = sum(1 for name, _, _ in slots if on_a_second.get(name) == band)
        bound.append((radio, radio.channel, max(1, on_band)))
    return tuple(bound)


def inventory(device, radio, chipset=b"mac80211_hwsim"):
    return DeviceInventory(
        device.serial.encode()[:64],
        (device.firmware or "unknown").encode()[:64],
        b"OpenSync pod via EMOSA",
        (InventoryRadio(radio.ruid, chipset),),
    )


def neighbors_1905(agent_al, controller_al, peers):
    """The controller on the agent's Ethernet port, then each Wi-Fi backhaul neighbor on its
    local interface (spec 8.5; no IEEE 802.1 bridge between the two ends of a 4-address link)."""
    by_local = {}
    for local, al in peers:
        by_local.setdefault(local, set()).add(al)
    return (Neighbors1905(agent_al, (Neighbor(controller_al, False),)),) + tuple(
        Neighbors1905(local, tuple(Neighbor(al, False) for al in sorted(by_local[local])))
        for local in sorted(by_local)
    )


def topology(
    *,
    agent_al,
    controller_al,
    radio,
    channel,
    bsses,
    ages,
    uplink=None,
    associated_at=None,
    peers=(),
    backhaul_radios=(),
    extra_radios=(),
):
    """Topology Response contents: the agent, its BSSes and their stations.

    ``extra_radios`` ((radio, bsses, channel), ...) are the agent's further bound radios (spec 9):
    each is an Operational and Configured radio, with no BSS when it carries none of EMOSA's (a
    child's radio with only its station), as every radio of the capability report must be; its
    BSSes (a parent's b-ap-50 on 5 GHz) are interfaces of its band on its channel, and their
    stations associated clients, so the controller models them on their own radio.

    ``bsses`` is the subset of the radio's BSSes the agent represents; ``ages``
    maps a station MAC to seconds since association (as far as EMOSA knows);
    ``associated_at`` (station MAC -> monotonic time), when given, lets each
    Topology Response report the age as of the response.
    The agent's 1905 interface is its own Ethernet port (declared representation).
    ``uplink`` (a :class:`BackhaulView`) adds the pod's EasyMesh backhaul STA as a
    non-AP STA interface on its parent BSSID, bridged with the BSSes; the 1905
    neighbor stays on the Ethernet port, where EMOSA's frames actually go.
    ``peers`` adds the pod's 1905 neighbors on its Wi-Fi backhaul (spec 8.5), as (local
    interface MAC, neighbor AL MAC) pairs: its parent pod's agent on its backhaul station, a
    child pod's on its backhaul BSS. ``backhaul_radios`` ((RUID, station MAC or None), the one
    in use first) are the pod's radios with a backhaul station, for the Backhaul STA
    Capability Report.
    """
    # the fronthaul radio and each further bound radio (spec 9), each with its own band and channel
    # for its BSS interfaces and its own Operational/Configured radio
    radios = ((radio, bsses, channel),) + tuple(extra_radios)
    interfaces = (LocalInterface(agent_al, 1, b""),) + tuple(
        LocalInterface(b.bssid, MEDIA[r.band], b.bssid + bytes([AP_ROLE, 0x00, ch, 0x00]))
        for r, bs, ch in radios
        for b in bs
    )
    if uplink is not None:
        station = uplink.station
        interfaces += (
            LocalInterface(
                station.mac,
                MEDIA[uplink.band],
                station.parent + bytes([STA_ROLE, 0x00, uplink.channel, 0x00]),
            ),
        )
    return TopologyFacts(
        device=DeviceInformation(agent_al, interfaces),
        bridges=BridgingCapability((tuple(i.mac for i in interfaces),)),
        non1905=(),
        neighbors1905=neighbors_1905(agent_al, controller_al, peers),
        operational=APOperationalBss(
            tuple(
                OperationalRadio(
                    r.ruid, tuple(OperationalBss(b.bssid, b.ssid.encode()) for b in bs)
                )
                for r, bs, _ in radios
            )
        ),
        configuration=BssConfigurationReport(
            tuple(
                ConfiguredRadio(
                    r.ruid,
                    tuple(ConfiguredBss(b.bssid, b.report_flags, b.ssid.encode()) for b in bs),
                )
                for r, bs, _ in radios
            )
        ),
        clients=AssociatedClients(
            tuple(
                BssClients(
                    b.bssid,
                    tuple(AssociatedClient(m, min(65535, int(ages[m]))) for m in b.stations),
                )
                for _, bs, _ in radios
                for b in bs
            )
        ),
        inventory_complete=True,
        powered_off_interfaces_absent=True,
        l2_neighbor_records_absent=True,
        mld_backhaul_vbss_tid_policy_absent=True,
        backhaul_stations=() if uplink is None else ((uplink.ruid, uplink.station.mac),),
        backhaul_radios=tuple(backhaul_radios),
        associated_at=tuple(sorted((associated_at or {}).items())),
    )
