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

Cold start: a restarted pod rebuilds its database from its bootstrap, which has
no fronthaul (the cloud normally creates it). When the bound VIF is absent, the
controller's M2 creates it: one guarded transaction inserts the VIF with the
received SSID/PSK, references it from the radio, sets the radio's declared
channel and adds its Inet row, as the lab NOC's ``pod_step`` does for its own
pods. Nothing is created before an authenticated M2 asks for it.
"""

import hashlib
import json

from emosa.backends.base import SubmitResult
from emosa.errors import EmosaError, Reason
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
    "vif_radio_idx": 1,
    "wpa": True,
    "wpa_key_mgmt": "wpa-psk",
    "rsn_pairwise_ccmp": True,
    "security": ["map", []],
}
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
    ):
        super().__init__(
            pod_id, session, vault, if_name=if_name, radio_name="", expected_serial=serial
        )
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
        """Ready to plan when the pod and its radio are bound, even with no VIF yet.

        Observed application still needs the VIF's own State (``observed.fresh``);
        only planning a cold-start creation may start from a radio without a BSS.
        """
        self.identity = None  # set again only by a successful _binding of a fresh read
        snap = await super().snapshot()
        if not snap.ready and self.identity and self.identity["bssid"] is None:
            snap.ready = snap.config.get("ssid") is None  # no VIF configured at all
        return snap

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
        intent.target(self.vault)
        if (intent.pod_id, intent.radio_id, intent.bss_id) != (
            self.pod_id,
            self.radio_id,
            self.bss_id,
        ):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound VIF")
        raw = await self.session.snapshot()
        vif_uuid, _, config, state, _ = self._binding(raw)
        if not raw["ready"] or not self.identity:
            raise EmosaError(Reason.NOT_READY, "complete current references/state required")
        common = {
            "mapping": PROFILE,
            "radio_id": self.radio_id,
            "bss_id": self.bss_id,
            "ssid": intent.ssid,
            "secret_ref": intent.secret_ref,
            "shared_radio_actuation": vif_uuid is None,
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
                "results": results,
            },
        )
