import asyncio
import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from test_topology import config, observation

from emosa.agents import validate_bindings
from emosa.config import validate
from emosa.easymesh_payloads import (
    APRadioBasicCapabilities,
    BasicOperatingClass,
    decode_value,
    encode_value,
)
from emosa.errors import EmosaError
from emosa.opensync.mapping import OpenSyncBackend
from emosa.radio_capabilities import InputUnavailable, load_inputs, project
from emosa.simulation.radio_capabilities import fixture_profile

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)


def inputs(tmp_path):
    binding, raw = observation(tmp_path)
    raw["schema"].fingerprint = "a" * 64
    raw["tables"]["AWLAN_Node"]["node"].update(
        model="EMOSA synthetic extender", firmware_version="simulation-only"
    )
    for name, row in raw["tables"]["Wifi_Radio_State"].items():
        row.update(
            country="US",
            freq_band="5G" if name == "lab-radio" else "2.4G",
            channel=36 if name == "lab-radio" else 6,
        )
    profile, evidence = fixture_profile(binding, raw["schema"].fingerprint, now=NOW)
    (tmp_path / profile["evidence"][0]["file"]).write_bytes(evidence)
    reference = save(tmp_path, profile)
    return binding, raw, profile, reference


def save(tmp_path, profile):
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps(profile))
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def mapped(binding, raw, profile, reference):
    return project(
        "pod-1", raw, binding, profile, reference, state_provenance="unit-fixture", now=NOW
    )


