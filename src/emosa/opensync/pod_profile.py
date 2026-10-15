# SPDX-License-Identifier: Apache-2.0
"""OpenSync 6.6 pods: the VIF encoding and the guarded writes EMOSA makes.

The pod layout (VIF names, rows, band, channel, extra slots) is data: a pod
profile (``emosa.opensync.profiles``, default ``opensync-lab-hwsim-6.6.1-v1``).
The encoding below is written from the pod's own managers, not from the
synthetic simulator:

- ``ow_ovsdb.c`` reports the RSN PSK AKM as ``wpa_key_mgmt=["wpa-psk"]``; RSN
  (WPA2) versus WPA is carried by ``rsn_pairwise_ccmp`` / ``wpa_pairwise_*``.
- ``osw_drv_target.c`` names PSK slots ``key`` (id 0) and ``key-N``. The lab's
  NOC historically wrote ``key--1``; this profile replaces whatever single slot
  it finds with ``key``.
- ``tx_chainmask`` is never written: on hwsim osw confsync then never settles
  and aborts ``owm`` after 180 s (opensync-lab, 23 September).

One backend binds one fronthaul AP VIF (by name) on the radio of one band of one
pod serial, and changes only that VIF's SSID and PSK, guarded atomically against
the observed graph.

Multi-BSS (opt-in, ``multi_bss=True``): one M2 set may configure several BSSes of
the radio. They map onto the VIFs the pod platform itself creates per radio
(opensync-platform-cfg80211 ``52_owm_prep.sh``: index 1 ``b-ap``, 2 ``home-ap``,
4 ``svc-d-ap``, 5 ``svc-e-ap``, 6 ``fh``). The first fronthaul BSS is the bound
``home-ap-24``; further fronthaul BSSes take ``svc-d-ap-24``, ``svc-e-ap-24``,
``fh-24`` in that order, a backhaul BSS takes ``b-ap-24`` (``multi_ap=backhaul_bss``).
One transaction makes the radio's managed VIFs exactly the set received: slot
VIFs no longer in the set are removed. ``onboard-ap`` and ``cp`` are never used.

Cold start: a restarted pod rebuilds its database from its bootstrap, which has
no fronthaul (the cloud normally creates it). When the bound VIF is absent, the
controller's M2 creates it: one guarded transaction inserts the VIF with the
received SSID/PSK, references it from the radio, sets the radio's declared
channel and adds its Inet row, as the lab NOC's ``pod_step`` does for its own
pods. Nothing is created before an authenticated M2 asks for it.
"""

import hashlib
import ipaddress
import json

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import OpenSyncBackend, check_results, guard, where_uuid
from emosa.opensync.profiles import load as load_profile
from emosa.opensync.radio_scope import psk, wpa2_psk
from emosa.opensync.uplink import start_instance
from emosa.wire.operation_bridge import ScopeContext

MODE = "opensync-6.6-hwsim"
PROVENANCE = "opensync-owm:Wifi_VIF_State"
VIF_GUARDS = (
    "if_name",
    "mode",
    "enabled",
    "ssid",
    "wpa",
    "wpa_key_mgmt",
    "wpa_psks",
    "security",
    "rsn_pairwise_ccmp",
    "wpa_pairwise_tkip",
    "wpa_pairwise_ccmp",
)
# a legacy pod's (spec 3.10): its WPA2-PSK is the security map
LEGACY_VIF_GUARDS = ("if_name", "mode", "enabled", "ssid", "security")
# spec 3.10: a legacy pod's wm restarts its authenticator (nas and eapd) in full only when an AP's
# security changes in more than its ssid and key (its oftag, say); a new ssid or key alone is a
# fast reload. EMOSA's write that makes an AP, changes its ssid or key, or puts it into its bridge
# gives the oftag this suffix, and once the pod's State shows that write (and the AP is in its
# bridge) the oftag is restored: two full reloads, the last with the AP's port there.
AUTH_RELOAD = "~reload"
# spec 3.10: a legacy pod's wm and authenticator (eapd) hear an AP's stations on its bridge, whose
# members are the bridge's Open vSwitch ports; the pod never adds an AP there (its cloud writes the
# Port), and Wifi_VIF_State.bridge shows the bridge regardless (from the pod's NVRAM). An AP of
# EMOSA's with a bridge and no Port gets one, its oftag marked so the authenticator restarts after.
PORT_GUARDS = ("if_name", "enabled", "bridge", "security")
# What shows whether such a port carries its stations, read only (spec 3.10): the bridge's fail
# mode (a secure bridge forwards only what its OpenFlow rules allow) and OpenSync's rules for it,
# and each port's Interface as Open vSwitch opened it (its ofport, or the error)
BRIDGE_MONITOR = {
    "Bridge": ["name", "ports", "fail_mode", "controller", "other_config", "status"],
    "Port": ["name", "interfaces", "tag", "trunks", "vlan_mode"],
    "Interface": ["name", "type", "ofport", "error", "link_state", "admin_state"],
    "Openflow_Config": ["bridge", "table", "priority", "rule", "action", "token"],
    "Openflow_State": ["bridge", "openflow_config", "success", "token"],
}
# the role an M2's Multi-AP extension gives a BSS, as OpenSync's multi_ap (spec 3.4): the pod's
# AP takes it as a Multi-AP agent's AP does (hostapd multi_ap 2 or 1)
MULTI_AP = {"fronthaul": "fronthaul_bss", "backhaul": "backhaul_bss"}
# A GRE parent AP's Inet columns beyond a slot's (spec 8.6): its underlay address and DHCP server
PARENT_INET = ("ip_assign_scheme", "inet_addr", "dhcpd")


