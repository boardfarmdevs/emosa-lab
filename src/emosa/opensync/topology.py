"""Complete graph projection for explicitly bound upstream-schema simulations.

Config establishes references only. Radio/VIF operational facts come from State.
No IEEE envelope, capability inference, Config/State mutation or wire admission.
"""

import hashlib
import re

from emosa.clock import utc_now
from emosa.easymesh_payloads import (
    APOperationalBss,
    OperationalBss,
    OperationalRadio,
    encode_value,
)
from emosa.errors import EmosaError
from emosa.topology_bindings import binding_digest

COLUMNS = {
    "AWLAN_Node": ["serial_number"],
    "Wifi_Radio_Config": ["if_name", "vif_configs"],
    "Wifi_Radio_State": [
        "if_name",
        "radio_config",
        "vif_states",
        "enabled",
        "mac",
        "freq_band",
        "channel",
    ],
    "Wifi_VIF_Config": ["if_name"],
    "Wifi_VIF_State": ["if_name", "vif_config", "mode", "enabled", "mac", "ssid"],
}


class _Unavailable(Exception):
    pass


def _require(condition, code):
    if not condition:
        raise _Unavailable(code)


def _mac(value):
    _require(isinstance(value, str), "observed_mac_missing")
    value = value.lower()
    _require(
        re.fullmatch(r"[0-9a-f][02468ace](?::[0-9a-f]{2}){5}", value)
        and value != "00:00:00:00:00:00",
        "observed_mac_invalid",
    )
    return value


def unavailable(pod_id, binding, code, *, raw=None):
    return {
        "schema_version": 1,
        "pod_id": pod_id,
        "interface": "local-diagnostic",
        "backend_mode": "ovsdb-sim",
        "scope": "complete configured radio/VIF graph; AP operational value only",
        "binding_sha256": binding_digest(binding) if binding else None,
        "snapshot_fresh": bool(raw and raw["ready"]),
        "generation": raw["generation"] if raw else None,
        "revision": raw["revision"] if raw else None,
        "schema_fingerprint": raw["schema"].fingerprint if raw else None,
        "observed_at": utc_now() if raw else None,
        "source_time": None,
        "ready": False,
        "complete": False,
        "blockers": [code],
        "radios": [],
        "operational_bss_value": None,
        "physical_links": "unknown",
        "protocol_adjacency": "not_started",
        "capabilities": "not_inferred",
        "mld_semantics": "not_qualified",
        "easymesh_wire_state": "blocked_P0",
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
    }


def project(pod_id, raw, binding, *, state_provenance):
    report = unavailable(pod_id, binding, "topology_binding_not_configured", raw=raw)
    if binding is None:
        return report
    try:
        _require(binding["pod_id"] == pod_id, "pod_binding_mismatch")
        _require(raw["ready"], "snapshot_not_fresh")
        _require(all(t in raw["tables"] for t in COLUMNS), "inventory_table_missing")
        # Only decode noncredential fields needed for this projection.
        rows = {
            t: {
                u: raw["schema"].row(t, {k: v for k, v in r.items() if k in cols})
                for u, r in raw["tables"][t].items()
            }
            for t, cols in COLUMNS.items()
        }
        nodes = list(rows["AWLAN_Node"].values())
        _require(
            len(nodes) == 1 and nodes[0].get("serial_number") == binding["expected_serial"],
            "configured_serial_not_matched",
        )
        radios, value_radios = _project_graph(rows, binding)
        value = encode_value(APOperationalBss(tuple(value_radios)))
    except _Unavailable as exc:
        report["blockers"] = [str(exc)]
        return report
    except EmosaError:
        report["blockers"] = ["payload_component_rejected"]
        return report
    report.update(
        ready=True,
        complete=True,
        blockers=[],
        al_mac=binding["al_mac"],
        identity_source="persisted explicit simulation binding; serial is not attestation",
        state_provenance=state_provenance,
        ssid_encoding="synthetic UTF-8 OVSDB string mapping",
        radios=radios,
        operational_bss_value={
            "type": "0x83",
            "value_hex": value.hex(),
            "sha256": hashlib.sha256(value).hexdigest(),
        },
    )
    return report


