"""OpenSync 6.6.1.0 on opensync-lab's hwsim pods: the observed VIF profile.

Profile ``opensync-lab-hwsim-6.6.1-v1`` is written from the pod's own managers,
not from the synthetic simulator:

- ``ow_ovsdb.c`` reports the RSN PSK AKM as ``wpa_key_mgmt=["wpa-psk"]``; RSN
  (WPA2) versus WPA is carried by ``rsn_pairwise_ccmp`` / ``wpa_pairwise_*``.
- ``osw_drv_target.c`` names PSK slots ``key`` (id 0) and ``key-N``. The lab's
  NOC historically wrote ``key--1``; this profile replaces whatever single slot
  it finds with ``key``.
- ``tx_chainmask`` is never written: on hwsim osw confsync then never settles
  and aborts ``owm`` after 180 s (opensync-lab fa3a1cc).

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
import json

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import OpenSyncBackend, check_results, guard, where_uuid
from emosa.wire.operation_bridge import ScopeContext

PROFILE = "opensync-lab-hwsim-6.6.1-v1"
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
# A cold pod's fronthaul, as opensync-lab's NOC creates it (local-noc mesh.py pod_step).
FRONTHAUL = {
    "mode": "ap",
    "enabled": True,
    "bridge": "br-home",
    "ssid_broadcast": "enabled",
    "ap_bridge": True,
    "mac_list_type": "none",
    "vif_radio_idx": 1,  # as local-noc writes it; index 2 left hostapd unable to add the BSS
    "wpa": True,
    "wpa_key_mgmt": "wpa-psk",
    "rsn_pairwise_ccmp": True,
    "security": ["map", []],
}
BACKHAUL = {
    **FRONTHAUL,
    "ssid_broadcast": "disabled",
    "ap_bridge": False,
    "multi_ap": "backhaul_bss",
}
# (if_name, role, vif_radio_idx) beyond the bound fronthaul, in assignment order
EXTRA_SLOTS_24 = (
    ("svc-d-ap-24", "fronthaul", 4),
    ("svc-e-ap-24", "fronthaul", 5),
    ("fh-24", "fronthaul", 6),
    ("b-ap-24", "backhaul", 1),
)
INET = {
    "if_type": "vif",
    "enabled": True,
    "network": True,
    "NAT": False,
    "ip_assign_scheme": "none",
    "mtu": 1500,
}


def wpa2_psk(row):
    """True for the 6.6 osw encoding of WPA2-PSK/CCMP with one PSK slot."""
    return (
        row.get("wpa") is True
        and row.get("wpa_key_mgmt") == ["wpa-psk"]
        and row.get("rsn_pairwise_ccmp") is True
        and not row.get("security")
        and not row.get("wpa_pairwise_tkip")
        and not row.get("wpa_pairwise_ccmp")
        and len(row.get("wpa_psks") or {}) == 1
    )


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
        if_name="home-ap-24",
        band="2.4G",
        channel=6,
        ht_mode="HT20",
        multi_bss=False,
        extra_slots=EXTRA_SLOTS_24,
    ):
        super().__init__(
            pod_id, session, vault, if_name=if_name, radio_name="", expected_serial=serial
        )
        self.slots = tuple(extra_slots) if multi_bss else ()
        self.max_bss = 1 + len(self.slots)
        self.state_provenance = PROVENANCE
        self.band, self.channel, self.ht_mode = band, channel, ht_mode
        self.anchor = None
        self.identity = None  # radio MAC, BSSID (None before the VIF exists), channel, radio

    def _values(self, row):
        psks = row.get("wpa_psks") or {}
        key = next(iter(psks.values())) if len(psks) == 1 else None
        return {
            "ssid": row.get("ssid"),
            "enabled": row.get("enabled"),
            "mode": row.get("mode"),
            "security_mode": "wpa2-psk" if wpa2_psk(row) else None,
            "credential_fingerprint": self.vault.fingerprint(key) if key is not None else None,
        }

    def _binding(self, snapshot):
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
        radio_states = [
            r
            for r in decoded.get("Wifi_Radio_State", {}).values()
            if r.get("radio_config") == radio_uuid
        ]
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
            if self.slots:
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
            )
        except (EmosaError, ConnectionError, TimeoutError):
            if self.last is None:
                raise
            self.last.ready = False
            self.last.observed.fresh = False
        return self.last

    @staticmethod
    def role(row):
        return "backhaul" if row.get("multi_ap") == "backhaul_bss" else "fronthaul"

    def _additional(self, rows):
        """Sorted [role, ssid, key fingerprint] of extra BSS rows (Config or State)."""
        values = []
        for row in rows:
            psks = row.get("wpa_psks") or {}
            key = next(iter(psks.values())) if len(psks) == 1 and wpa2_psk(row) else None
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
        """Slot VIFs present on the pod: if_name -> uuid, Config row, State row."""
        names = {name for name, _, _ in self.slots}
        radio = decoded["Wifi_Radio_Config"][radio_uuid]
        found = {}
        for uuid, row in decoded.get("Wifi_VIF_Config", {}).items():
            name = row.get("if_name")
            if name not in names:
                continue
            if name in found or uuid not in (radio.get("vif_configs") or []):
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
                    [PROFILE, node_uuid, radio_uuid, self.identity["radio_mac"]], sort_keys=True
                ).encode()
            ).hexdigest(),
        )
        if self.anchor is not None and (
            self.anchor.schema_fingerprint != anchor.schema_fingerprint
            or self.anchor.binding_token != anchor.binding_token
        ):
            raise EmosaError(Reason.NOT_READY, "bound pod graph changed")
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
        raw = await self.session.snapshot()
        vif_uuid, radio_uuid, config, state, decoded = self._binding(raw)
        if not raw["ready"] or not self.identity:
            raise EmosaError(Reason.NOT_READY, "complete current references/state required")
        if self.slots:
            self._extras(decoded, radio_uuid)
        common = {
            "mapping": PROFILE,
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
            "fields": ["ssid", "wpa_psks"],
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
                "row": {
                    **FRONTHAUL,
                    "if_name": self.if_name,
                    "ssid": intent.ssid,
                    "wpa_psks": ["map", [["key", key]]],
                },
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
                    "row": {**INET, "if_name": self.if_name},
                },
            ]
            counts += [None, None]
        return transaction, counts

    async def _extra_ops(self, intent, raw, radio_uuid):
        """Make the managed slot VIFs exactly the intent's additional BSSes."""
        decoded = {
            t: {u: raw["schema"].row(t, r) for u, r in rows.items()}
            for t, rows in raw["tables"].items()
        }
        extras = self._extras(decoded, radio_uuid)
        assigned = self._assign(intent)
        inet = await self.session.transact(
            [{"op": "select", "table": "Wifi_Inet_Config", "where": [], "columns": ["if_name"]}]
        )
        inet_names = {row.get("if_name") for row in (inet[0].get("rows", []) if inet else [])}
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
                base = BACKHAUL if role == "backhaul" else FRONTHAUL
                key = self.vault.resolve(bss["secret_ref"])
                if present:
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
                            "row": {
                                **base,
                                "if_name": name,
                                "ssid": bss["ssid"],
                                "vif_radio_idx": radio_idx,
                                "wpa_psks": ["map", [["key", key]]],
                            },
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
                if name not in inet_names:
                    ops.append(
                        {
                            "op": "insert",
                            "table": "Wifi_Inet_Config",
                            "row": {**INET, "if_name": name},
                        }
                    )
                    counts.append(None)
        return ops, counts

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        vif_uuid, radio_uuid, config, _, _ = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        if vif_uuid is None:
            transaction, counts = await self._create(intent, raw, radio_uuid)
        else:
            vif = raw["tables"]["Wifi_VIF_Config"][vif_uuid]
            radio = raw["tables"]["Wifi_Radio_Config"][radio_uuid]
            slots = sorted(config.get("wpa_psks") or {})
            transaction = [
                self._node_guard(raw),
                guard("Wifi_Radio_Config", radio_uuid, radio, ["if_name", "vif_configs"]),
                guard("Wifi_VIF_Config", vif_uuid, vif, list(VIF_GUARDS)),
                {
                    "op": "update",
                    "table": "Wifi_VIF_Config",
                    "where": where_uuid(vif_uuid),
                    "row": {"ssid": intent.ssid},
                },
                {
                    "op": "mutate",
                    "table": "Wifi_VIF_Config",
                    "where": where_uuid(vif_uuid),
                    "mutations": [
                        ["wpa_psks", "delete", ["set", slots]],
                        [
                            "wpa_psks",
                            "insert",
                            ["map", [["key", self.vault.resolve(intent.secret_ref)]]],
                        ],
                    ],
                },
            ]
            counts = [None, None, None, 1, 1]
        if self.slots and intent.additional is not None:
            extra_ops, extra_counts = await self._extra_ops(intent, raw, radio_uuid)
            transaction += extra_ops
            counts += extra_counts
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
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "session_generation": raw["generation"],
                "profile": PROFILE,
                "action": "create" if vif_uuid is None else "update",
                "additional_bss_count": len(intent.additional or ()),
                "results": results,
            },
        )
