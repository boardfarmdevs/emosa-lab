import copy
import json

import pytest
from test_radio_capabilities import NOW, inputs, mapped, save

from emosa.radio_capabilities import InputUnavailable, load_inputs
from emosa.simulation.radio_capabilities import fixture_profile

pytestmark = pytest.mark.unit


def extended(tmp_path):
    binding, raw, _, _ = inputs(tmp_path)
    profile, evidence = fixture_profile(
        binding, raw["schema"].fingerprint, now=NOW, with_extensions=True
    )
    (tmp_path / profile["evidence"][0]["file"]).write_bytes(evidence)
    ref = save(tmp_path, profile)
    return binding, raw, load_inputs(ref), ref


def test_normalized_claims_map_to_asymmetric_exact_values_and_bound_inventory(tmp_path):
    b, raw, p, ref = extended(tmp_path)
    result = mapped(b, raw, p, ref)
    ext = result["extensions"]
    assert result["ready"] and all(v["ready"] for v in ext.values())
    assert [v["value_hex"] for v in ext["technology"]["values"]] == [
        "0200000140015e",
        "020000014001fff6ffc92a20",
        "0200000140025e",
    ]
    inventory = ext["device_inventory"]["values"][0]["decoded"]
    assert bytes.fromhex(inventory["serial_number_hex"]).decode() == b["expected_serial"]
    assert bytes.fromhex(inventory["software_version_hex"]).decode() == "simulation-only"
    assert len(inventory["radios"]) == 2
    assert not result["complete_ap_capability_report"] and not result["physical_pod_proven"]
    assert p["extensions"]["technology"][0]["vht"]["rx_mcs_codes"] == [1, 2, 0, 3, 3, 3, 3, 3]
    assert str(tmp_path) not in json.dumps(result)
    # Current channel/SSID observations cannot rewrite supported technology facts.
    raw["tables"]["Wifi_Radio_State"]["lab-radio"]["channel"] = 40
    assert mapped(b, raw, p, ref)["extensions"] == ext


def test_old_contract_is_unknown_and_cannot_advertise_unsupported_technology(tmp_path):
    b, raw, p, ref = inputs(tmp_path)
    result = mapped(b, raw, p, ref)
    assert result["ready"]
    assert result["extensions"]["technology"]["blockers"] == ["technology_input_not_configured"]
    assert result["extensions"]["device_inventory"]["blockers"] == [
        "device_inventory_input_not_configured"
    ]


@pytest.mark.parametrize(
    "fault",
    [
        "unknown-ht",
        "unknown-vht",
        "unknown-he",
        "he",
        "missing-radio",
        "duplicate-radio",
        "extra-radio",
        "evidence",
        "scope",
        "tx-float",
        "rx-float",
        "mcs-float",
        "mcs-streams",
        "ht-width",
        "vht-width",
    ],
)
def test_technology_fault_withdraws_all_technology_values_only(tmp_path, fault):
    b, raw, p, ref = extended(tmp_path)
    rows = p["extensions"]["technology"]
    first = rows[0]
    if fault.startswith("unknown-"):
        first[fault.split("-")[1]] = None
    elif fault == "he":
        first["he"] = True
    elif fault == "missing-radio":
        rows.pop()
    elif fault == "duplicate-radio":
        rows.append(copy.deepcopy(first))
    elif fault == "extra-radio":
        rows[1]["radio_id"] = "unknown"
    elif fault == "evidence":
        first["evidence_id"] = "absent"
    elif fault == "scope":
        p["evidence"][0]["covers"].remove("technology")
    elif fault == "tx-float":
        first["ht"]["max_tx_streams"] = 2.0
    elif fault == "rx-float":
        first["vht"]["max_rx_streams"] = 3.0
    elif fault == "mcs-float":
        first["vht"]["tx_mcs_codes"][0] = 2.0
    elif fault == "mcs-streams":
        first["vht"]["tx_mcs_codes"][7] = 0
    elif fault == "ht-width":
        first["ht"]["ht40"] = False
    elif fault == "vht-width":
        first["vht"]["short_gi_160"] = True
    ref = save(tmp_path, p)
    result = mapped(b, raw, load_inputs(ref), ref)
    assert result["ready"] and result["extensions"]["device_inventory"]["ready"]
    assert (
        not result["extensions"]["technology"]["ready"]
        and result["extensions"]["technology"]["values"] == []
    )
    assert result["extensions"]["technology"]["blockers"]


