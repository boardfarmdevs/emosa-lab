# SPDX-License-Identifier: Apache-2.0
"""A legacy pod's radio plan, as an EMOSA scope (spec 3.10, profile ``radio_plan: emosa``).

A legacy pod's database is its vendor's seed again at every start: the operator's APs on every
radio, some with an address and a DHCP server of their own, and the radios' channels as the
operator left them. With ``radio_plan: emosa`` the plan is EMOSA's, written once per start of the
pod's OpenSync in one guarded transaction: the Inet row of every ``mode=ap`` VIF the profile does
not name (its fronthaul and its slots) off, which takes its address and DHCP server away and
leaves its link to ``wm`` (OpenSync's inet_vif), and, while the pod is wired (its uplink in use an
Ethernet port, by the interface: ``uplink_wired``), the fronthaul radio on the profile's channel
and ``ht_mode``. An AP is never disabled: the 2.4.3 pods' vendor healthcheck reboots a pod
whose AP stops beaconing (14 October). Each such AP is neutralized instead: hidden, out of any
bridge, on EMOSA's own passphrase (a per-pod ``emosa-`` secret, never logged), so the
operator's credentials admit no one and it still beacons. A VIF the profile names is never
touched. A converted pod talks only to EMOSA: the seed's own way to its operator's cloud,
``AWLAN_Node``'s MQTT settings, headers and topics (qm's broker), emptied, and its
``Wifi_Stats_Config`` rows (the statistics it samples for that broker) removed, in the same
write. While the pod is wired and the site has a GTP (``uplink.bootstrap``), every station row
not on the GTP's SSID (its own or a linked credential's: EMOSA's GTP station, or one a start-up
hook wrote) is deleted in that write too, with its Inet row, its credentials and its radio's
reference: the seed's onboarding stations, which can associate to nothing on a converted site and
fail the vendor healthcheck. Every enabled AP beacons: a radio the profile neither binds nor
places (its ``idle_radios``) that carries an enabled AP while its ``Wifi_Radio_State`` shows it
off or with no channel is turned on, at the profile's channel and width for it, in that write too
(a seed's 5 GHz radio has no channel until its operator's cloud sets one; one that beacons is
never moved).
Applied when, on the same start, those APs are neutral, their Inet rows off, the operator's cloud
cleared, no such station left, no such radio dark and the radio on the profile's channel (or the
pod not wired, its radio then its upstream's, which ``wm`` follows).
"""

import json
from dataclasses import dataclass

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import check_results, failed_check, where_uuid
from emosa.opensync.stations import deletion
from emosa.opensync.uplink import INET_TABLES, credential_key, start_instance, uplink_wired

MODE = "opensync-2.0-radio-plan"
NEUTRAL_REF = "emosa-unclaimed"
PROVENANCE = "opensync-wm:Wifi_VIF_State+Wifi_Radio_Config"
# AWLAN_Node's columns that point qm at a broker: the operator's cloud on a seed
CLOUD = ("mqtt_headers", "mqtt_settings", "mqtt_topics")
# What the scope monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES): the radio's
# channel and width, which its guard compares, and the Inet rows', cm's, the cloud's and the
# statistics' columns it reads
MONITOR = {
    # an unclaimed AP neutral: hidden (with bridge, security); a station's credentials
    "Wifi_VIF_Config": ["ssid_broadcast", "credential_configs"],
    "Wifi_Credential_Config": ["ssid"],
    "Wifi_Radio_Config": ["channel", "ht_mode"],  # with the schema's enabled and vif_configs
    "Wifi_Inet_Config": ["if_name", "enabled"],
    "Connection_Manager_Uplink": ["if_name", "if_type", "is_used"],
    "AWLAN_Node": list(CLOUD),
    "Wifi_Stats_Config": ["stats_type", "radio_type"],
}
for _table in INET_TABLES:  # what the uplink in use is (uplink_wired)
    MONITOR[_table] = list(
        dict.fromkeys([*MONITOR.get(_table, []), "if_name", "if_type", "gre_ifname"])
    )


