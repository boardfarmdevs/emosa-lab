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
Wifi_VIF_State mode=sta (backhaul STA)     the pod's Wi-Fi uplink: kept in the
                                           view, not yet reported (declared
                                           representation: Ethernet to EMOSA)
=========================================  ======================================

Southbound, the controller's M2 set becomes VIF rows (``pod_profile``); the
pod's managers apply them, and only this view confirms what they applied.
"""

from dataclasses import dataclass, field, replace

from emosa.easymesh_payloads import (
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
)
from emosa.errors import EmosaError, Reason
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
                uplinks.append(UplinkView(vif["if_name"], mac(vif["mac"]), vif.get("ssid") or ""))
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


# -- the view as EasyMesh payloads ----------------------------------------------


def radio_capabilities(template, radio, *, channel, max_bss, max_eirp):
    """AP Capability Report contents for one radio operated on one channel.

    Only the current channel is operable: EMOSA does not move the pod's radio.
    """
    if radio.band not in OPERATING_CLASSES:
        raise EmosaError(Reason.UNSUPPORTED_OPERATION, "radio band not mapped")
    opclass, channels = OPERATING_CLASSES[radio.band]
    basic = APRadioBasicCapabilities(
        radio.ruid,
        max_bss,
        (BasicOperatingClass(opclass, max_eirp, tuple(n for n in channels if n != channel)),),
    )
    caps = replace(
        template.radios[0],
        basic=basic,
        ht=APHTCapabilities(radio.ruid, 0),
        advanced=APRadioAdvancedCapabilities(radio.ruid, 0),
    )
    return replace(template, radios=(caps,), profile2=Profile2APCapability(0, 0, 0, 0))


def inventory(device, radio, chipset=b"mac80211_hwsim"):
    return DeviceInventory(
        device.serial.encode()[:64],
        (device.firmware or "unknown").encode()[:64],
        b"OpenSync pod via EMOSA",
        (InventoryRadio(radio.ruid, chipset),),
    )


def topology(template, *, agent_al, controller_al, radio, channel, bsses, ages):
    """Topology Response contents: the agent, its BSSes and their stations.

    ``bsses`` is the subset of the radio's BSSes the agent represents; ``ages``
    maps a station MAC to seconds since association (as far as EMOSA knows).
    The agent's 1905 interface is its own Ethernet port (declared representation).
    """
    interfaces = (LocalInterface(agent_al, 1, b""),) + tuple(
        LocalInterface(b.bssid, IEEE_802_11N_24, b.bssid + bytes([0x00, 0x00, channel, 0x00]))
        for b in bsses
    )
    return replace(
        template,
        device=DeviceInformation(agent_al, interfaces),
        bridges=BridgingCapability((tuple(i.mac for i in interfaces),)),
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
    )
