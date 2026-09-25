"""The OpenSync OVSDB model as the EasyMesh / IEEE 1905.1 model: one pure translation.

EMOSA's virtual agent tells an EasyMesh controller what an OpenSync pod is and
does. Everything it says comes from the pod's own State tables, through two pure
steps (no I/O, no clock):

1. :func:`device_view`: decoded OVSDB rows -> :class:`DeviceView`, a small
   EasyMesh-shaped model of the pod: device, radios, BSSes, stations.
2. :func:`radio_capabilities`, :func:`inventory` and :func:`topology`:
   a view -> the EasyMesh payloads the agent sends (AP Radio Basic
   Capabilities, Device Inventory, Topology Response contents).

The mapping (see doc/architecture/opensync-easymesh-mapping.md):

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
OPERATING_CLASSES = {"2.4G": (81, tuple(range(1, 14)))}
IEEE_802_11N_24 = 0x0103  # 1905.1 media type of an 802.11n 2.4 GHz interface
MEDIA = {"2.4G": IEEE_802_11N_24, "5G": 0x0104}  # 802.11n per band (no HT claims beyond)
AP_ROLE, STA_ROLE = 0x00, 0x40  # 1905.1 802.11 media-specific role: AP, non-AP STA


def mac(text):
    value = bytes.fromhex(text.replace(":", ""))
    if len(value) != 6:
        raise EmosaError(Reason.INVALID_INPUT, "not a MAC address")
    return value


def role(multi_ap):
    """The EasyMesh role of an AP VIF from its OpenSync ``multi_ap`` value."""
    return BACKHAUL if multi_ap == "backhaul_bss" else FRONTHAUL


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


def device_view(decoded):
    """The pod as EasyMesh sees it, from schema-decoded OVSDB rows ({table: {uuid: row}})."""
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
                        role(vif.get("multi_ap")),
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


def backhaul(device, station):
    """The pod's EasyMesh backhaul through ``station``, or None.

    Only a connected Multi-AP backhaul STA on a mapped band with a known
    channel and parent qualifies; the caller decides that ``cm`` uses it.
    """
    for radio in device.radios:
        for uplink in radio.uplinks:
            if (
                uplink.if_name == station
                and uplink.multi_ap
                and uplink.parent is not None
                and radio.band in MEDIA
                and radio.channel is not None
            ):
                return BackhaulView(radio.ruid, radio.band, radio.channel, uplink)
    return None


# -- the view as EasyMesh payloads ----------------------------------------------


def radio_capabilities(radio, *, channel, max_bss, max_eirp):
    """AP Capability Report contents for one radio operated on one channel.

    Only the current channel is operable: EMOSA does not move the pod's radio.
    Declared: no HT/VHT/HE/EHT capability claims, WPA2-PSK with CCMP-128 only.
    """
    if radio.band not in OPERATING_CLASSES:
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "radio band not mapped")
    opclass, channels = OPERATING_CLASSES[radio.band]
    basic = APRadioBasicCapabilities(
        radio.ruid,
        max_bss,
        (BasicOperatingClass(opclass, max_eirp, tuple(n for n in channels if n != channel)),),
    )
    return EarlyCapabilities(
        (
            EarlyRadio(
                basic,
                APHTCapabilities(radio.ruid, 0),
                APRadioAdvancedCapabilities(radio.ruid, 0),
                False,
                False,
                False,
            ),
        ),
        APCapability(0),
        Profile2APCapability(0, 0, 0, 0),
        AKMSuiteCapabilities((), (PSK,)),
        SupportedCipherSuites((CCMP128,)),
        True,
    )


def inventory(device, radio, chipset=b"mac80211_hwsim"):
    return DeviceInventory(
        device.serial.encode()[:64],
        (device.firmware or "unknown").encode()[:64],
        b"OpenSync pod via EMOSA",
        (InventoryRadio(radio.ruid, chipset),),
    )


def topology(*, agent_al, controller_al, radio, channel, bsses, ages, uplink=None):
    """Topology Response contents: the agent, its BSSes and their stations.

    ``bsses`` is the subset of the radio's BSSes the agent represents; ``ages``
    maps a station MAC to seconds since association (as far as EMOSA knows).
    The agent's 1905 interface is its own Ethernet port (declared representation).
    ``uplink`` (a :class:`BackhaulView`) adds the pod's EasyMesh backhaul STA as a
    non-AP STA interface on its parent BSSID, bridged with the BSSes; the 1905
    neighbor stays on the Ethernet port, where EMOSA's frames actually go.
    """
    interfaces = (LocalInterface(agent_al, 1, b""),) + tuple(
        LocalInterface(b.bssid, IEEE_802_11N_24, b.bssid + bytes([AP_ROLE, 0x00, channel, 0x00]))
        for b in bsses
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
        neighbors1905=(Neighbors1905(agent_al, (Neighbor(controller_al, False),)),),
        operational=APOperationalBss(
            (
                OperationalRadio(
                    radio.ruid, tuple(OperationalBss(b.bssid, b.ssid.encode()) for b in bsses)
                ),
            )
        ),
        configuration=BssConfigurationReport(
            (
                ConfiguredRadio(
                    radio.ruid,
                    tuple(ConfiguredBss(b.bssid, b.report_flags, b.ssid.encode()) for b in bsses),
                ),
            )
        ),
        clients=AssociatedClients(
            tuple(
                BssClients(
                    b.bssid,
                    tuple(AssociatedClient(m, min(65535, int(ages[m]))) for m in b.stations),
                )
                for b in bsses
            )
        ),
        inventory_complete=True,
        powered_off_interfaces_absent=True,
        l2_neighbor_records_absent=True,
        mld_backhaul_vbss_tid_policy_absent=True,
        backhaul_stations=() if uplink is None else ((uplink.ruid, uplink.station.mac),),
    )