@dataclass(frozen=True)
class RadioPlanIntent:
    """The pod's radio plan: only the VIFs named ``claimed`` may be APs with an Inet row on (an
    address of their own); the radio of ``band`` on ``channel`` at ``ht_mode`` while the pod is
    wired; each of ``idle_radios`` on, at its channel and ht_mode, when it carries an enabled AP
    but cannot beacon."""

    pod_id: str
    claimed: tuple
    band: str
    channel: int
    ht_mode: str
    neutral_ref: str = NEUTRAL_REF  # EMOSA's own passphrase for the unclaimed APs
    # the site's GTP SSID (uplink.bootstrap): while the pod is wired, every station row not on it is
    # deleted (None: no GTP, no station judged)
    gtp_ssid: str | None = None
    # the radios the profile neither binds nor places: ({"band", "channel", "ht_mode"}, ...)
    idle_radios: tuple = ()
    # spec 3.10: channel and ht_mode are the station's that holds the radio (the agent's follow of
    # its move), not the profile's: the radio written alone, on whatever uplink
    follows: bool = False

    def record(self):
        return {
            "pod_id": self.pod_id,
            "claimed": list(self.claimed),
            "band": self.band,
            "channel": self.channel,
            "ht_mode": self.ht_mode,
            "neutral_ref": self.neutral_ref,
            **({"gtp_ssid": self.gtp_ssid} if self.gtp_ssid else {}),
            **({"idle_radios": [dict(r) for r in self.idle_radios]} if self.idle_radios else {}),
            **({"follows": True} if self.follows else {}),
        }

    def validate(self):
        if not isinstance(self.pod_id, str) or not self.pod_id:
            raise EmosaError(Reason.INVALID_INPUT, "explicit pod required")
        if not self.claimed or not all(isinstance(n, str) and n for n in self.claimed):
            raise EmosaError(Reason.INVALID_INPUT, "the profile's VIF names are required")
        if not isinstance(self.channel, int) or not isinstance(self.ht_mode, str):
            raise EmosaError(Reason.INVALID_INPUT, "the profile's channel and ht_mode are required")
        if type(self.follows) is not bool:
            raise EmosaError(Reason.INVALID_INPUT, "follows is true or false")
        if self.gtp_ssid is not None and (
            not isinstance(self.gtp_ssid, str) or not 1 <= len(self.gtp_ssid) <= 32
        ):
            raise EmosaError(Reason.INVALID_INPUT, "the GTP's SSID is 1 to 32 characters")
        idle = self.idle_radios
        if not all(
            isinstance(r, dict)
            and isinstance(r.get("band"), str)
            and r["band"] != self.band
            and isinstance(r.get("channel"), int)
            and isinstance(r.get("ht_mode"), str)
            for r in idle
        ) or len({r["band"] for r in idle}) != len(idle):
            raise EmosaError(Reason.INVALID_INPUT, "an idle radio is a band, channel and ht_mode")

    def target(self, vault=None):
        self.validate()
        return {
            "unclaimed_inet_on": [],
            "unclaimed_exposed": [],
            "operator_cloud": [],
            "foreign_stations": [],
            "dark_radios": [],
            "radio_planned": True,
        }


def neutral_row(key):
    """An unclaimed AP made neutral: hidden, in no bridge, on ``key`` (WPA2-PSK, the security
    map); still enabled, so it beacons."""
    return {
        "security": ["map", [["encryption", "WPA-PSK"], ["key", key]]],
        "ssid_broadcast": "disabled",
        "bridge": "",
    }


def neutral(row, key):
    """The decoded AP row already neutral (``neutral_row``)."""
    bridge = row.get("bridge")
    return (
        row.get("ssid_broadcast") == "disabled"
        and bridge in (None, "", [])
        and credential_key(row) == key
    )


def kept_station(row, intent, credentials):
    """A station row the plan keeps: any without a GTP configured; else one on the GTP's SSID, its
    own or a linked credential's (EMOSA's GTP station, or one a start-up hook wrote)."""
    if intent.gtp_ssid is None:
        return True
    ssids = {row.get("ssid")} | {
        credentials.get(c, {}).get("ssid") for c in row.get("credential_configs") or []
    }
    return intent.gtp_ssid in ssids


def optional(value):
    """An OVSDB optional's value in a wait row: the value, or the empty set."""
    return ["set", []] if value is None else value


def ovs_map(value):
    """A decoded map column as OVSDB's map, its pairs sorted."""
    return ["map", [[k, v] for k, v in sorted((value or {}).items())]]