@pytest.mark.parametrize(
    "fault", ["serial", "firmware", "utf8-length", "surrogate", "radio", "duplicate-radio", "scope"]
)
def test_inventory_fault_does_not_guess_or_truncate_strings(tmp_path, fault):
    b, raw, p, ref = extended(tmp_path)
    d = p["extensions"]["device_inventory"]
    if fault == "serial":
        d["serial_number"] = "different"
    elif fault == "firmware":
        d["software_version"] = "different"
    elif fault == "utf8-length":
        d["execution_env"] = "é" * 33
    elif fault == "surrogate":
        d["execution_env"] = "\ud800"
    elif fault == "radio":
        d["radios"].pop()
    elif fault == "duplicate-radio":
        d["radios"].append(copy.deepcopy(d["radios"][0]))
    elif fault == "scope":
        p["evidence"][0]["covers"].remove("device_inventory")
    ref = save(tmp_path, p)
    result = mapped(b, raw, load_inputs(ref), ref)
    assert result["ready"] and result["extensions"]["technology"]["ready"]
    assert (
        not result["extensions"]["device_inventory"]["ready"]
        and result["extensions"]["device_inventory"]["values"] == []
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing-support",
        "string-bool",
        "true-without-fields",
        "unknown-field",
        "missing-vendor",
        "empty-serial",
        "bool-streams",
        "mcs-range",
    ],
)
def test_invalid_extension_contracts_rejected_before_mapping(tmp_path, fault):
    _, _, p, _ = extended(tmp_path)
    t = p["extensions"]["technology"][0]
    d = p["extensions"]["device_inventory"]
    if fault == "missing-support":
        del t["he"]
    elif fault == "string-bool":
        t["he"] = "false"
    elif fault == "true-without-fields":
        t["ht"] = True
    elif fault == "unknown-field":
        t["qualified"] = True
    elif fault == "missing-vendor":
        del d["radios"][0]["chipset_vendor"]
    elif fault == "empty-serial":
        d["serial_number"] = ""
    elif fault == "bool-streams":
        t["ht"]["max_tx_streams"] = True
    elif fault == "mcs-range":
        t["vht"]["tx_mcs_codes"][0] = 4
    with pytest.raises(InputUnavailable):
        load_inputs(save(tmp_path, p))


def test_explicit_unsupported_has_no_values_and_stale_parent_withdraws_everything(tmp_path):
    b, raw, p, ref = extended(tmp_path)
    for row in p["extensions"]["technology"]:
        row.update(ht=False, vht=False, he=False)
    ref = save(tmp_path, p)
    result = mapped(b, raw, load_inputs(ref), ref)
    assert result["extensions"]["technology"] == {"ready": True, "blockers": [], "values": []}
    raw["ready"] = False
    result = mapped(b, raw, p, ref)
    assert not result["ready"]
    assert all(not e["ready"] and not e["values"] for e in result["extensions"].values())


@pytest.mark.parametrize(
    "fault", ["missing-ht40", "missing-vht80", "vht160", "vht8080", "center-as-primary"]
)
def test_width_claims_need_audited_classes_and_center_is_not_a_primary_channel(tmp_path, fault):
    b, raw, p, ref = extended(tmp_path)
    ops = p["radios"][0]["operating_classes"]
    if fault == "missing-ht40":
        p["radios"][0]["operating_classes"] = [
            o for o in ops if o["operating_class"] not in (116, 117)
        ]
    elif fault == "missing-vht80":
        p["radios"][0]["operating_classes"] = [o for o in ops if o["operating_class"] != 128]
    elif fault in ("vht160", "vht8080"):
        p["extensions"]["technology"][0]["vht"][fault] = True
    else:
        raw["tables"]["Wifi_Radio_State"]["lab-radio"]["channel"] = 42
    ref = save(tmp_path, p)
    result = mapped(b, raw, load_inputs(ref), ref)
    assert not result["extensions"]["technology"]["ready"]
    assert not result["extensions"]["technology"]["values"]
    if fault == "center-as-primary":
        assert not result["ready"] and result["blockers"] == [
            "observed_channel_not_supported_by_input"
        ]
