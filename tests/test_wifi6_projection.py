import copy
import json
from datetime import timedelta

import pytest

from emosa.easymesh_payloads import decode_value
from emosa.radio_capabilities import InputUnavailable, load_inputs
from emosa_lab.simulation.radio_capabilities import fixture_profile
from test_radio_capabilities import NOW, inputs, mapped, save

pytestmark = pytest.mark.unit


def wifi6_inputs(tmp_path):
    binding, raw, _, _ = inputs(tmp_path)
    profile, evidence = fixture_profile(
        binding, raw["schema"].fingerprint, now=NOW, with_wifi6=True
    )
    (tmp_path / profile["evidence"][0]["file"]).write_bytes(evidence)
    ref = save(tmp_path, profile)
    return binding, raw, load_inputs(ref), ref


def test_per_role_values_have_exact_asymmetric_maps_without_complete_he_readiness(tmp_path):
    b, raw, p, ref = wifi6_inputs(tmp_path)
    result = mapped(b, raw, p, ref)
    assert result["ready"] and result["extensions"]["wifi6"]["ready"]
    assert result["extensions"]["device_inventory"]["ready"]
    assert result["extensions"]["technology"] == {
        "ready": False,
        "blockers": ["he_and_wifi6_mapping_pending"],
        "values": [],
    }
    values = result["extensions"]["wifi6"]["values"]
    # Hand-derived Table 95 values: maps c6ff/f9ff (AP Rx/Tx) and
    # fdff/feff (STA Rx/Tx). No byte sequence comes from this encoder.
    assert [v["value_hex"] for v in values] == [
        "0200000140010104c6fff9ffa7231209c4",
        "0200000140020204c6fff9ffa7231209c444fdfffeff47000000c8",
    ]
    assert [v["type"] for v in values] == ["0xaa", "0xaa"]
    value = decode_value(0xAA, bytes.fromhex(values[1]["value_hex"]))
    assert [r.role for r in value.roles] == [0, 1]
    assert value.roles[0].mcs.up_to_80.rx == (2, 1, 0, 3, 3, 3, 3, 3)
    assert value.roles[1].mcs.up_to_80.tx == (2, 3, 3, 3, 3, 3, 3, 3)
    assert value.roles[1].mu_mimo_users == value.roles[1].max_dl_ofdma_tx == 0
    assert (
        not result["complete_ap_capability_report"] and not result["controller_onboarding_proven"]
    )
    assert not result["physical_pod_proven"] and result["qualified_easymesh_profile"] is None
    assert str(tmp_path) not in json.dumps(result)
    assert "initial-simulation-key" not in json.dumps(result)
    # Role order and current channel do not determine capability map direction.
    p["extensions"]["wifi6"][1]["roles"].reverse()
    raw["tables"]["Wifi_Radio_State"]["lab-radio"]["channel"] = 40
    assert mapped(b, raw, p, ref)["extensions"]["wifi6"]["values"] == values


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("missing-input", "wifi6_input_not_configured"),
        ("missing-radio", "wifi6_radio_inventory_mismatch"),
        ("duplicate-radio", "wifi6_radio_inventory_mismatch"),
        ("unknown-radio", "wifi6_radio_inventory_mismatch"),
        ("he-unknown", "wifi6_he_support_unknown"),
        ("he-false", "wifi6_radio_inventory_mismatch"),
        ("evidence", "extension_evidence_scope_missing"),
        ("scope", "extension_evidence_scope_missing"),
        ("incomplete-roles", "wifi6_role_inventory_incomplete"),
        ("duplicate-role", "wifi6_duplicate_role"),
        ("missing-ap", "wifi6_observed_ap_role_missing"),
        ("missing-sta", "wifi6_observed_sta_role_missing"),
        ("wider160", "wifi6_wider_context_review_pending"),
        ("wider8080", "wifi6_wider_context_review_pending"),
        ("wide-beamformee", "wifi6_width_feature_mismatch"),
        ("spatial-reuse", "wifi6_adapter_feature_unimplemented"),
        ("acu", "wifi6_adapter_feature_unimplemented"),
        ("rx-unsupported", "wifi6_direction_has_no_supported_nss"),
        ("tx-unsupported", "wifi6_direction_has_no_supported_nss"),
        ("ap-limit-without-feature", "wifi6_ap_limit_feature_mismatch"),
        ("ap-feature-without-limit", "wifi6_ap_limit_feature_mismatch"),
        ("sta-ap-limit", "wifi6_sta_ap_limits_not_zero"),
        ("float-mcs", "invalid_wifi6_mcs_codes"),
        ("float-limit", "invalid_wifi6_user_limit"),
    ],
)
def test_one_bad_radio_withdraws_entire_wifi6_section_without_inventing_capabilities(
    tmp_path, fault, reason
):
    b, raw, p, ref = wifi6_inputs(tmp_path)
    rows = p["extensions"]["wifi6"]
    # Fault the last radio to ensure no value for the first leaks through.
    row = rows[-1]
    ap = row["roles"][0]
    if fault == "missing-input":
        del p["extensions"]["wifi6"]
    elif fault == "missing-radio":
        rows.pop()
    elif fault == "duplicate-radio":
        rows.append(copy.deepcopy(row))
    elif fault == "unknown-radio":
        row["radio_id"] = "unknown"
    elif fault == "he-unknown":
        p["extensions"]["technology"][-1]["he"] = None
    elif fault == "he-false":
        p["extensions"]["technology"][-1]["he"] = False
    elif fault == "evidence":
        row["evidence_id"] = "unknown"
    elif fault == "scope":
        p["evidence"][0]["covers"].remove("wifi6")
    elif fault == "incomplete-roles":
        row["complete_role_inventory"] = False
    elif fault == "duplicate-role":
        row["roles"] = [ap, copy.deepcopy(ap)]
    elif fault == "missing-ap":
        row["roles"] = [row["roles"][1]]
    elif fault == "missing-sta":
        row["roles"] = [ap]
    elif fault.startswith("wider"):
        ap["mcs"]["mhz160" if fault == "wider160" else "mhz80plus80"] = ap["mcs"]["up_to_80"]
    elif fault == "wide-beamformee":
        ap["features"]["beamformee_sts_gt_80"] = True
    elif fault in ("spatial-reuse", "acu"):
        ap["features"][
            "spatial_reuse" if fault == "spatial-reuse" else "anticipated_channel_usage"
        ] = True
    elif fault.endswith("-unsupported"):
        ap["mcs"]["up_to_80"][fault[:2] + "_codes"] = [3] * 8
    elif fault == "ap-limit-without-feature":
        ap["features"]["dl_ofdma"] = False
    elif fault == "ap-feature-without-limit":
        ap["ap_user_limits"]["ul_mu_mimo_rx"] = 0
    elif fault == "sta-ap-limit":
        row["roles"][1]["ap_user_limits"]["dl_ofdma_tx"] = 1
    elif fault == "float-mcs":
        ap["mcs"]["up_to_80"]["rx_codes"][0] = 2.0
    elif fault == "float-limit":
        ap["ap_user_limits"]["dl_ofdma_tx"] = 18.0
    ref = save(tmp_path, p)
    result = mapped(b, raw, load_inputs(ref), ref)
    assert result["ready"] and result["extensions"]["device_inventory"]["ready"]
    assert result["extensions"]["wifi6"] == {"ready": False, "blockers": [reason], "values": []}


