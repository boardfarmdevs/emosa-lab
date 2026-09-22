"""Conservative sole-BSS checks; neither a physical qualification nor WSC authority."""

from emosa.errors import EmosaError, Reason

TOPOLOGY_COLUMNS = {
    "Wifi_Radio_Config": ["if_name", "freq_band", "enabled", "vif_configs"],
    "Wifi_Radio_State": ["if_name", "radio_config", "vif_states", "freq_band", "mac"],
    "Wifi_VIF_Config": ["if_name", "mode", "enabled", "multi_ap", "bridge"],
    "Wifi_VIF_State": ["if_name", "vif_config", "mode", "enabled", "mac"],
}
SECURITY_COLUMNS = [
    "wpa",
    "wpa_key_mgmt",
    "wpa_psks",
    "security",
    "rsn_pairwise_ccmp",
    "wpa_pairwise_tkip",
    "wpa_pairwise_ccmp",
]


def assess(rows, *, if_name, radio_name, ready, credentials_available=False):
    """Assess the observed graph without returning SSIDs, keys or fingerprints.

    Orphan and shared references anywhere in this database conservatively block
    the trial: their radio membership cannot be safely inferred. A second fully
    separate radio is allowed. Physical manager/management-path ownership remains
    external evidence even if every database reference is consistent.
    """
    blockers = []

    def require(condition, code):
        if not condition and code not in blockers:
            blockers.append(code)

    require(ready, "snapshot_not_fresh")
    require(all(table in rows for table in TOPOLOGY_COLUMNS), "missing_inventory_table")
    radios = rows.get("Wifi_Radio_Config", {})
    rstates = rows.get("Wifi_Radio_State", {})
    vifs = rows.get("Wifi_VIF_Config", {})
    vstates = rows.get("Wifi_VIF_State", {})
    chosen_radios = [(u, r) for u, r in radios.items() if r.get("if_name") == radio_name]
    chosen_vifs = [(u, v) for u, v in vifs.items() if v.get("if_name") == if_name]
    require(len(chosen_radios) == 1, "radio_binding_not_unique")
    require(len(chosen_vifs) == 1, "vif_binding_not_unique")
    owners = {u: [r for r, row in radios.items() if u in row.get("vif_configs", [])] for u in vifs}
    for radio in radios.values():
        require(all(u in vifs for u in radio.get("vif_configs", [])), "dangling_config_reference")
    require(all(len(value) == 1 for value in owners.values()), "orphan_or_shared_config")
    for rstate in rstates.values():
        require(rstate.get("radio_config") in radios, "orphan_radio_state")
        require(all(u in vstates for u in rstate.get("vif_states", [])), "dangling_state_reference")
    for uid, vstate in vstates.items():
        config_id = vstate.get("vif_config")
        state_owners = [r for r in rstates.values() if uid in r.get("vif_states", [])]
        require(config_id in vifs and len(state_owners) == 1, "orphan_or_shared_state")
        if config_id in vifs and len(state_owners) == 1:
            require(
                owners[config_id] == [state_owners[0].get("radio_config")],
                "config_state_radio_disagreement",
            )
    binding = None
    selected_state = None
    selected_config = None
    if len(chosen_radios) == len(chosen_vifs) == 1:
        radio_id, radio = chosen_radios[0]
        vif_id, selected_config = chosen_vifs[0]
        binding = {
            "radio_config_uuid": radio_id,
            "vif_config_uuid": vif_id,
            "radio_name": radio_name,
            "if_name": if_name,
        }
        require(radio.get("vif_configs") == [vif_id], "radio_is_not_sole_existing_bss")
        require(radio.get("enabled") is True, "radio_not_enabled")
        require(
            selected_config.get("mode") == "ap" and selected_config.get("enabled") is True,
            "vif_not_enabled_ap",
        )
        # Explicit "none" means an ordinary AP without a native Multi-AP role.
        # Missing metadata is unknown. EMOSA supplies the virtual agent role externally.
        require(selected_config.get("multi_ap") == "none", "ordinary_ap_role_not_explicit")
        states = [(u, s) for u, s in vstates.items() if s.get("vif_config") == vif_id]
        radio_states = [s for s in rstates.values() if s.get("radio_config") == radio_id]
        require(len(states) == 1 and len(radio_states) == 1, "state_binding_not_unique")
        if len(states) == len(radio_states) == 1:
            state_id, selected_state = states[0]
            rs = radio_states[0]
            require(rs.get("vif_states") == [state_id], "radio_state_is_not_sole_bss")
            require(
                rs.get("if_name") == radio_name and rs.get("freq_band") == radio.get("freq_band"),
                "radio_state_mismatch",
            )
            require(
                selected_state.get("if_name") == if_name
                and selected_state.get("mode") == "ap"
                and selected_state.get("enabled") is True,
                "ap_state_unavailable",
            )
    topology_blockers = list(blockers)
    credential_status = "not_collected"
    if credentials_available:
        credential_status = "unsupported"
        supported = selected_config is not None and selected_state is not None
        for row in (selected_config, selected_state):
            supported = (
                supported
                and row is not None
                and (
                    row.get("wpa") is True
                    and row.get("wpa_key_mgmt") == ["wpa2-psk"]
                    and row.get("rsn_pairwise_ccmp") is True
                    and row.get("wpa_pairwise_tkip") is False
                    and row.get("wpa_pairwise_ccmp") is False
                    and row.get("security") == {}
                    and set(row.get("wpa_psks", {})) == {"key"}
                )
            )
        require(supported, "credential_layout_not_single_wpa2_psk")
        if supported:
            credential_status = "single_wpa2_psk_observed"
    else:
        blockers.append("credential_layout_not_collected")
    return {
        "schema_version": 1,
        "scope": "sole-fronthaul-radio",
        "topology_candidate": not topology_blockers,
        "synthetic_mapping_candidate": not blockers,
        "credential_layout": credential_status,
        "binding": binding,
        "blockers": blockers,
        "physical_qualification": False,
        "wire_admission": False,
        "unresolved": [
            "controller/exchange authentication and complete CMDU semantics",
            "physical identity, firmware and manager behavior",
            "exclusive writers, management/backhaul dependencies and recovery",
            "actual radio capabilities; observed BSS count is not Max_BSS",
        ],
    }