def _unique_by_name(rows):
    result = {}
    for uid, row in rows.items():
        name = row.get("if_name")
        _require(
            isinstance(name, str) and name not in result, "interface_name_missing_or_ambiguous"
        )
        result[name] = (uid, row)
    return result


def _states_by_config(rows, configs, column):
    result = {}
    for uid, row in rows.items():
        ref = row.get(column)
        _require(ref in configs and ref not in result, "state_reference_missing_or_ambiguous")
        result[ref] = (uid, row)
    _require(set(result) == set(configs), "state_inventory_incomplete")
    return result


def _project_graph(rows, binding):
    rc, vc = rows["Wifi_Radio_Config"], rows["Wifi_VIF_Config"]
    rs, vs = rows["Wifi_Radio_State"], rows["Wifi_VIF_State"]
    radio_names, vif_names = _unique_by_name(rc), _unique_by_name(vc)
    expected = binding["topology"]["radios"]
    _require(
        set(radio_names) == {r["if_name"] for r in expected}, "radio_inventory_differs_from_binding"
    )
    _require(
        set(vif_names) == {v["if_name"] for r in expected for v in r["interfaces"]},
        "vif_inventory_differs_from_binding",
    )
    radio_states = _states_by_config(rs, rc, "radio_config")
    vif_states = _states_by_config(vs, vc, "vif_config")
    radios, value_radios = [], []
    for radio in expected:
        rc_id, config = radio_names[radio["if_name"]]
        _, state = radio_states[rc_id]
        _require(state.get("if_name") == radio["if_name"], "radio_state_name_mismatch")
        _require(_mac(state.get("mac")) == radio["expected_mac"], "radio_mac_mismatch")
        _require(type(state.get("enabled")) is bool, "radio_operational_state_unknown")
        vif_ids = {vif_names[v["if_name"]][0] for v in radio["interfaces"]}
        _require(set(config.get("vif_configs", [])) == vif_ids, "config_radio_membership_mismatch")
        _require(
            set(state.get("vif_states", [])) == {vif_states[u][0] for u in vif_ids},
            "state_radio_membership_mismatch",
        )
        interfaces, bsses = [], []
        for interface in radio["interfaces"]:
            vc_id, _ = vif_names[interface["if_name"]]
            _, observed = vif_states[vc_id]
            _require(observed.get("if_name") == interface["if_name"], "vif_state_name_mismatch")
            _require(observed.get("mode") == interface["mode"], "vif_mode_mismatch")
            _require(type(observed.get("enabled")) is bool, "vif_operational_state_unknown")
            mac = _mac(observed.get("mac"))
            _require(mac == interface["expected_mac"], "vif_mac_mismatch")
            _require(not observed["enabled"] or state["enabled"], "enabled_vif_on_disabled_radio")
            item = {
                "interface_id": interface["interface_id"],
                "if_name": interface["if_name"],
                "mode": observed["mode"],
                "enabled": observed["enabled"],
                "observed_mac": mac,
            }
            if interface["mode"] == "ap":
                item["bss_id"] = interface["interface_id"]
                if observed["enabled"]:
                    ssid = observed.get("ssid")
                    _require(isinstance(ssid, str), "operational_ssid_unknown")
                    try:
                        ssid_bytes = ssid.encode("utf-8")
                    except UnicodeError:
                        raise _Unavailable("ssid_representation_unsupported") from None
                    _require(1 <= len(ssid_bytes) <= 32, "ssid_representation_unsupported")
                    item["ssid_hex"] = ssid_bytes.hex()
                    bsses.append(OperationalBss(bytes.fromhex(mac.replace(":", "")), ssid_bytes))
            interfaces.append(item)
        radios.append(
            {
                "radio_id": radio["radio_id"],
                "if_name": radio["if_name"],
                "ruid": radio["ruid"],
                "observed_mac": state["mac"].lower(),
                "enabled": state["enabled"],
                "freq_band": state.get("freq_band"),
                "channel": state.get("channel"),
                "interfaces": interfaces,
            }
        )
        value_radios.append(
            OperationalRadio(bytes.fromhex(radio["ruid"].replace(":", "")), tuple(bsses))
        )
    return radios, value_radios
