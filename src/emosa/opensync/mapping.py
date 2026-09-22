from dataclasses import asdict

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.radio_scope import (
    assess,
    decode_rows,
    require_synthetic_scope,
    transaction_guards,
)
from emosa.opensync.topology import project, unavailable


def where_uuid(row_id):
    return [["_uuid", "==", ["uuid", row_id]]]


def guard(table, row_id, row, columns):
    return {
        "op": "wait",
        "table": table,
        "where": where_uuid(row_id),
        "columns": columns,
        "until": "==",
        "rows": [{k: row[k] for k in columns}],
        "timeout": 0,
    }


def check_results(results, expected_counts):
    if not isinstance(results, list) or len(results) != len(expected_counts):
        raise EmosaError(Reason.OUTCOME_UNKNOWN, "incomplete transaction result")
    for result, count in zip(results, expected_counts, strict=True):
        if not isinstance(result, dict):
            raise EmosaError(Reason.OUTCOME_UNKNOWN, "missing transaction operation result")
        if result.get("error"):
            code = (
                Reason.PRECONDITION_FAILED
                if result["error"] == "timed out"
                else Reason.INVALID_INPUT
            )
            raise EmosaError(code, "transaction rejected by server")
        if count is not None and result.get("count") != count:
            raise EmosaError(Reason.OUTCOME_UNKNOWN, "unexpected matched-row count")