def parent_rendered(row):
    """An Inet row (raw RFC 7047 values) as a GRE parent AP has it: a static address or a DHCP
    server, which a plain slot's row never has."""
    address, dhcpd = row.get("inet_addr"), row.get("dhcpd")
    return (
        row.get("ip_assign_scheme") == "static"
        or (isinstance(address, str) and address != "")
        or (isinstance(dhcpd, list) and len(dhcpd) == 2 and dhcpd[0] == "map" and bool(dhcpd[1]))
    )


def slots_on(profile, band):
    """The profile's extra slots whose VIF is on the radio of ``band`` (spec 9): a slot names its
    radio's band in ``slot_bands``, else it is on the fronthaul's radio."""
    bands = dict(profile.slot_bands)
    return tuple(s for s in profile.extra_slots if bands.get(s[0], profile.band) == band)


class PodBackend(OpenSyncBackend):
    """One fronthaul AP VIF on one real OpenSync 6.6 pod, bound by serial."""

    mode = MODE

    def __init__(
        self,
        pod_id,
        session,
        vault,
        *,
        serial,
        profile=None,
        multi_bss=False,
        underlay=None,
    ):
        self.profile = profile or load_profile()
        # the pod's 169.254.N.0/24 as a GRE parent (spec 8.6), from the fleet registry
        self.underlay = underlay
        super().__init__(
            pod_id,
            session,
            vault,
            if_name=self.profile.fronthaul_if,
            radio_name="",
            expected_serial=serial,
        )
        # multi_bss: the radio takes an M2 set (spec 3.4); its slots, the further BSSes it maps.
        # One M2 configures one radio (spec 9): this scope owns the slots on the fronthaul's radio;
        # a slot on another of the profile's radios is that radio's scope's (RadioBackend).
        self.multi_bss = multi_bss
        self.band, self.channel = self.profile.band, self.profile.channel
        self.ht_mode = self.profile.ht_mode
        # every slot EMOSA manages on the pod, whichever radio's scope writes it (the report)
        self.pod_slots = self.profile.extra_slots if multi_bss else ()
        self.slots = slots_on(self.profile, self.band) if multi_bss else ()
        self.max_bss = 1 + len(self.slots)
        self.state_provenance = PROVENANCE
        self.anchor = None
        self.identity = None  # radio MAC, BSSID (None before the VIF exists), channel, radio
        self.auth_reloads = 0  # spec 3.10: the oftags auth_reload restored
        self.ports_added = 0  # spec 3.10: the APs bridge_ports put into their bridge

    @property
    def mapping(self):
        """The mapping this scope plans under (its plan's "mapping"): the pod's profile."""
        return self.profile.id

    @property
    def parent_aps(self):
        """The slot VIFs a backhaul BSS takes as the pod's GRE parent AP (spec 8.6), on any of the
        pod's radios (a parent's b-ap-50 is on its 5 GHz radio, spec 9)."""
        if self.profile.backhaul_mode != "gre-parent":
            return frozenset()
        return frozenset(name for name, role, _ in self.pod_slots if role == "backhaul")

    @property
    def vif_guards(self):
        """The guarded AP VIF fields of the pod's generation (spec 3.2, 3.10)."""
        return LEGACY_VIF_GUARDS if self.profile.legacy else VIF_GUARDS

    def _secured(self, row, key, config=None):
        """The row with WPA2-PSK and the key in the pod's encoding: the single wpa_psks slot, or on
        a legacy pod the security map, the row's own entries then encryption WPA-PSK and the key
        (spec 3.10), its oftag with AUTH_RELOAD when the write makes the AP or changes its ssid or
        key (``config``, the AP's decoded Config row, None when it is made)."""
        if not self.profile.legacy:
            return {**row, "wpa_psks": ["map", [["key", key]]]}
        security = row.get("security") or ["map", []]
        entries = {**dict(security[1]), "encryption": "WPA-PSK", "key": key}
        if config is None or config.get("ssid") != row.get("ssid") or psk(config) != key:
            entries["oftag"] = entries.get("oftag", "") + AUTH_RELOAD
        return {**row, "security": ["map", sorted([k, v] for k, v in entries.items())]}

    def _unbridged(self, decoded, config):
        """spec 3.10: the uuid of the bridge a legacy AP of EMOSA's (the fronthaul, a slot) names
        when the AP is on and no Port of its name is anywhere (the pod never adds one); None when
        it has a Port, no bridge, or the bridge is not the pod's one Bridge of that name."""
        name, bridge = config.get("if_name"), config.get("bridge")
        if (
            config.get("mode") != "ap"
            or config.get("enabled") is not True
            or name not in {self.profile.fronthaul_if, *(s for s, _, _ in self.pod_slots)}
            or not isinstance(bridge, str)
            or not bridge
            or any(r.get("name") == name for r in decoded.get("Port", {}).values())
        ):
            return None
        found = [u for u, r in decoded.get("Bridge", {}).items() if r.get("name") == bridge]
        return found[0] if len(found) == 1 else None

    @staticmethod
    def _decoded(raw):
        schema = raw["schema"]
        return {
            t: {u: schema.row(t, r) for u, r in rows.items()} for t, rows in raw["tables"].items()
        }

    async def bridge_ports(self):
        """spec 3.10: each legacy AP of EMOSA's that the pod has applied (its State) and that is
        not in its bridge gets its Interface and Port into the bridge, guarded (the bridge's
        ports, no Port of its name, the AP's row), and its oftag marked (AUTH_RELOAD) unless it
        is already: the authenticator restarts in full with the port there, and auth_reload
        restores the oftag after. The names put into their bridge."""
        if not self.profile.legacy:
            return []
        raw = await self.session.snapshot()
        tables, decoded = raw["tables"], self._decoded(raw)
        states = {r.get("if_name") for r in decoded.get("Wifi_VIF_State", {}).values()}
        bridges, ops, counts, names = {}, [], [], []
        for uuid, config in sorted(decoded.get("Wifi_VIF_Config", {}).items()):
            bridge = self._unbridged(decoded, config)
            name = config.get("if_name")
            if bridge is None or name not in states:
                continue
            if bridge not in bridges:  # each bridge's ports guarded once, before any change
                row = tables["Bridge"][bridge]
                bridges[bridge] = guard(
                    "Bridge", bridge, row, [c for c in ("name", "ports") if c in row]
                )
            row, tag = tables["Wifi_VIF_Config"][uuid], f"ap{len(names)}"
            ops += [
                guard("Wifi_VIF_Config", uuid, row, [c for c in PORT_GUARDS if c in row]),
                {
                    "op": "wait",
                    "table": "Port",
                    "where": [["name", "==", name]],
                    "columns": ["name"],
                    "until": "==",
                    "rows": [],
                    "timeout": 0,
                },
                {
                    "op": "insert",
                    "table": "Interface",
                    "uuid-name": f"{tag}_if",
                    "row": {"name": name},
                },
                {
                    "op": "insert",
                    "table": "Port",
                    "uuid-name": f"{tag}_port",
                    "row": {"name": name, "interfaces": ["set", [["named-uuid", f"{tag}_if"]]]},
                },
                {
                    "op": "mutate",
                    "table": "Bridge",
                    "where": where_uuid(bridge),
                    "mutations": [["ports", "insert", ["set", [["named-uuid", f"{tag}_port"]]]]],
                },
            ]
            counts += [None, None, None, None, 1]
            oftag = (config.get("security") or {}).get("oftag", "")
            if not oftag.endswith(AUTH_RELOAD):
                ops.append(
                    {
                        "op": "mutate",
                        "table": "Wifi_VIF_Config",
                        "where": where_uuid(uuid),
                        "mutations": [
                            ["security", "delete", ["set", ["oftag"]]],
                            ["security", "insert", ["map", [["oftag", oftag + AUTH_RELOAD]]]],
                        ],
                    }
                )
                counts.append(1)
            names.append(name)
        if not names:
            return []
        self.write_count += 1
        guards = list(bridges.values())
        check_results(await self.session.transact(guards + ops), [None] * len(guards) + counts)
        self.ports_added += len(names)
        return names

    async def auth_reload(self):
        """spec 3.10: each legacy AP whose oftag carries AUTH_RELOAD gets its oftag back, guarded,
        once the pod's State shows that write (its ssid, key and oftag) and the AP is in its bridge
        (bridge_ports): the second full reload of the pod's authenticator, after wm has taken the
        first. The names restored."""
        if not self.profile.legacy:
            return []
        raw = await self.session.snapshot()
        tables, decoded = raw["tables"], self._decoded(raw)
        states = {s.get("if_name"): s for s in decoded.get("Wifi_VIF_State", {}).values()}
        ops, names = [], []
        for uuid, row in sorted(tables.get("Wifi_VIF_Config", {}).items()):
            config = decoded["Wifi_VIF_Config"][uuid]
            oftag = (config.get("security") or {}).get("oftag", "")
            if config.get("mode") != "ap" or not oftag.endswith(AUTH_RELOAD):
                continue
            state = states.get(config.get("if_name")) or {}
            if (
                (state.get("security") or {}).get("oftag") != oftag
                or state.get("ssid") != config.get("ssid")
                or psk(state) != psk(config)
            ):
                continue  # wm has not taken the write yet
            if self._unbridged(decoded, config) is not None:
                continue  # its port first (bridge_ports), then the restore's full reload
            mutations = [["security", "delete", ["set", ["oftag"]]]]
            if oftag != AUTH_RELOAD:
                mutations.append(
                    ["security", "insert", ["map", [["oftag", oftag[: -len(AUTH_RELOAD)]]]]]
                )
            ops += [
                guard("Wifi_VIF_Config", uuid, row, ["if_name", "ssid", "security"]),
                {
                    "op": "mutate",
                    "table": "Wifi_VIF_Config",
                    "where": where_uuid(uuid),
                    "mutations": mutations,
                },
            ]
            names.append(config["if_name"])
        if not ops:
            return []
        self.write_count += 1
        check_results(await self.session.transact(ops), [None, 1] * len(names))
        self.auth_reloads += len(names)
        return names

    def _role_row(self, row, multi_ap):
        """The row with its role in multi_ap (spec 3.4); a legacy pod has none (spec 3.10)."""
        if self.profile.legacy:
            return {k: v for k, v in row.items() if k != "multi_ap"}
        return {**row, "multi_ap": multi_ap}

    def _values(self, row):
        key = psk(row)
        return {
            "ssid": row.get("ssid"),
            "enabled": row.get("enabled"),
            "mode": row.get("mode"),
            "security_mode": "wpa2-psk" if wpa2_psk(row) else None,
            "credential_fingerprint": self.vault.fingerprint(key) if key is not None else None,
        }

    def _radio(self, snapshot):
        """The bound pod and the one radio of this scope's band: (decoded rows, the radio's
        Wifi_Radio_Config uuid and row, its Wifi_Radio_State rows)."""
        schema, tables = snapshot["schema"], snapshot["tables"]
        decoded = {
            t: {u: schema.row(t, row) for u, row in rows.items()} for t, rows in tables.items()
        }
        nodes = list(decoded.get("AWLAN_Node", {}).values())
        if len(nodes) != 1 or nodes[0].get("serial_number") != self.expected_serial:
            raise EmosaError(Reason.NOT_READY, "pod identity absent or not the bound serial")
        radios = [
            (u, r)
            for u, r in decoded.get("Wifi_Radio_Config", {}).items()
            if r.get("freq_band") == self.band
        ]
        if len(radios) != 1:
            raise EmosaError(Reason.NOT_READY, "no single radio of the bound band")
        radio_uuid, radio = radios[0]
        self.radio_name = radio.get("if_name")
        radio_states = [
            r
            for r in decoded.get("Wifi_Radio_State", {}).values()
            if r.get("radio_config") == radio_uuid
        ]
        return decoded, radio_uuid, radio, radio_states

    def _binding(self, snapshot):
        decoded, radio_uuid, radio, radio_states = self._radio(snapshot)
        configs = [
            (u, r)
            for u, r in decoded.get("Wifi_VIF_Config", {}).items()
            if r.get("if_name") == self.if_name
        ]
        if len(configs) > 1:
            raise EmosaError(Reason.NOT_READY, "bound fronthaul VIF ambiguous")
        vif_uuid, config = configs[0] if configs else (None, {})
        if vif_uuid is not None and vif_uuid not in (radio.get("vif_configs") or []):
            raise EmosaError(Reason.NOT_READY, "bound VIF is not on the bound band's radio")
        states = [
            (u, r)
            for u, r in decoded.get("Wifi_VIF_State", {}).items()
            if vif_uuid is not None
            and r.get("vif_config") == vif_uuid
            and r.get("if_name") == self.if_name
        ]
        state = states[0][1] if len(states) == 1 else {}
        if (
            len(radio_states) != 1
            or not states
            or states[0][0] not in (radio_states[0].get("vif_states") or [])
        ):
            state = {}
        self.identity = (
            {
                "radio_mac": radio_states[0].get("mac"),
                "bssid": state.get("mac") if state else None,
                "channel": radio_states[0].get("channel"),
                "radio_if_name": self.radio_name,
            }
            if len(radio_states) == 1 and radio_states[0].get("mac")
            else None
        )
        return vif_uuid, radio_uuid, config, state, decoded

    async def snapshot(self):
        """The bound BSS (and, multi-BSS, the managed extra BSSes) as the pod has them.

        Ready to plan when the pod and its radio are bound, even with no VIF yet;
        observed application still needs the VIF's own State (``observed.fresh``).
        """
        self.identity = None  # set again only by a successful _binding of a fresh read
        try:
            raw = await self.session.snapshot()
            vif_uuid, radio_uuid, config, state, decoded = self._binding(raw)
            configured, observed = self._values(config), self._values(state)
            # a multi-BSS radio reports its further BSSes, none when its profile has no slot,
            # so that an M2 set's intent (its additional BSSes fitted to the slots) matches
            # (the C agent's PodBackend the same)
            if self.multi_bss:
                extras = self._extras(decoded, radio_uuid)
                configured["additional"] = self._additional(e["config"] for e in extras.values())
                observed["additional"] = self._additional(
                    e["state"] for e in extras.values() if e["state"]
                )
            fresh = raw["ready"] and bool(state)
            ready = fresh or bool(raw["ready"] and self.identity and vif_uuid is None)
            self.last = Snapshot(
                configured,
                Observation(
                    self.pod_id,
                    self.bss_id,
                    observed,
                    "ovsdb",
                    self.mode,
                    raw["generation"],
                    utc_now(),
                    fresh,
                    self.state_provenance,
                    revision=raw["revision"],
                ),
                ready,
                raw["generation"],
                raw["schema"].fingerprint,
                self._instance(decoded),
            )
        except (EmosaError, ConnectionError, TimeoutError):
            if self.last is None:
                raise
            self.last.ready = False
            self.last.observed.fresh = False
        return self.last

    @staticmethod
    def _instance(decoded):
        """Which start of the pod's OpenSync (its radio rows), or None before it has them."""
        try:
            return start_instance(decoded)
        except EmosaError:
            return None

    def role(self, row):
        """A slot row's role: a Multi-AP backhaul BSS, or the GRE parent AP. A parent AP's slot is
        the backhaul only as a parent renders it, not a Multi-AP BSS (multi_ap none, or unset): a
        Multi-AP BSS left in it (a pod promoted to GRE parent while onboarded, finding A) keeps its
        own value, so that the intent is not satisfied and the slot is converted."""
        multi_ap = row.get("multi_ap")
        if row.get("if_name") in self.parent_aps:
            return "backhaul" if multi_ap in (None, "none") else multi_ap
        return "backhaul" if multi_ap == "backhaul_bss" else "fronthaul"

    def _additional(self, rows):
        """Sorted [role, ssid, key fingerprint] of extra BSS rows (Config or State)."""
        values = []
        for row in rows:
            key = psk(row) if wpa2_psk(row) else None
            usable = row.get("mode") == "ap" and row.get("enabled") is True
            values.append(
                [
                    self.role(row),
                    row.get("ssid"),
                    self.vault.fingerprint(key) if key is not None and usable else None,
                ]
            )
        return sorted(values, key=json.dumps)

    def _extras(self, decoded, radio_uuid):
        """This scope's slot VIFs present on its radio: if_name -> uuid, Config row, State row."""
        names = {name for name, _, _ in self.slots}
        on_radio = set(decoded["Wifi_Radio_Config"][radio_uuid].get("vif_configs") or [])
        found = {}
        for uuid, row in decoded.get("Wifi_VIF_Config", {}).items():
            name = row.get("if_name")
            if name not in names:
                continue
            if name in found or uuid not in on_radio:
                raise EmosaError(Reason.NOT_READY, "slot VIF ambiguous or on another radio")
            states = [
                r
                for r in decoded.get("Wifi_VIF_State", {}).values()
                if r.get("vif_config") == uuid and r.get("if_name") == name
            ]
            found[name] = {
                "uuid": uuid,
                "config": row,
                "state": states[0] if len(states) == 1 else {},
            }
        return found

    def _parent_vif(self):
        """The GRE parent AP's row (spec 8.6), as the OpenSync cloud writes a parent's backhaul
        AP: no Multi-AP, no bridge, no bridging between its stations; hidden unless the
        profile says otherwise."""
        return self._role_row(
            {
                **self.profile.backhaul_row,
                "ssid_broadcast": "disabled",
                **(self.profile.gre_parent_vif or {}),
                "bridge": "",
                "ap_bridge": False,
            },
            "none",
        )

    def _parent_inet(self, name):
        """The GRE parent AP's Inet row (spec 8.6): the underlay's .1, its DHCP range .10 to
        .250; the profile's gre_parent.inet over the other columns."""
        net = ipaddress.ip_network(self.underlay)
        start, stop = net.network_address + 10, net.network_address + 250
        return {
            "if_type": "vif",
            "enabled": True,
            "network": True,
            "NAT": False,
            "mtu": 1600,
            **(self.profile.gre_parent_inet or {}),
            "if_name": name,
            "ip_assign_scheme": "static",
            "inet_addr": str(net.network_address + 1),
            "netmask": str(net.netmask),
            "dhcpd": ["map", [["start", str(start)], ["stop", str(stop)], ["lease_time", "12h"]]],
        }

    def _assign(self, intent):
        """Slot for each additional BSS of the intent, by role in slot order."""
        free = {
            role: [n for n, r, _ in self.slots if r == role] for role in ("fronthaul", "backhaul")
        }
        assigned = {}
        for bss in intent.additional or ():
            if not free[bss["role"]]:
                raise EmosaError(
                    Reason.UNSUPPORTED_OPERATION, "more BSSes of a role than the radio maps"
                )
            assigned[free[bss["role"]].pop(0)] = bss
        return assigned

    async def context(self):
        raw = await self.session.snapshot()
        _, radio_uuid, _, _, decoded = self._binding(raw)
        if not raw["ready"] or not self.identity:
            raise EmosaError(Reason.NOT_READY, "bound radio State unavailable")
        node_uuid = next(iter(decoded["AWLAN_Node"]))
        # The BSS may be created by an operation (cold start), so the anchor pins
        # the pod and its radio; VIF fields are guarded in each transaction.
        anchor = ScopeContext(
            raw["generation"],
            raw["schema"].fingerprint,
            hashlib.sha256(
                json.dumps(
                    [self.profile.id, node_uuid, radio_uuid, self.identity["radio_mac"]],
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        )
        # No comparison with an earlier exchange's anchor: a pod whose OpenSync restarts
        # rebuilds its database with new row UUIDs and is the same pod (same serial and
        # radio). Within one exchange the WSC bridge compares every step with the anchor
        # it started from, so an M2 is never applied to a changed graph.
        self.anchor = anchor
        return anchor

    async def plan(self, intent):
        if intent.additional and not self.slots:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "this radio maps one BSS")
        intent.target(self.vault)
        if (intent.pod_id, intent.radio_id, intent.bss_id) != (
            self.pod_id,
            self.radio_id,
            self.bss_id,
        ):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound VIF")
        assigned = self._assign(intent)
        if not self.underlay and self.parent_aps & set(assigned):
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "a GRE parent AP needs the pod's underlay (spec 8.6)"
            )
        raw = await self.session.snapshot()
        vif_uuid, radio_uuid, config, state, decoded = self._binding(raw)
        if not raw["ready"] or not self.identity:
            raise EmosaError(Reason.NOT_READY, "complete current references/state required")
        if self.slots:
            self._extras(decoded, radio_uuid)
        common = {
            "mapping": self.profile.id,
            "radio_id": self.radio_id,
            "bss_id": self.bss_id,
            "ssid": intent.ssid,
            "secret_ref": intent.secret_ref,
            "shared_radio_actuation": vif_uuid is None or bool(self.slots),
            "additional_slots": {name: bss["role"] for name, bss in assigned.items()},
        }
        if vif_uuid is None:
            return {
                **common,
                "action": "create",
                "fields": ["Wifi_VIF_Config", "Wifi_Radio_Config.vif_configs/channel", "Inet"],
                "channel": self.channel,
                "guard": "pod serial, radio references, bound VIF absent",
            }
        if not state:
            raise EmosaError(Reason.NOT_READY, "bound VIF State unavailable")
        if config.get("mode") != "ap" or config.get("enabled") is not True or not wpa2_psk(config):
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "current VIF/security representation unqualified"
            )
        return {
            **common,
            "action": "update",
            "fields": ["ssid", "security"]
            if self.profile.legacy
            else ["ssid", "multi_ap", "wpa_psks"],
            "guard": "pod serial, radio/VIF references and VIF security fields",
        }

    def _node_guard(self, raw):
        return {
            "op": "wait",
            "table": "AWLAN_Node",
            "where": [],
            "columns": ["_uuid", "serial_number"],
            "until": "==",
            "rows": [
                {
                    "_uuid": ["uuid", next(iter(raw["tables"]["AWLAN_Node"]))],
                    "serial_number": self.expected_serial,
                }
            ],
            "timeout": 0,
        }

    async def _create(self, intent, raw, radio_uuid):
        """Cold-start transaction: the fronthaul VIF from the received M2."""
        radio = raw["tables"]["Wifi_Radio_Config"][radio_uuid]
        inet = await self.session.transact(
            [
                {
                    "op": "select",
                    "table": "Wifi_Inet_Config",
                    "where": [["if_name", "==", self.if_name]],
                    "columns": ["_uuid"],
                }
            ]
        )
        missing_inet = not (inet and inet[0].get("rows"))
        key = self.vault.resolve(intent.secret_ref)
        transaction = [
            self._node_guard(raw),
            guard("Wifi_Radio_Config", radio_uuid, radio, ["if_name", "vif_configs"]),
            {
                "op": "wait",
                "table": "Wifi_VIF_Config",
                "where": [["if_name", "==", self.if_name]],
                "columns": ["if_name"],
                "until": "==",
                "rows": [],
                "timeout": 0,
            },
            {
                "op": "insert",
                "table": "Wifi_VIF_Config",
                "uuid-name": "fh",
                "row": self._secured(
                    self._role_row(
                        {
                            **self.profile.fronthaul_vif,
                            "if_name": self.if_name,
                            "ssid": intent.ssid,
                        },
                        MULTI_AP["fronthaul"],
                    ),
                    key,
                ),
            },
            {
                "op": "mutate",
                "table": "Wifi_Radio_Config",
                "where": where_uuid(radio_uuid),
                "mutations": [["vif_configs", "insert", ["set", [["named-uuid", "fh"]]]]],
            },
            {
                "op": "update",
                "table": "Wifi_Radio_Config",
                "where": where_uuid(radio_uuid),
                "row": {"channel": self.channel, "ht_mode": self.ht_mode, "enabled": True},
            },
        ]
        counts = [None, None, None, None, 1, 1]
        if missing_inet:
            transaction += [
                {
                    "op": "wait",
                    "table": "Wifi_Inet_Config",
                    "where": [["if_name", "==", self.if_name]],
                    "columns": ["if_name"],
                    "until": "==",
                    "rows": [],
                    "timeout": 0,
                },
                {
                    "op": "insert",
                    "table": "Wifi_Inet_Config",
                    "row": {**self.profile.inet, "if_name": self.if_name},
                },
            ]
            counts += [None, None]
        return transaction, counts

    async def _extra_ops(self, intent, raw, radio_uuid):
        """Make the scope's slot VIFs on its radio exactly the intent's additional BSSes: insert
        and delete of its own VIFs in the radio's vif_configs, never a replace that would drop
        the pod's own rows there (its backhaul station, spec 9)."""
        decoded = {
            t: {u: raw["schema"].row(t, r) for u, r in rows.items()}
            for t, rows in raw["tables"].items()
        }
        extras = self._extras(decoded, radio_uuid)
        assigned = self._assign(intent)
        inet = await self.session.transact(
            [
                {
                    "op": "select",
                    "table": "Wifi_Inet_Config",
                    "where": [],
                    "columns": ["if_name", *PARENT_INET],
                }
            ]
        )
        inet_rows = {row.get("if_name"): row for row in (inet[0].get("rows", []) if inet else [])}
        inet_names = set(inet_rows)
        ops, counts = [], []
        for index, (name, role, radio_idx) in enumerate(self.slots):
            present = extras.get(name)
            if present:
                raw_row = raw["tables"]["Wifi_VIF_Config"][present["uuid"]]
                ops.append(
                    guard(
                        "Wifi_VIF_Config",
                        present["uuid"],
                        raw_row,
                        [c for c in (*VIF_GUARDS, "multi_ap") if c in raw_row],
                    )
                )
                counts.append(None)
            bss = assigned.get(name)
            if bss is None and present:
                ops += [
                    {
                        "op": "mutate",
                        "table": "Wifi_Radio_Config",
                        "where": where_uuid(radio_uuid),
                        "mutations": [
                            ["vif_configs", "delete", ["set", [["uuid", present["uuid"]]]]]
                        ],
                    },
                    {
                        "op": "delete",
                        "table": "Wifi_VIF_Config",
                        "where": where_uuid(present["uuid"]),
                    },
                ]
                counts += [1, 1]
                if name in inet_names:
                    ops.append(
                        {
                            "op": "delete",
                            "table": "Wifi_Inet_Config",
                            "where": [["if_name", "==", name]],
                        }
                    )
                    counts.append(None)
            elif bss is not None:
                if name in self.parent_aps:
                    base = self._parent_vif()
                else:
                    base = self._role_row(
                        self.profile.backhaul_row
                        if role == "backhaul"
                        else self.profile.fronthaul_vif,
                        MULTI_AP[role],
                    )
                key = self.vault.resolve(bss["secret_ref"])
                if present and self.profile.legacy:  # the whole security map, in the update
                    ops.append(
                        {
                            "op": "update",
                            "table": "Wifi_VIF_Config",
                            "where": where_uuid(present["uuid"]),
                            "row": self._secured(
                                {
                                    **{k: v for k, v in base.items() if k != "vif_radio_idx"},
                                    "ssid": bss["ssid"],
                                },
                                key,
                                present["config"],
                            ),
                        }
                    )
                    counts.append(1)
                elif present:
                    slots = sorted(present["config"].get("wpa_psks") or {})
                    ops += [
                        {
                            "op": "update",
                            "table": "Wifi_VIF_Config",
                            "where": where_uuid(present["uuid"]),
                            "row": {
                                **{k: v for k, v in base.items() if k != "vif_radio_idx"},
                                "ssid": bss["ssid"],
                            },
                        },
                        {
                            "op": "mutate",
                            "table": "Wifi_VIF_Config",
                            "where": where_uuid(present["uuid"]),
                            "mutations": [
                                ["wpa_psks", "delete", ["set", slots]],
                                ["wpa_psks", "insert", ["map", [["key", key]]]],
                            ],
                        },
                    ]
                    counts += [1, 1]
                else:
                    named = f"extra{index}"
                    ops += [
                        {
                            "op": "wait",
                            "table": "Wifi_VIF_Config",
                            "where": [["if_name", "==", name]],
                            "columns": ["if_name"],
                            "until": "==",
                            "rows": [],
                            "timeout": 0,
                        },
                        {
                            "op": "insert",
                            "table": "Wifi_VIF_Config",
                            "uuid-name": named,
                            "row": self._secured(
                                {
                                    **base,
                                    "if_name": name,
                                    "ssid": bss["ssid"],
                                    "vif_radio_idx": radio_idx,
                                },
                                key,
                            ),
                        },
                        {
                            "op": "mutate",
                            "table": "Wifi_Radio_Config",
                            "where": where_uuid(radio_uuid),
                            "mutations": [
                                ["vif_configs", "insert", ["set", [["named-uuid", named]]]]
                            ],
                        },
                    ]
                    counts += [None, None, 1]
                if name in self.parent_aps:
                    # the underlay's address and DHCP, in place of a Multi-AP slot's row
                    parent_inet = self._parent_inet(name)
                    if name in inet_names:
                        ops.append(
                            {
                                "op": "update",
                                "table": "Wifi_Inet_Config",
                                "where": [["if_name", "==", name]],
                                "row": {k: v for k, v in parent_inet.items() if k != "if_name"},
                            }
                        )
                    else:
                        ops.append(
                            {"op": "insert", "table": "Wifi_Inet_Config", "row": parent_inet}
                        )
                    counts.append(None)
                elif name not in inet_names:
                    ops.append(
                        {
                            "op": "insert",
                            "table": "Wifi_Inet_Config",
                            "row": {**self.profile.inet, "if_name": name},
                        }
                    )
                    counts.append(None)
                elif parent_rendered(inet_rows[name]):
                    # a former GRE parent AP's row (the pod's profile no longer a parent, spec
                    # 8.6): the profile's slot row again, the underlay's address and DHCP server
                    # cleared, so that no stray DHCP server is left on the backhaul BSS
                    ops.append(
                        {
                            "op": "update",
                            "table": "Wifi_Inet_Config",
                            "where": [["if_name", "==", name]],
                            "row": {
                                **{k: v for k, v in self.profile.inet.items() if k != "if_name"},
                                "inet_addr": ["set", []],
                                "netmask": ["set", []],
                                "dhcpd": ["map", []],
                            },
                        }
                    )
                    counts.append(None)
        return ops, counts

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        vif_uuid, radio_uuid, config, _, decoded = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        if vif_uuid is None:
            transaction, counts = await self._create(intent, raw, radio_uuid)
        else:
            vif = raw["tables"]["Wifi_VIF_Config"][vif_uuid]
            radio = raw["tables"]["Wifi_Radio_Config"][radio_uuid]
            transaction = [
                self._node_guard(raw),
                guard("Wifi_Radio_Config", radio_uuid, radio, ["if_name", "vif_configs"]),
                guard("Wifi_VIF_Config", vif_uuid, vif, list(self.vif_guards)),
            ]
            key = self.vault.resolve(intent.secret_ref)
            if self.profile.legacy:  # the profile's security map with the key, in one update
                security = self.profile.fronthaul_vif.get("security") or ["map", []]
                transaction.append(
                    {
                        "op": "update",
                        "table": "Wifi_VIF_Config",
                        "where": where_uuid(vif_uuid),
                        "row": self._secured(
                            {"ssid": intent.ssid, "security": security}, key, config
                        ),
                    }
                )
                counts = [None, None, None, 1]
            else:
                slots = sorted(config.get("wpa_psks") or {})
                transaction += [
                    {
                        "op": "update",
                        "table": "Wifi_VIF_Config",
                        "where": where_uuid(vif_uuid),
                        "row": {"ssid": intent.ssid, "multi_ap": MULTI_AP["fronthaul"]},
                    },
                    {
                        "op": "mutate",
                        "table": "Wifi_VIF_Config",
                        "where": where_uuid(vif_uuid),
                        "mutations": [
                            ["wpa_psks", "delete", ["set", slots]],
                            ["wpa_psks", "insert", ["map", [["key", key]]]],
                        ],
                    },
                ]
                counts = [None, None, None, 1, 1]
        if self.slots and intent.additional is not None:
            extra_ops, extra_counts = await self._extra_ops(intent, raw, radio_uuid)
            transaction += extra_ops
            counts += extra_counts
        return await self._commit(
            transaction,
            counts,
            attempt,
            raw,
            decoded,
            action="create" if vif_uuid is None else "update",
            additional=len(intent.additional or ()),
        )

    async def _commit(self, transaction, counts, attempt, raw, decoded, *, action, additional):
        """Send one guarded transaction and judge its reply (the submit of every radio's scope)."""
        if self.before_transaction:
            await self.before_transaction()
        discard, self.drop_next_reply = self.drop_next_reply, False
        self.write_count += 1
        try:
            results = await self.session.transact(
                transaction,
                attempt["transaction_id"],
                attempt["session_generation"],
                discard_reply=discard,
            )
            check_results(results, counts)
        except (ConnectionError, TimeoutError):
            return SubmitResult("unknown", {"attribution": "unknown"}, Reason.OUTCOME_UNKNOWN)
        except EmosaError as exc:
            if exc.code == Reason.PRECONDITION_FAILED:
                return SubmitResult("conflict", {}, exc.code)
            return SubmitResult(
                "unknown" if exc.code == Reason.OUTCOME_UNKNOWN else "rejected", {}, exc.code
            )
        instance = self._instance(decoded)
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "session_generation": raw["generation"],
                **({"instance": instance} if instance else {}),
                "profile": self.profile.id,
                "action": action,
                "additional_bss_count": additional,
                "results": results,
            },
        )