class RadioPlanBackend:
    """The radio plan scope of one legacy pod, bound by serial: its profile's plan ``intent``."""

    mode = MODE

    def __init__(self, pod_id, session, vault, *, serial, intent):
        self.pod_id, self.session, self.expected_serial = pod_id, session, serial
        self.vault = vault
        self.intent = intent
        self.instance = None
        self.last = None
        self.write_count = 0

    def _decode(self, raw):
        schema = raw["schema"]
        return {
            t: {u: schema.row(t, row) for u, row in rows.items()}
            for t, rows in raw["tables"].items()
        }

    def _binding(self, raw):
        decoded = self._decode(raw)
        nodes = decoded.get("AWLAN_Node", {})
        if len(nodes) != 1 or next(iter(nodes.values())).get("serial_number") != (
            self.expected_serial
        ):
            raise EmosaError(Reason.NOT_READY, "pod identity absent or not the bound serial")
        self.instance = start_instance(decoded)
        return decoded, next(iter(nodes))

    def _changes(self, decoded, intent):
        """What the plan writes now: the unclaimed APs not neutral ((uuid, if_name) each: hidden,
        in no bridge, on EMOSA's own passphrase), their Inet rows on ((uuid, if_name)), the radio
        ((uuid, row) or None: wired and not on the profile's channel), the operator's cloud
        ({column: map} as it is, or None: no MQTT setting left), the statistics rows ((uuid,
        row) each), the foreign stations (uuids, or None) and the dark idle radios ((uuid, row,
        idle radio) each: an enabled AP on it, wm showing it off or with no channel). And the
        configured view, with the unclaimed APs on in their Config or State (left on)."""
        claimed = set(intent.claimed)
        aps = sorted(
            (r.get("if_name"), u)
            for u, r in decoded.get("Wifi_VIF_Config", {}).items()
            if r.get("mode") == "ap" and r.get("if_name") not in claimed
        )
        names = {n for n, _ in aps}
        configs = {n for n, u in aps if decoded["Wifi_VIF_Config"][u].get("enabled") is True}
        states = {
            r.get("if_name")
            for r in decoded.get("Wifi_VIF_State", {}).values()
            if r.get("if_name") in names and r.get("enabled") is True
        }
        inets = sorted(
            (r.get("if_name"), u)
            for u, r in decoded.get("Wifi_Inet_Config", {}).items()
            if r.get("if_name") in names and r.get("enabled") is True
        )
        key = self.vault.generated(intent.neutral_ref)
        exposed = [(n, u) for n, u in aps if not neutral(decoded["Wifi_VIF_Config"][u], key)]
        radios = [
            (u, r)
            for u, r in decoded.get("Wifi_Radio_Config", {}).items()
            if r.get("freq_band") == intent.band
        ]
        radio = radios[0] if len(radios) == 1 else (None, {})
        # an Ethernet port, decided by the interface (27 October): a GRE over Wi-Fi typed eth isn't
        wired = uplink_wired(decoded)
        on_plan = (radio[1].get("channel"), radio[1].get("ht_mode")) == (
            intent.channel,
            intent.ht_mode,
        )
        # spec 3.10: a station on the radio connected to a parent (the GTP station, or a Wi-Fi
        # uplink) holds its channel: the radio follows that station, and the pod's OpenSync pulls
        # it back to its Config's channel, so the profile's channel is written only while no
        # station holds the radio (the agent's follow writes the station's)
        vif_states = decoded.get("Wifi_VIF_State", {})
        radio_state = next(
            (
                r
                for r in decoded.get("Wifi_Radio_State", {}).values()
                if r.get("freq_band") == intent.band
            ),
            {},
        )
        held = isinstance(radio_state.get("channel"), int) and any(
            (vif_states.get(u) or {}).get("mode") == "sta"
            and (vif_states.get(u) or {}).get("enabled") is True
            and bool((vif_states.get(u) or {}).get("parent"))
            for u in radio_state.get("vif_states") or ()
        )
        # while wired: every station row not on the GTP's SSID, with what goes with it
        vifs = decoded.get("Wifi_VIF_Config", {})
        credentials = decoded.get("Wifi_Credential_Config", {})
        foreign = sorted(
            (r.get("if_name") or "", u)
            for u, r in vifs.items()
            if wired and r.get("mode") == "sta" and not kept_station(r, intent, credentials)
        )
        node = next(iter(decoded.get("AWLAN_Node", {}).values()), {})
        cloud = {c: dict(node.get(c) or {}) for c in CLOUD}
        stats = sorted(decoded.get("Wifi_Stats_Config", {}).items())
        # every enabled AP beacons: an idle radio carrying one goes on when wm shows it off or
        # with no channel (a seed's Config has no channel even where its radio beacons)
        idle = {r["band"]: r for r in intent.idle_radios}
        shown = {
            r.get("if_name"): r.get("enabled") is True and r.get("channel") is not None
            for r in decoded.get("Wifi_Radio_State", {}).values()
        }
        dark = sorted(
            (r.get("if_name") or "", u, idle[r.get("freq_band")])
            for u, r in decoded.get("Wifi_Radio_Config", {}).items()
            if r.get("freq_band") in idle
            and not shown.get(r.get("if_name"))
            and any(
                vifs.get(v, {}).get("mode") == "ap" and vifs.get(v, {}).get("enabled") is True
                for v in r.get("vif_configs") or []
            )
        )
        writes_radio = intent.follows or (wired and not held)
        changes = (
            [(u, n) for n, u in exposed],
            [(u, n) for n, u in inets],
            radio if writes_radio and radio[0] is not None and not on_plan else None,
            cloud if any(cloud.values()) else None,
            stats,
            [u for _, u in foreign] or None,
            [(u, decoded["Wifi_Radio_Config"][u], d) for _, u, d in dark],
        )
        if intent.follows:  # the station's channel followed: that write alone
            changes = ([], [], changes[2], None, [], None, [])
        configured = {
            "unclaimed": sorted(names),
            "unclaimed_enabled": sorted(configs | states),
            "unclaimed_inet_on": sorted(n for n, _ in inets),
            "unclaimed_exposed": sorted(n for n, _ in exposed),
            "operator_cloud": [c for c in CLOUD if cloud[c]]
            + (["Wifi_Stats_Config"] if stats else []),
            "foreign_stations": sorted({n for n, _ in foreign}),
            "dark_radios": [n for n, _, _ in dark],
            "channel": radio[1].get("channel"),
            "ht_mode": radio[1].get("ht_mode"),
            "wired": wired,
            # the station that holds the radio: its channel and width as wm shows the radio
            "station_channel": radio_state.get("channel") if held else None,
            "station_ht_mode": radio_state.get("ht_mode") if held else None,
            "follows": intent.follows,
            "radio_planned": on_plan if intent.follows else (not wired or on_plan or held),
            "pending": any(changes),
        }
        return changes, configured

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, _ = self._binding(raw)
            _, configured = self._changes(decoded, self.intent)
            self.last = Snapshot(
                configured,
                Observation(
                    self.pod_id,
                    "radio-plan",
                    dict(configured),
                    "ovsdb",
                    self.mode,
                    raw["generation"],
                    utc_now(),
                    bool(raw["ready"]),
                    PROVENANCE,
                    revision=raw["revision"],
                ),
                bool(raw["ready"]),
                raw["generation"],
                raw["schema"].fingerprint,
            )
        except (EmosaError, ConnectionError, TimeoutError):
            if self.last is None:
                raise
            self.last.ready = False
            self.last.observed.fresh = False
        return self.last

    def _check(self, intent):
        intent.validate()
        if intent.pod_id != self.pod_id:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound pod")

    async def plan(self, intent):
        self._check(intent)
        raw = await self.session.snapshot()
        decoded, _ = self._binding(raw)
        if not raw["ready"]:
            raise EmosaError(Reason.NOT_READY, "the pod's database is not ready")
        (exposed, inets, radio, _, _, _, _), configured = self._changes(decoded, intent)
        return {
            "mapping": MODE,
            "action": "radio-plan",
            "neutralize": [n for _, n in exposed],
            "inet_off": [n for _, n in inets],
            "channel": None if radio is None else intent.channel,
            "cloud_cleared": configured["operator_cloud"],
            "stations_removed": configured["foreign_stations"],
            "radios_on": configured["dark_radios"],
            "instance": self.instance,
            "fields": [
                "Wifi_VIF_Config.security",
                "Wifi_VIF_Config.ssid_broadcast",
                "Wifi_VIF_Config.bridge",
                "Wifi_Inet_Config.enabled",
                "Wifi_Radio_Config.channel",
                "Wifi_Radio_Config.ht_mode",
                "Wifi_Radio_Config.enabled",
                *(f"AWLAN_Node.{c}" for c in CLOUD),
                "Wifi_Stats_Config",
                "Wifi_VIF_Config (stations)",
                "Wifi_Credential_Config",
                "Wifi_Inet_Config (stations)",
            ],
            "guard": "pod serial and each changed row as it is",
        }

    def transaction(self, intent, decoded, node_uuid):
        """The guarded write: its operations and the counts each must match (None: no write)."""
        (exposed, inets, radio, cloud, stats, stations, dark), _ = self._changes(decoded, intent)
        if not (exposed or inets or radio or cloud or stats or stations or dark):
            return None, None
        operations = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "columns": ["serial_number"],
                "until": "==",
                "rows": [{"serial_number": self.expected_serial}],
                "timeout": 0,
            }
        ]
        counts = [None]
        key = self.vault.generated(intent.neutral_ref)
        for uuid, name in exposed:
            operations += [
                {
                    "op": "wait",
                    "table": "Wifi_VIF_Config",
                    "where": where_uuid(uuid),
                    "columns": ["if_name", "mode"],
                    "until": "==",
                    "rows": [{"if_name": name, "mode": "ap"}],
                    "timeout": 0,
                },
                {
                    "op": "update",
                    "table": "Wifi_VIF_Config",
                    "where": where_uuid(uuid),
                    "row": neutral_row(key),
                },
            ]
            counts += [None, 1]
        for uuid, name in inets:
            operations += [
                {
                    "op": "wait",
                    "table": "Wifi_Inet_Config",
                    "where": where_uuid(uuid),
                    "columns": ["if_name", "enabled"],
                    "until": "==",
                    "rows": [{"if_name": name, "enabled": True}],
                    "timeout": 0,
                },
                {
                    "op": "update",
                    "table": "Wifi_Inet_Config",
                    "where": where_uuid(uuid),
                    "row": {"enabled": False},
                },
            ]
            counts += [None, 1]
        if radio is not None:
            uuid, row = radio
            operations += [
                {
                    "op": "wait",
                    "table": "Wifi_Radio_Config",
                    "where": where_uuid(uuid),
                    "columns": ["if_name", "channel", "ht_mode"],
                    "until": "==",
                    "rows": [
                        {
                            "if_name": row.get("if_name"),
                            "channel": optional(row.get("channel")),
                            "ht_mode": optional(row.get("ht_mode")),
                        }
                    ],
                    "timeout": 0,
                },
                {
                    "op": "update",
                    "table": "Wifi_Radio_Config",
                    "where": where_uuid(uuid),
                    "row": {"channel": intent.channel, "ht_mode": intent.ht_mode},
                },
            ]
            counts += [None, 1]
        for uuid, row, idle in dark:  # each dark idle radio as it is, on at its channel
            operations += [
                {
                    "op": "wait",
                    "table": "Wifi_Radio_Config",
                    "where": where_uuid(uuid),
                    "columns": ["if_name", "enabled", "channel", "ht_mode"],
                    "until": "==",
                    "rows": [
                        {
                            "if_name": row.get("if_name"),
                            "enabled": optional(row.get("enabled")),
                            "channel": optional(row.get("channel")),
                            "ht_mode": optional(row.get("ht_mode")),
                        }
                    ],
                    "timeout": 0,
                },
                {
                    "op": "update",
                    "table": "Wifi_Radio_Config",
                    "where": where_uuid(uuid),
                    "row": {
                        "enabled": True,
                        "channel": idle["channel"],
                        "ht_mode": idle["ht_mode"],
                    },
                },
            ]
            counts += [None, 1]
        if cloud is not None:  # qm's broker, headers and topics emptied: no cloud but EMOSA
            operations += [
                {
                    "op": "wait",
                    "table": "AWLAN_Node",
                    "where": where_uuid(node_uuid),
                    "columns": list(CLOUD),
                    "until": "==",
                    "rows": [{c: ovs_map(cloud[c]) for c in CLOUD}],
                    "timeout": 0,
                },
                {
                    "op": "update",
                    "table": "AWLAN_Node",
                    "where": where_uuid(node_uuid),
                    "row": {c: ["map", []] for c in CLOUD},
                },
            ]
            counts += [None, 1]
        for uuid, row in stats:  # each statistics row as it is, removed
            operations += [
                {
                    "op": "wait",
                    "table": "Wifi_Stats_Config",
                    "where": where_uuid(uuid),
                    "columns": ["stats_type", "radio_type"],
                    "until": "==",
                    "rows": [
                        {
                            "stats_type": optional(row.get("stats_type")),
                            "radio_type": optional(row.get("radio_type")),
                        }
                    ],
                    "timeout": 0,
                },
                {"op": "delete", "table": "Wifi_Stats_Config", "where": where_uuid(uuid)},
            ]
            counts += [None, 1]
        if stations is not None:  # each station row as it is, with what only it uses
            more, more_counts = deletion(decoded, stations)
            operations += more
            counts += more_counts
        return operations, counts

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        decoded, node_uuid = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        operations, counts = self.transaction(intent, decoded, node_uuid)
        if operations is None:
            return SubmitResult("rejected", {}, Reason.NOT_READY)  # nothing differs any more
        self.write_count += 1
        try:
            results = await self.session.transact(
                operations, attempt["transaction_id"], attempt["session_generation"]
            )
            check_results(results, counts)
        except (ConnectionError, TimeoutError):
            return SubmitResult("unknown", {"attribution": "unknown"}, Reason.OUTCOME_UNKNOWN)
        except EmosaError as exc:
            return failed_check(exc)
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "session_generation": raw["generation"],
                "action": "radio-plan",
                "instance": self.instance,
                "results": json.loads(json.dumps(results, default=str)),
            },
            None,
        )
