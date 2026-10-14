# SPDX-License-Identifier: Apache-2.0
"""A legacy pod's radio plan, as an EMOSA scope (spec 3.10, profile ``radio_plan: emosa``).

A legacy pod's database is its vendor's seed again at every start: the operator's APs on every
radio, some with an address and a DHCP server of their own, and the radios' channels as the
operator left them. With ``radio_plan: emosa`` the plan is EMOSA's, written once per start of the
pod's OpenSync in one guarded transaction: the Inet row of every ``mode=ap`` VIF the profile does
not name (its fronthaul and its slots) off, which takes its address and DHCP server away and
leaves its link to ``wm`` (OpenSync's inet_vif), and, while the pod is wired (its uplink in use an
Ethernet port), the fronthaul radio on the profile's channel and ``ht_mode``. An AP is never
disabled: the 2.4.3 pods' vendor healthcheck reboots a pod whose AP stops beaconing (14
October). Each such AP is neutralized instead: hidden, out of any bridge, on EMOSA's own
passphrase (a per-pod ``emosa-`` secret, never logged), so the operator's credentials admit no
one and it still beacons. A VIF the profile names is never touched. A converted
pod talks only to EMOSA: the seed's own way to its operator's cloud, ``AWLAN_Node``'s MQTT
settings, headers and topics (qm's broker), emptied, and its ``Wifi_Stats_Config`` rows (the
statistics it samples for that broker) removed, in the same write. While the pod is wired and the
site has a GTP (``uplink.bootstrap``), every station row not on the GTP's SSID (its own or a
linked credential's: EMOSA's GTP station, or one a start-up hook wrote) is deleted in that write
too, with its Inet row, its credentials and its radio's reference: the seed's onboarding stations,
which can associate to nothing on a converted site and fail the vendor healthcheck. Applied when,
on the same start, those APs are neutral, their Inet rows off, the operator's cloud cleared, no
such station left and the radio on the profile's channel (or the pod not wired, its
radio then its upstream's, which ``wm`` follows).
"""

import json
from dataclasses import dataclass

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import check_results, where_uuid
from emosa.opensync.uplink import credential_key, start_instance

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
    "Wifi_Radio_Config": ["channel", "ht_mode"],
    "Wifi_Inet_Config": ["if_name", "enabled"],
    "Connection_Manager_Uplink": ["if_name", "if_type", "is_used"],
    "AWLAN_Node": list(CLOUD),
    "Wifi_Stats_Config": ["stats_type", "radio_type"],
}