class RadioBackend(PodBackend):
    """A further radio of the pod (spec 9): the slots on it, from that radio's own M2 set.

    One M2 configures one radio (EasyMesh 6.1 §7.1), so each of the profile's ``radios`` is its
    own scope with its own operations: a GRE parent's b-ap-50 on its 5 GHz radio is written from
    the 5 GHz radio's M2, never from the fronthaul's, and a renew of one radio leaves the other's
    BSSes alone. The radio serves no fronthaul of its own: its intent (``RadioIntent``) is the
    set's BSSes fitted to its slots. Of the radio it writes only its slot VIFs (insert/delete of
    its own references in ``vif_configs``) and their Inet rows; never the channel or ``ht_mode``
    (the radio follows the pod's station on it) nor the pod's own rows there (the station),
    unless the profile places the radio (a declared ``channel``: a fronthaul with no station),
    whose channel and width it then writes with its slots.
    """

    primary_bss = False  # the WSC bridge builds a RadioIntent: no primary fronthaul BSS

    def __init__(
        self, pod_id, session, vault, *, serial, band, profile=None, multi_bss=False, underlay=None
    ):
        super().__init__(
            pod_id,
            session,
            vault,
            serial=serial,
            profile=profile,
            multi_bss=multi_bss,
            underlay=underlay,
        )
        if band == self.profile.band or band not in {r["band"] for r in self.profile.radios}:
            raise EmosaError(Reason.INVALID_INPUT, "not one of the profile's further radios")
        self.band = band
        self.if_name = None  # no bound fronthaul VIF
        self.radio_id = self.bss_id = f"radio-{band}"
        # never written (spec 9: it follows the station), unless the profile places the radio: a
        # declared channel (a fronthaul with no station) and its width, written with its slots
        extra = next(r for r in self.profile.radios if r["band"] == band)
        self.channel = extra.get("channel")
        self.ht_mode = extra.get("ht_mode") if self.channel is not None else None
        self.slots = slots_on(self.profile, band) if multi_bss else ()
        self.max_bss = len(self.slots)
        # its exchange takes the radio's whole M2 set, fitted to its slots (none without multi_bss)
        self.multi_bss = True

    def _binding(self, snapshot):
        decoded, radio_uuid, _, radio_states = self._radio(snapshot)
        self.identity = (
            {
                "radio_mac": radio_states[0].get("mac"),
                "bssid": None,
                "channel": radio_states[0].get("channel"),
                "radio_if_name": self.radio_name,
            }
            if len(radio_states) == 1 and radio_states[0].get("mac")
            else None
        )
        return None, radio_uuid, {}, {}, decoded

    async def snapshot(self):
        """The radio's slot BSSes as the pod has them; ready once the pod and radio are bound."""
        self.identity = None
        try:
            raw = await self.session.snapshot()
            _, radio_uuid, _, _, decoded = self._binding(raw)
            extras = self._extras(decoded, radio_uuid)
            ready = bool(raw["ready"] and self.identity)
            self.last = Snapshot(
                {"additional": self._additional(e["config"] for e in extras.values())},
                Observation(
                    self.pod_id,
                    self.bss_id,
                    {
                        "additional": self._additional(
                            e["state"] for e in extras.values() if e["state"]
                        )
                    },
                    "ovsdb",
                    self.mode,
                    raw["generation"],
                    utc_now(),
                    ready,
                    self.state_provenance,
                    revision=raw["revision"],
                ),
                ready,
                raw["generation"],
                raw["schema"].fingerprint,
                self._instance(decoded),
            )
        except (EmosaError, ConnectionError, TimeoutError):
            if self.last is None:
                raise
            self.last.ready = False
            self.last.observed.fresh = False
        return self.last

    async def plan(self, intent):
        intent.target(self.vault)
        if (intent.pod_id, intent.radio_id) != (self.pod_id, self.radio_id):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound radio")
        assigned = self._assign(intent)
        if not self.underlay and self.parent_aps & set(assigned):
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "a GRE parent AP needs the pod's underlay (spec 8.6)"
            )
        raw = await self.session.snapshot()
        _, radio_uuid, _, _, decoded = self._binding(raw)
        if not raw["ready"] or not self.identity:
            raise EmosaError(Reason.NOT_READY, "complete current references/state required")
        self._extras(decoded, radio_uuid)
        return {
            "mapping": self.profile.id,
            "radio_id": self.radio_id,
            "bss_id": self.bss_id,
            "action": "slots",
            "additional_slots": {name: bss["role"] for name, bss in assigned.items()},
            "fields": ["Wifi_VIF_Config", "Wifi_Radio_Config.vif_configs", "Inet"],
            "guard": "pod serial, the radio, its slot VIFs",
        }

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        _, radio_uuid, _, _, decoded = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        radio = raw["tables"]["Wifi_Radio_Config"][radio_uuid]
        # the radio's identity only: its vif_configs also hold the pod's own station, which the
        # pod may change at any time; EMOSA's insert/delete of its own references is atomic
        transaction = [
            self._node_guard(raw),
            guard("Wifi_Radio_Config", radio_uuid, radio, ["if_name"]),
        ]
        counts = [None, None]
        if self.channel is not None:
            # a radio the profile places: its declared channel and width, as a cold fronthaul's
            placed = {"channel": self.channel, "enabled": True}
            if self.ht_mode is not None:
                placed["ht_mode"] = self.ht_mode
            if any(radio.get(k) != v for k, v in placed.items()):
                transaction.append(
                    {
                        "op": "update",
                        "table": "Wifi_Radio_Config",
                        "where": where_uuid(radio_uuid),
                        "row": placed,
                    }
                )
                counts.append(1)
        extra_ops, extra_counts = await self._extra_ops(intent, raw, radio_uuid)
        return await self._commit(
            transaction + extra_ops,
            counts + extra_counts,
            attempt,
            raw,
            decoded,
            action="slots",
            additional=len(intent.additional),
        )