def test_explicit_capacities_map_to_exact_bytes_without_inference(tmp_path):
    binding, raw, profile, reference = inputs(tmp_path)
    verified = load_inputs(reference)
    result = mapped(binding, raw, verified, reference)
    assert result["ready"] and result["snapshot_fresh"]
    assert [r["decoded"]["max_bss"] for r in result["radios"]] == [4, 2]
    assert [r["value"]["value_hex"] for r in result["radios"]] == [
        "0200000140010401731400",
        "02000001400202035111020c0d5311005411020c0d",
    ]
    assert not result["complete_ap_capability_report"]
    assert result["qualified_easymesh_profile"] is None
    assert not result["controller_onboarding_proven"] and not result["physical_pod_proven"]
    assert result["profile_sha256"] == reference["sha256"]
    # Operational channels and SSIDs can change without changing supported limits.
    raw["tables"]["Wifi_Radio_State"]["lab-radio"]["channel"] = 40
    raw["tables"]["Wifi_VIF_State"]["guest-ap"]["ssid"] = "changed-observed-SSID"
    assert mapped(binding, raw, profile, reference)["radios"] == result["radios"]
    encoded = json.dumps(result)
    assert str(tmp_path) not in encoded and "review_note" not in encoded
    assert "never-publish-private-key" not in encoded


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "serial",
        "binding",
        "pod",
        "schema",
        "model",
        "firmware",
        "country",
        "missing-country",
        "expired",
        "future",
        "incomplete",
        "missing-radio",
        "extra-radio",
        "duplicate-radio",
        "ruid",
        "evidence",
        "duplicate-class",
        "unknown-class",
        "wrong-channel",
        "no-operable-channel",
        "capacity",
        "observed-channel",
        "missing-channel",
        "band",
        "floating-power",
        "floating-class",
        "floating-capacity",
        "floating-channel",
    ],
)
def test_inconsistent_claims_withdraw_whole_pod_payload(tmp_path, fault):
    binding, raw, p, ref = inputs(tmp_path)
    rs = raw["tables"]["Wifi_Radio_State"]["lab-radio"]
    op = p["radios"][0]["operating_classes"][0]
    if fault == "stale":
        raw["ready"] = False
    elif fault == "serial":
        raw["tables"]["AWLAN_Node"]["node"]["serial_number"] = "other"
    elif fault == "binding":
        p["binding_sha256"] = "b" * 64
    elif fault == "pod":
        p["pod_id"] = "other"
    elif fault == "schema":
        p["schema_fingerprint"] = "b" * 64
    elif fault in {"model", "firmware"}:
        p["device"]["model" if fault == "model" else "firmware_version"] = "other"
    elif fault == "country":
        rs["country"] = "CA"
    elif fault == "missing-country":
        del rs["country"]
    elif fault == "expired":
        p["expires_at"] = NOW.isoformat().replace("+00:00", "Z")
    elif fault == "future":
        p["issued_at"] = (NOW + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    elif fault == "incomplete":
        p["complete_operating_class_inventory"] = False
    elif fault == "missing-radio":
        p["radios"].pop()
    elif fault == "extra-radio":
        extra = copy.deepcopy(p["radios"][0])
        extra["radio_id"] = "extra"
        p["radios"].append(extra)
    elif fault == "duplicate-radio":
        p["radios"].append(copy.deepcopy(p["radios"][0]))
    elif fault == "ruid":
        p["radios"][0]["ruid"] = "02:00:00:00:40:02"
    elif fault == "evidence":
        p["radios"][0]["evidence_id"] = "missing"
    elif fault == "duplicate-class":
        p["radios"][0]["operating_classes"].append(copy.deepcopy(op))
    elif fault == "unknown-class":
        op["operating_class"] = 129
    elif fault == "wrong-channel":
        op["non_operable_channels"] = [1]
    elif fault == "no-operable-channel":
        op["non_operable_channels"] = [36, 40, 44, 48]
    elif fault == "capacity":
        p["radios"][0]["max_bss"] = 1
    elif fault == "observed-channel":
        rs["channel"] = 149
    elif fault == "missing-channel":
        del rs["channel"]
    elif fault == "band":
        rs["freq_band"] = "2.4G"
    elif fault == "floating-power":
        op["max_eirp_dbm"] = 20.0
    elif fault == "floating-class":
        op["operating_class"] = 115.0
    elif fault == "floating-capacity":
        p["radios"][0]["max_bss"] = 4.0
    elif fault == "floating-channel":
        op["non_operable_channels"] = [40.0]
    ref = save(tmp_path, p)
    result = mapped(binding, raw, load_inputs(ref), ref)
    assert not result["ready"] and result["radios"] == [] and result["blockers"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing-profile",
        "profile-hash",
        "evidence-hash",
        "missing-evidence",
        "duplicate-evidence",
        "physical",
        "json",
        "duplicate-json",
        "oversize-profile",
        "oversize-evidence",
        "fifo",
        "symlink",
        "directory",
        "traversal",
        "zero-max",
        "power-range",
        "bool-power",
        "duplicate-channel",
        "unknown-field",
        "naive-time",
        "missing-completeness",
    ],
)
def test_untrusted_files_rejected_without_echoing_contents(tmp_path, fault):
    _, _, p, ref = inputs(tmp_path)
    path = tmp_path / "capabilities.json"
    ev = tmp_path / p["evidence"][0]["file"]
    if fault == "missing-profile":
        path.unlink()
    elif fault == "profile-hash":
        path.write_text("private-content")
    elif fault == "evidence-hash":
        ev.write_text("private-content")
    elif fault == "missing-evidence":
        ev.unlink()
    elif fault == "duplicate-evidence":
        p["evidence"].append(copy.deepcopy(p["evidence"][0]))
    elif fault == "physical":
        p["source_kind"] = "physical_pod_draft"
    elif fault in {"json", "duplicate-json"}:
        path.write_text(
            "private-content" if fault == "json" else '{"private-content":1,"private-content":2}'
        )
        ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif fault == "oversize-profile":
        path.write_bytes(b"x" * 65_537)
    elif fault == "oversize-evidence":
        ev.write_bytes(b"x" * 1_048_577)
    elif fault in {"fifo", "symlink", "directory"}:
        ev.unlink()
        if fault == "fifo":
            os.mkfifo(ev)
        elif fault == "symlink":
            ev.symlink_to(path)
        else:
            ev.mkdir()
    elif fault == "traversal":
        p["evidence"][0]["file"] = "../private-content"
    elif fault == "zero-max":
        p["radios"][0]["max_bss"] = 0
    elif fault == "power-range":
        p["radios"][0]["operating_classes"][0]["max_eirp_dbm"] = 128
    elif fault == "bool-power":
        p["radios"][0]["operating_classes"][0]["max_eirp_dbm"] = True
    elif fault == "duplicate-channel":
        p["radios"][0]["operating_classes"][0]["non_operable_channels"] = [40, 40]
    elif fault == "unknown-field":
        p["private-content"] = "secret"
    elif fault == "naive-time":
        p["expires_at"] = "2026-09-22T12:00:00"
    elif fault == "missing-completeness":
        del p["complete_operating_class_inventory"]
    if fault in {
        "duplicate-evidence",
        "physical",
        "traversal",
        "zero-max",
        "power-range",
        "bool-power",
        "duplicate-channel",
        "unknown-field",
        "naive-time",
        "missing-completeness",
    }:
        ref = save(tmp_path, p)
    with pytest.raises(InputUnavailable) as error:
        load_inputs(ref)
    assert "private-content" not in str(error.value) and str(tmp_path) not in str(error.value)