class OpenSyncBackend:
    """Mapping is deliberately qualified only for the upstream-schema simulator."""

    mode = "ovsdb-sim"

    def __init__(
        self,
        pod_id,
        session,
        vault,
        *,
        if_name="lab-ap",
        radio_name="lab-radio",
        bss_id="bss-1",
        radio_id="radio-1",
        backend_mode="ovsdb-sim",
        state_provenance="independent-simulated-manager:Wifi_VIF_State",
        expected_serial=None,
        mapping_scope="existing-bss",
        topology_binding=None,
    ):
        if backend_mode != "ovsdb-sim":
            raise EmosaError(
                Reason.MISSING_PREREQUISITE,
                "actual target schema, manager and trust qualification required",
            )
        self.pod_id, self.session, self.vault = pod_id, session, vault
        self.if_name, self.radio_name = if_name, radio_name
        self.bss_id, self.radio_id = bss_id, radio_id
        self.expected_serial = expected_serial
        self.topology_binding = topology_binding
        if mapping_scope not in {"existing-bss", "sole-fronthaul-radio"}:
            raise EmosaError(Reason.INVALID_INPUT, "unknown mapping scope")
        if mapping_scope == "sole-fronthaul-radio" and not expected_serial:
            raise EmosaError(Reason.INVALID_INPUT, "sole-radio simulation requires a bound serial")
        self.mapping_scope = mapping_scope
        if state_provenance not in {
            "independent-simulated-manager:Wifi_VIF_State",
            "independent-hostapd-nl80211-manager:Wifi_VIF_State",
        }:
            raise EmosaError(Reason.INVALID_INPUT, "unknown simulation State provenance")
        self.state_provenance = state_provenance
        self.last = None
        self.drop_next_reply = False
        self.before_transaction = None  # controller-side fault boundary, never used in hardware
        self.write_count = 0

    def _values(self, row):
        modes = row.get("wpa_key_mgmt")
        modern = (
            row.get("wpa") is True
            and modes == ["wpa2-psk"]
            and row.get("rsn_pairwise_ccmp") is True
            and not row.get("security")
            and row.get("wpa_pairwise_tkip") is False
            and row.get("wpa_pairwise_ccmp") is False
        )
        key = row.get("wpa_psks", {}).get("key")
        return {
            "ssid": row.get("ssid"),
            "enabled": row.get("enabled"),
            "mode": row.get("mode"),
            "security_mode": "wpa2-psk" if modern else None,
            "credential_fingerprint": self.vault.fingerprint(key) if key is not None else None,
        }

    def _binding(self, snapshot):
        schema, tables = snapshot["schema"], snapshot["tables"]
        decoded = {
            t: {u: schema.row(t, row) for u, row in rows.items()} for t, rows in tables.items()
        }
        if self.expected_serial is not None:
            nodes = list(decoded.get("AWLAN_Node", {}).values())
            if len(nodes) != 1 or nodes[0].get("serial_number") != self.expected_serial:
                raise EmosaError(Reason.NOT_READY, "simulated pod identity absent or mismatched")
        if self.mapping_scope == "sole-fronthaul-radio":
            require_synthetic_scope(snapshot, if_name=self.if_name, radio_name=self.radio_name)
        configs = [
            (u, r)
            for u, r in decoded.get("Wifi_VIF_Config", {}).items()
            if r.get("if_name") == self.if_name
        ]
        radios = [
            (u, r)
            for u, r in decoded.get("Wifi_Radio_Config", {}).items()
            if r.get("if_name") == self.radio_name
        ]
        if len(configs) != 1 or len(radios) != 1:
            raise EmosaError(
                Reason.NOT_READY, "designated existing AP/radio binding ambiguous or absent"
            )
        vif_uuid, config = configs[0]
        radio_uuid, radio = radios[0]
        if vif_uuid not in radio.get("vif_configs", []):
            raise EmosaError(Reason.NOT_READY, "VIF is not referenced by designated radio")
        states = [
            (u, r)
            for u, r in decoded.get("Wifi_VIF_State", {}).items()
            if r.get("vif_config") == vif_uuid and r.get("if_name") == self.if_name
        ]
        radio_states = [
            r
            for r in decoded.get("Wifi_Radio_State", {}).values()
            if r.get("radio_config") == radio_uuid
        ]
        state = states[0][1] if len(states) == 1 else {}
        if (
            len(radio_states) != 1
            or not states
            or states[0][0] not in radio_states[0].get("vif_states", [])
        ):
            state = {}
        return vif_uuid, radio_uuid, config, state, decoded

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            _, _, config, state, _ = self._binding(raw)
            self.last = Snapshot(
                self._values(config),
                Observation(
                    self.pod_id,
                    self.bss_id,
                    self._values(state),
                    "ovsdb",
                    self.mode,
                    raw["generation"],
                    utc_now(),
                    raw["ready"] and bool(state),
                    self.state_provenance,
                    revision=raw["revision"],
                ),
                raw["ready"] and bool(state),
                raw["generation"],
                raw["schema"].fingerprint,
            )
        except (EmosaError, ConnectionError, TimeoutError):
            if self.last is None:
                raise
            self.last.ready = False
            self.last.observed.fresh = False
        return self.last

    async def inventory(self):
        raw = await self.session.snapshot()
        _, radio_uuid, _, state, decoded = self._binding(raw)
        return {
            "pod_id": self.pod_id,
            "source": "OpenSync",
            "management_transport": "OVSDB",
            "backend_mode": self.mode,
            "schema_fingerprint": raw["schema"].fingerprint,
            "generation": raw["generation"],
            "revision": raw["revision"],
            "observed_at": utc_now(),
            "ready": raw["ready"] and bool(state),
            "device_identity": list(decoded.get("AWLAN_Node", {}).values()),
            "topology": {"physical_links": "unknown", "protocol_adjacency": "not_started"},
            "inventory_scope": "designated_existing_bss",
            "radios": [
                {
                    "radio_id": self.radio_id,
                    **{k: r.get(k) for k in ("if_name", "freq_band", "channel", "mac")},
                }
                for r in decoded.get("Wifi_Radio_State", {}).values()
                if r.get("radio_config") == radio_uuid
            ],
            "bsses": [
                {
                    "bss_id": self.bss_id,
                    "radio_id": self.radio_id,
                    **{k: state.get(k) for k in ("if_name", "mac", "ssid", "enabled", "mode")},
                }
            ],
            "clients": [
                {
                    "bss_id": self.bss_id,
                    "mac": r.get("mac"),
                    "state": r.get("state"),
                    "source_time": None,
                }
                for u, r in decoded.get("Wifi_Associated_Clients", {}).items()
                if u in state.get("associated_clients", [])
            ],
        }

    async def topology(self):
        if self.topology_binding is None:
            return unavailable(self.pod_id, None, "topology_binding_not_configured")
        try:
            raw = await self.session.snapshot()
        except (EmosaError, ConnectionError, TimeoutError) as exc:
            return unavailable(
                self.pod_id,
                self.topology_binding,
                exc.code.value if isinstance(exc, EmosaError) else "NOT_READY",
            )
        return project(
            self.pod_id, raw, self.topology_binding, state_provenance=self.state_provenance
        )

    async def plan(self, intent):
        intent.target(self.vault)
        if (intent.pod_id, intent.radio_id, intent.bss_id) != (
            self.pod_id,
            self.radio_id,
            self.bss_id,
        ):
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "request exceeds designated existing AP scope"
            )
        raw = await self.session.snapshot()
        _, _, config, state, _ = self._binding(raw)
        if not raw["ready"] or not state:
            raise EmosaError(Reason.NOT_READY, "complete current references/state required")
        values = self._values(config)
        if (
            values["mode"] != "ap"
            or values["enabled"] is not True
            or values["security_mode"] != "wpa2-psk"
        ):
            raise EmosaError(
                Reason.UNSUPPORTED_OPERATION, "current VIF/security representation unqualified"
            )
        if "key" not in config.get("wpa_psks", {}):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "designated existing PSK key absent")
        return {
            "mapping": "synthetic-existing-bss-v1",
            "radio_id": self.radio_id,
            "bss_id": self.bss_id,
            "fields": ["ssid", "wpa_psks[key]"],
            "ssid": intent.ssid,
            "secret_ref": intent.secret_ref,
            "guard": "VIF fields, security and radio/VIF references",
            "shared_radio_actuation": False,
            "mapping_scope": self.mapping_scope,
        }

    async def radio_scope(self):
        raw = await self.session.snapshot()
        report = assess(
            decode_rows(raw),
            if_name=self.if_name,
            radio_name=self.radio_name,
            ready=raw["ready"],
            credentials_available=True,
        )
        nodes = decode_rows(raw).get("AWLAN_Node", {})
        if (
            self.expected_serial is None
            or len(nodes) != 1
            or next(iter(nodes.values())).get("serial_number") != self.expected_serial
        ):
            report["blockers"].append("configured_serial_not_matched")
            report["synthetic_mapping_candidate"] = False
        return {
            **report,
            "pod_id": self.pod_id,
            "generation": raw["generation"],
            "schema_fingerprint": raw["schema"].fingerprint,
            "mapping_scope": self.mapping_scope,
        }

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        vif_uuid, radio_uuid, _, _, _ = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        vif = raw["tables"]["Wifi_VIF_Config"][vif_uuid]
        radio = raw["tables"]["Wifi_Radio_Config"][radio_uuid]
        guards = [
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
        ]
        transaction = [
            guard("Wifi_Radio_Config", radio_uuid, radio, ["if_name", "vif_configs"]),
            guard("Wifi_VIF_Config", vif_uuid, vif, guards),
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
                    ["wpa_psks", "delete", ["set", ["key"]]],
                    [
                        "wpa_psks",
                        "insert",
                        ["map", [["key", self.vault.resolve(intent.secret_ref)]]],
                    ],
                ],
            },
        ]
        if self.mapping_scope == "sole-fronthaul-radio":
            transaction[0:0] = transaction_guards(raw)
        if self.expected_serial is not None:
            # Guard the complete identity set atomically with Config changes. A different
            # or additional node arriving after planning must not inherit write authority.
            transaction.insert(
                0,
                {
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
                },
            )
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
            check_results(results, [None] * (len(transaction) - 2) + [1, 1])
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
                "results": results,
            },
        )

    async def close(self):
        await self.session.close()

    async def status(self):
        snap = await self.snapshot()
        return {
            "pod_id": self.pod_id,
            "backend_mode": self.mode,
            "source": "OpenSync",
            "management_backend": "opensync_ovsdb",
            "ready": snap.ready,
            "observation": asdict(snap.observed),
            "protocol_state": "blocked_P0",
        }