def decode_rows(raw):
    return {
        table: {uid: raw["schema"].row(table, row) for uid, row in entries.items()}
        for table, entries in raw["tables"].items()
    }


def require_synthetic_scope(raw, *, if_name, radio_name):
    report = assess(
        decode_rows(raw),
        if_name=if_name,
        radio_name=radio_name,
        ready=raw["ready"],
        credentials_available=True,
    )
    if not report["synthetic_mapping_candidate"]:
        raise EmosaError(
            Reason.PRECONDITION_FAILED,
            "sole-radio simulation scope is not established",
            blockers=report["blockers"],
        )
    return report


def transaction_guards(raw):
    """Guard complete graph membership, including row insertion/deletion/recreation.

    Matching a chosen row alone misses a concurrent extra/orphan BSS. UUIDs are
    included; otherwise replacement by an equal-looking row could escape a wait.
    No State writes or locks are issued. Failure aborts the whole transaction.
    """
    guards = []
    for table, fields in TOPOLOGY_COLUMNS.items():
        columns = ["_uuid", *fields]
        if table == "Wifi_VIF_State":
            columns += SECURITY_COLUMNS
        guards.append(
            {
                "op": "wait",
                "table": table,
                "where": [],
                "columns": columns,
                "until": "==",
                "timeout": 0,
                "rows": [
                    {"_uuid": ["uuid", uid], **{c: row[c] for c in columns if c != "_uuid"}}
                    for uid, row in raw["tables"][table].items()
                ],
            }
        )
    return guards