def test_missing_input_does_not_connect_or_infer_capabilities(tmp_path):
    class NoConnection:
        async def snapshot(self):
            pytest.fail("must not open an unconfigured connection")

    backend = OpenSyncBackend("pod-1", NoConnection(), None)
    result = asyncio.run(backend.radio_capabilities())
    assert result["blockers"] == ["capability_input_not_configured"]
    assert not result["ready"] and result["radios"] == []
    value = config(tmp_path)
    value["pods"][0]["radio_capabilities"] = {"path": "/private/input.json", "sha256": "a" * 64}
    validate("config", value)
    validate_bindings(value)
    del value["pods"][0]["virtual_agent"]["topology"]
    with pytest.raises(EmosaError):
        validate_bindings(value)


def test_signed_power_has_independent_boundary_bytes_and_all_prefixes_rejected():
    ruid = bytes.fromhex("020000014001")
    for power, byte in [(-128, "80"), (-1, "ff"), (0, "00"), (127, "7f")]:
        payload = APRadioBasicCapabilities(ruid, 255, (BasicOperatingClass(81, power, (12, 13)),))
        expected = bytes.fromhex("020000014001ff0151" + byte + "020c0d")
        assert encode_value(payload) == expected
        assert decode_value(0x85, expected) == payload
        for length in range(len(expected)):
            with pytest.raises(EmosaError):
                decode_value(0x85, expected[:length])
    assert encode_value(APRadioBasicCapabilities(ruid, 1, ())) == ruid + b"\x01\x00"
    for value in [ruid + b"\x00\x00", expected + b"\0"]:
        with pytest.raises(EmosaError):
            decode_value(0x85, value)
    # Receive unknown classes diagnostically; never emit an unaudited class.
    decoded = decode_value(0x85, ruid + bytes((1, 1, 255, 0, 0)))
    assert decoded.operating_classes[0].operating_class == 255
    with pytest.raises(EmosaError):
        encode_value(decoded)


@pytest.mark.parametrize(
    "payload",
    [
        APRadioBasicCapabilities(b"short", 1, ()),
        APRadioBasicCapabilities(b"123456", 0, ()),
        APRadioBasicCapabilities(b"123456", True, ()),
        APRadioBasicCapabilities(b"123456", 1, []),
        APRadioBasicCapabilities(b"123456", 1, (None,)),
        *[
            APRadioBasicCapabilities(b"123456", 1, (BasicOperatingClass(81, n, ()),))
            for n in (-129, 128, True, 1.0)
        ],
        APRadioBasicCapabilities(b"123456", 1, (BasicOperatingClass(True, 0, ()),)),
        APRadioBasicCapabilities(b"123456", 1, (BasicOperatingClass(81, 0, (True,)),)),
        APRadioBasicCapabilities(b"123456", 1, (BasicOperatingClass(81, 0, (0,)),)),
        APRadioBasicCapabilities(b"123456", 1, (BasicOperatingClass(81, 0, (14,)),)),
        APRadioBasicCapabilities(b"123456", 1, (BasicOperatingClass(81, 0, [12]),)),
    ],
)
def test_invalid_capability_encoder_objects_fail_closed(payload):
    with pytest.raises(EmosaError):
        encode_value(payload)