@pytest.mark.parametrize(
    "fault",
    [
        "extra",
        "missing-feature",
        "unknown-feature",
        "string-feature",
        "null-feature",
        "bool-limit",
        "large-limit",
        "reserved-role",
        "empty-roles",
        "many-roles",
        "short-map",
        "long-map",
        "bad-code",
        "bool-code",
        "string-inventory",
    ],
)
def test_malformed_role_contract_never_reaches_mapper(tmp_path, fault):
    _, _, p, _ = wifi6_inputs(tmp_path)
    row = p["extensions"]["wifi6"][0]
    role = row["roles"][0]
    if fault == "extra":
        row["qualified"] = True
    elif fault == "missing-feature":
        del role["features"]["dl_ofdma"]
    elif fault == "unknown-feature":
        role["features"]["guessed_feature"] = True
    elif fault in ("string-feature", "null-feature"):
        role["features"]["dl_ofdma"] = "false" if fault == "string-feature" else None
    elif fault in ("bool-limit", "large-limit"):
        role["ap_user_limits"]["dl_mu_mimo_tx"] = True if fault == "bool-limit" else 16
    elif fault == "reserved-role":
        role["role"] = "reserved"
    elif fault == "empty-roles":
        row["roles"] = []
    elif fault == "many-roles":
        row["roles"] *= 3
    elif fault in ("short-map", "long-map"):
        role["mcs"]["up_to_80"]["rx_codes"] = [0] * (7 if fault == "short-map" else 9)
    elif fault in ("bad-code", "bool-code"):
        role["mcs"]["up_to_80"]["rx_codes"][0] = 4 if fault == "bad-code" else True
    elif fault == "string-inventory":
        row["complete_role_inventory"] = "true"
    with pytest.raises(InputUnavailable):
        load_inputs(save(tmp_path, p))


@pytest.mark.parametrize("fault", ["stale", "firmware", "country", "expired", "schema", "binding"])
def test_parent_context_failure_withdraws_all_wifi6_values(tmp_path, fault):
    b, raw, p, ref = wifi6_inputs(tmp_path)
    if fault == "stale":
        raw["ready"] = False
    elif fault == "firmware":
        raw["tables"]["AWLAN_Node"]["node"]["firmware_version"] = "changed"
    elif fault == "country":
        raw["tables"]["Wifi_Radio_State"]["lab-radio"]["country"] = "CA"
    elif fault == "expired":
        p["expires_at"] = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    elif fault == "schema":
        p["schema_fingerprint"] = "b" * 64
    else:
        p["binding_sha256"] = "b" * 64
    ref = save(tmp_path, p)
    result = mapped(b, raw, load_inputs(ref), ref)
    assert not result["ready"]
    assert all(
        not section["ready"] and not section["values"] for section in result["extensions"].values()
    )


def test_only_explicit_he_absence_can_produce_ready_empty_wifi6_section(tmp_path):
    b, raw, p, ref = wifi6_inputs(tmp_path)
    for row in p["extensions"]["technology"]:
        row["he"] = False
    del p["extensions"]["wifi6"]
    assert mapped(b, raw, p, ref)["extensions"]["wifi6"] == {
        "ready": True,
        "blockers": [],
        "values": [],
    }
    p["extensions"]["technology"][0]["he"] = None
    assert not mapped(b, raw, p, ref)["extensions"]["wifi6"]["ready"]
    del p["extensions"]["technology"]
    assert not mapped(b, raw, p, ref)["extensions"]["wifi6"]["ready"]
