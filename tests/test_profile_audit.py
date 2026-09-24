import copy
import json
import os
from pathlib import Path

import pytest

from emosa.errors import EmosaError
from emosa_lab.evaluation.cli import main
from emosa_lab.evaluation.profile_audit import (
    audit,
    read_features,
    unknown_features,
    validate_features,
)

pytestmark = pytest.mark.unit


def test_unknown_input_does_not_become_no_feature_support_or_profile_qualification():
    result = audit()
    assert result["default_unknown_radio"]
    assert (
        not result["profile_advertisement_ready"] and result["qualified_easymesh_profile"] is None
    )
    assert not result["full_clause_audit_complete"]
    assert result["counter_units"]["decision"] == "unresolved"
    obligations = result["ap_capability_report"]["obligations"]
    assert all(
        r["decision"] == "unresolved"
        for r in obligations
        if r["type"] in {"0x86", "0x87", "0x88", "0xaa", "0xbe", "0xdf", "0xe7"}
    )
    assert not any(r["qualified_value_available"] for r in obligations)
    assert {r["id"] for r in result["requirements"]} >= {
        "channel_selection",
        "steering",
        "metrics",
        "client_capability",
        "backhaul",
        "four_address",
    }


@pytest.mark.parametrize("support", [True, False, None])
def test_he_always_controls_both_he_and_wifi6_requirements_and_never_m1_companions(support):
    value = unknown_features()
    value["radios"][0]["he"] = support
    value["radios"][0]["advanced_qos"] = False
    result = audit(value)
    rows = result["ap_capability_report"]["obligations"]
    expected = (
        "required"
        if support is True
        else "omit_if_unsupported"
        if support is False
        else "unresolved"
    )
    assert [r["decision"] for r in rows if r["type"] in {"0x88", "0xaa"}] == [expected, expected]
    assert next(r for r in rows if r["type"] == "0xbe")["decision"] == "omit_if_unsupported"
    assert result["wsc_m1_companions"]["required_per_radio"] == [
        "0x85",
        "0x11 (WSC M1)",
        "0xb4",
        "0xbe",
    ]
    assert not result["wsc_m1_companions"]["ready"]


@pytest.mark.parametrize(
    "left,right,expected",
    [
        (True, None, "required"),
        (False, None, "unresolved"),
        (False, False, "omit_if_unsupported"),
        (None, None, "unresolved"),
    ],
)
def test_eht_is_agent_wide_if_any_radio_supports_it(left, right, expected):
    value = unknown_features()
    value["radios"][0]["eht"] = left
    other = copy.deepcopy(value["radios"][0])
    other.update(radio_id="second", eht=right)
    value["radios"].append(other)
    result = audit(value)
    assert [
        r["decision"]
        for r in result["ap_capability_report"]["obligations"]
        if r["type"] in {"0xdf", "0xe7"}
    ] == [expected, expected]


def test_all_declared_features_still_cannot_pass_a_profile():
    value = unknown_features()
    for key in value["radios"][0]:
        if key != "radio_id":
            value["radios"][0][key] = True
    value["controller"].update(profile=2, kib_mib=True, preferred_scaled_unit=2)
    result = audit(value)
    assert result["counter_units"]["unit"] == "MiB"
    assert not result["ap_capability_report"]["ready"] and not result["profile_advertisement_ready"]
    assert not result["controller_onboarding_proven"] and not result["physical_pod_proven"]
    rows = result["ap_capability_report"]["obligations"]
    assert {r["type"] for r in rows if r["decision"] == "applicability_review_pending"} == {
        "0xcc",
        "0xa5",
        "0xa9",
        "0xb2",
    }


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "missing",
        "string",
        "extra",
        "claim",
        "empty",
        "many",
        "markup",
        "float-profile",
        "bool-profile",
        "float-unit",
    ],
)
def test_malformed_or_qualification_claim_inputs_are_rejected(fault):
    value = unknown_features()
    if fault == "duplicate":
        value["radios"].append(copy.deepcopy(value["radios"][0]))
    elif fault == "missing":
        del value["radios"][0]["he"]
    elif fault == "string":
        value["radios"][0]["ht"] = "false"
    elif fault == "extra":
        value["qualified"] = True
    elif fault == "claim":
        value["scope"] = "qualified"
    elif fault == "empty":
        value["radios"] = []
    elif fault == "many":
        value["radios"] *= 9
    elif fault == "markup":
        value["radios"][0]["radio_id"] = "<script>|table"
    elif fault == "float-profile":
        value["controller"]["profile"] = 1.0
    elif fault == "bool-profile":
        value["controller"]["profile"] = True
    elif fault == "float-unit":
        value["controller"]["preferred_scaled_unit"] = 1.0
    with pytest.raises(EmosaError):
        validate_features(value)


@pytest.mark.parametrize("fault", ["json", "duplicate", "oversize", "fifo"])
def test_bounded_regular_input_files_and_private_errors(tmp_path, fault):
    path = tmp_path / "private-file.json"
    if fault == "json":
        path.write_text("never-echo-private-text")
    elif fault == "duplicate":
        path.write_text('{"never-echo-private-text":1,"never-echo-private-text":2}')
    elif fault == "oversize":
        path.write_bytes(b"x" * 16_385)
    else:
        os.mkfifo(path)
    with pytest.raises(EmosaError) as error:
        read_features(path)
    assert "never-echo-private-text" not in json.dumps(error.value.public())


def test_examples_cli_exit_codes_and_offline_execution(tmp_path, capsys, monkeypatch):
    for file in Path("examples/protocol").glob("profile-features.*.json"):
        value = read_features(file)
        assert not audit(value)["profile_advertisement_ready"]
    example = Path("examples/protocol/profile-features.synthetic.json").resolve()
    monkeypatch.chdir(tmp_path)
    assert main(["profile-audit"]) == 5
    result = json.loads(capsys.readouterr().out)
    assert result["default_unknown_radio"]
    assert main(["profile-audit", "--features", str(example), "--format", "markdown"]) == 5
    text = capsys.readouterr().out
    assert "**blocked**" in text and "radio-2" in text and '"unit": "bytes"' in text
    assert main(["--execution", "lxd", "profile-audit"]) == 2
    capsys.readouterr()
    assert list(tmp_path.iterdir()) == []
