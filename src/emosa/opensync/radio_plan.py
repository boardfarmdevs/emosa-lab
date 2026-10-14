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
one and it still beacons. A station is never touched, nor a VIF the profile names. Applied when,
on the same start, those APs are neutral, their Inet rows off and the radio on the profile's
channel (or the pod not wired, its radio then its upstream's, which ``wm`` follows).
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
# What the scope monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES): the radio's
# channel and width, which its guard compares, and the Inet rows' and cm's columns it reads
MONITOR = {
    "Wifi_VIF_Config": [
        "ssid_broadcast"
    ],  # an unclaimed AP neutral: hidden (with bridge, security)
    "Wifi_Radio_Config": ["channel", "ht_mode"],
    "Wifi_Inet_Config": ["if_name", "enabled"],
    "Connection_Manager_Uplink": ["if_name", "if_type", "is_used"],
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

    def record(self):
        return {
            "pod_id": self.pod_id,
            "claimed": list(self.claimed),
            "band": self.band,
            "channel": self.channel,
            "ht_mode": self.ht_mode,
            "neutral_ref": self.neutral_ref,
        }

    def validate(self):
        if not isinstance(self.pod_id, str) or not self.pod_id:
            raise EmosaError(Reason.INVALID_INPUT, "explicit pod required")
        if not self.claimed or not all(isinstance(n, str) and n for n in self.claimed):
            raise EmosaError(Reason.INVALID_INPUT, "the profile's VIF names are required")
        if not isinstance(self.channel, int) or not isinstance(self.ht_mode, str):
            raise EmosaError(Reason.INVALID_INPUT, "the profile's channel and ht_mode are required")

    def target(self, vault=None):
        self.validate()
        return {"unclaimed_inet_on": [], "unclaimed_exposed": [], "radio_planned": True}


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


def optional(value):
    """An OVSDB optional's value in a wait row: the value, or the empty set."""
    return ["set", []] if value is None else value


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
        in no bridge, on EMOSA's own passphrase), their Inet rows on ((uuid, if_name)), and the
        radio ((uuid, row) or None: wired and not on the profile's channel). And the configured
        view, with the unclaimed APs on in their Config or State (left on)."""
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
        changes = (
            [(u, n) for n, u in exposed],
            [(u, n) for n, u in inets],
            radio if wired and radio[0] is not None and not on_plan else None,
        )
        configured = {
            "unclaimed": sorted(names),
            "unclaimed_enabled": sorted(configs | states),
            "unclaimed_inet_on": sorted(n for n, _ in inets),
            "unclaimed_exposed": sorted(n for n, _ in exposed),
            "channel": radio[1].get("channel"),
            "ht_mode": radio[1].get("ht_mode"),
            "wired": wired,
            "radio_planned": not wired or on_plan,
            "pending": bool(changes[0] or changes[1] or changes[2]),
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
        (exposed, inets, radio), _ = self._changes(decoded, intent)
        return {
            "mapping": MODE,
            "action": "radio-plan",
            "neutralize": [n for _, n in exposed],
            "inet_off": [n for _, n in inets],
            "channel": None if radio is None else intent.channel,
            "instance": self.instance,
            "fields": [
                "Wifi_VIF_Config.security",
                "Wifi_VIF_Config.ssid_broadcast",
                "Wifi_VIF_Config.bridge",
                "Wifi_Inet_Config.enabled",
                "Wifi_Radio_Config.channel",
                "Wifi_Radio_Config.ht_mode",
            ],
            "guard": "pod serial and each changed row as it is",
        }

    def transaction(self, intent, decoded, node_uuid):
        """The guarded write: its operations and the counts each must match (None: no write)."""
        (exposed, inets, radio), _ = self._changes(decoded, intent)
        if not (exposed or inets or radio):
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