@dataclass(frozen=True)
class RadioPlanIntent:
    """The pod's radio plan: only the VIFs named ``claimed`` may be APs with an Inet row on (an
    address of their own); the radio of ``band`` on ``channel`` at ``ht_mode`` while the pod is
    wired."""

    pod_id: str
    claimed: tuple
    band: str
    channel: int
    ht_mode: str
    neutral_ref: str = NEUTRAL_REF  # EMOSA's own passphrase for the unclaimed APs
    # the site's GTP SSID (uplink.bootstrap): while the pod is wired, every station row not on it is
    # deleted (None: no GTP, no station judged)
    gtp_ssid: str | None = None

    def record(self):
        return {
            "pod_id": self.pod_id,
            "claimed": list(self.claimed),
            "band": self.band,
            "channel": self.channel,
            "ht_mode": self.ht_mode,
            "neutral_ref": self.neutral_ref,
            **({"gtp_ssid": self.gtp_ssid} if self.gtp_ssid else {}),
        }

    def validate(self):
        if not isinstance(self.pod_id, str) or not self.pod_id:
            raise EmosaError(Reason.INVALID_INPUT, "explicit pod required")
        if not self.claimed or not all(isinstance(n, str) and n for n in self.claimed):
            raise EmosaError(Reason.INVALID_INPUT, "the profile's VIF names are required")
        if not isinstance(self.channel, int) or not isinstance(self.ht_mode, str):
            raise EmosaError(Reason.INVALID_INPUT, "the profile's channel and ht_mode are required")
        if self.gtp_ssid is not None and (
            not isinstance(self.gtp_ssid, str) or not 1 <= len(self.gtp_ssid) <= 32
        ):
            raise EmosaError(Reason.INVALID_INPUT, "the GTP's SSID is 1 to 32 characters")

    def target(self, vault=None):
        self.validate()
        return {
            "unclaimed_inet_on": [],
            "unclaimed_exposed": [],
            "operator_cloud": [],
            "foreign_stations": [],
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
        ({column: map} as it is, or None: no MQTT setting left) and the statistics rows ((uuid,
        row) each). And the configured view, with the unclaimed APs on in their Config or State
        (left on)."""
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
        used = [
            r
            for r in decoded.get("Connection_Manager_Uplink", {}).values()
            if r.get("is_used") is True
        ]
        wired = len(used) == 1 and used[0].get("if_type") == "eth"
        on_plan = (radio[1].get("channel"), radio[1].get("ht_mode")) == (
            intent.channel,
            intent.ht_mode,
        )
        # while wired: every station row not on the GTP's SSID, with what goes with it
        vifs = decoded.get("Wifi_VIF_Config", {})
        credentials = decoded.get("Wifi_Credential_Config", {})
        foreign = sorted(
            (r.get("if_name") or "", u)
            for u, r in vifs.items()
            if wired and r.get("mode") == "sta" and not kept_station(r, intent, credentials)
        )
        gone = {u for _, u in foreign}
        kept = [r for u, r in vifs.items() if u not in gone]
        kept_credentials = {c for r in kept for c in r.get("credential_configs") or []}
        kept_names = {r.get("if_name") for r in kept}
        stations = {
            "rows": [(u, vifs[u]) for _, u in foreign],
            "radios": sorted(
                (u, v)
                for u, r in decoded.get("Wifi_Radio_Config", {}).items()
                for v in r.get("vif_configs") or []
                if v in gone
            ),
            "credentials": sorted(
                {
                    c
                    for u in gone
                    for c in vifs[u].get("credential_configs") or []
                    if c in credentials and c not in kept_credentials
                }
            ),
            "inets": sorted(
                u
                for u, r in decoded.get("Wifi_Inet_Config", {}).items()
                if r.get("if_name") in {n for n, _ in foreign} - kept_names
            ),
        }
        node = next(iter(decoded.get("AWLAN_Node", {}).values()), {})
        cloud = {c: dict(node.get(c) or {}) for c in CLOUD}
        stats = sorted(decoded.get("Wifi_Stats_Config", {}).items())
        changes = (
            [(u, n) for n, u in exposed],
            [(u, n) for n, u in inets],
            radio if wired and radio[0] is not None and not on_plan else None,
            cloud if any(cloud.values()) else None,
            stats,
            stations if foreign else None,
        )
        configured = {
            "unclaimed": sorted(names),
            "unclaimed_enabled": sorted(configs | states),
            "unclaimed_inet_on": sorted(n for n, _ in inets),
            "unclaimed_exposed": sorted(n for n, _ in exposed),
            "operator_cloud": [c for c in CLOUD if cloud[c]]
            + (["Wifi_Stats_Config"] if stats else []),
            "foreign_stations": sorted({n for n, _ in foreign}),
            "channel": radio[1].get("channel"),
            "ht_mode": radio[1].get("ht_mode"),
            "wired": wired,
            "radio_planned": not wired or on_plan,
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
        (exposed, inets, radio, _, _, _), configured = self._changes(decoded, intent)
        return {
            "mapping": MODE,
            "action": "radio-plan",
            "neutralize": [n for _, n in exposed],
            "inet_off": [n for _, n in inets],
            "channel": None if radio is None else intent.channel,
            "cloud_cleared": configured["operator_cloud"],
            "stations_removed": configured["foreign_stations"],
            "instance": self.instance,
            "fields": [
                "Wifi_VIF_Config.security",
                "Wifi_VIF_Config.ssid_broadcast",
                "Wifi_VIF_Config.bridge",
                "Wifi_Inet_Config.enabled",
                "Wifi_Radio_Config.channel",
                "Wifi_Radio_Config.ht_mode",
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
        (exposed, inets, radio, cloud, stats, stations), _ = self._changes(decoded, intent)
        if not (exposed or inets or radio or cloud or stats or stations):
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
        if stations is not None:
            # each station row as it is, out of its radio, deleted; then its credentials and Inet
            for uuid, row in stations["rows"]:
                operations.append(
                    {
                        "op": "wait",
                        "table": "Wifi_VIF_Config",
                        "where": where_uuid(uuid),
                        "columns": ["if_name", "mode", "ssid"],
                        "until": "==",
                        "rows": [
                            {
                                "if_name": row.get("if_name"),
                                "mode": "sta",
                                "ssid": optional(row.get("ssid")),  # a credential list: none
                            }
                        ],
                        "timeout": 0,
                    }
                )
                counts.append(None)
            for radio_uuid, uuid in stations["radios"]:
                operations.append(
                    {
                        "op": "mutate",
                        "table": "Wifi_Radio_Config",
                        "where": where_uuid(radio_uuid),
                        "mutations": [["vif_configs", "delete", ["set", [["uuid", uuid]]]]],
                    }
                )
                counts.append(1)
            for table, uuids in (
                ("Wifi_VIF_Config", [u for u, _ in stations["rows"]]),
                ("Wifi_Credential_Config", stations["credentials"]),
                ("Wifi_Inet_Config", stations["inets"]),
            ):
                for uuid in uuids:
                    operations.append({"op": "delete", "table": table, "where": where_uuid(uuid)})
                    counts.append(1)
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
            if exc.code == Reason.PRECONDITION_FAILED:
                return SubmitResult("conflict", {}, exc.code)
            return SubmitResult(
                "unknown" if exc.code == Reason.OUTCOME_UNKNOWN else "rejected", {}, exc.code
            )
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
