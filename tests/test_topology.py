import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from emosa.agents import validate_bindings
from emosa.config import validate
from emosa.easymesh_payloads import decode_value
from emosa.errors import EmosaError, Reason
from emosa.opensync.topology import project
from emosa.store import Store
from emosa.topology_bindings import REGISTRY_KEY, binding_digest, canonical_binding
from emosa_lab.app import Application
from emosa_lab.simulation.connecting_pod import configuration
from emosa_lab.simulation.topology import agent_binding

pytestmark = pytest.mark.unit


def config(tmp_path):
    result = configuration(tmp_path, "unix:/private/simulation.sock")
    result["pods"][0]["virtual_agent"] = agent_binding(1)
    return result


def observation(tmp_path):
    binding = canonical_binding(config(tmp_path)["pods"][0])
    tables = {
        "AWLAN_Node": {"node": {"serial_number": binding["expected_serial"]}},
        "Wifi_Radio_Config": {},
        "Wifi_Radio_State": {},
        "Wifi_VIF_Config": {},
        "Wifi_VIF_State": {},
    }
    # Predecoded graph unit inputs. Real OVSDB/schema normalization is exercised
    # separately by test_topology_service and the executable two-pod demo.
    for radio in binding["topology"]["radios"]:
        name = radio["if_name"]
        interfaces = radio["interfaces"]
        tables["Wifi_Radio_Config"][name] = {
            "if_name": name,
            "vif_configs": [v["if_name"] for v in interfaces],
        }
        tables["Wifi_Radio_State"][name] = {
            "if_name": name,
            "radio_config": name,
            "vif_states": [v["if_name"] for v in interfaces],
            "mac": radio["expected_mac"],
            "enabled": True,
        }
        for vif in interfaces:
            v = vif["if_name"]
            tables["Wifi_VIF_Config"][v] = {
                "if_name": v,
                "ssid": "desired-not-observed",
                "wpa_psks": {"key": "never-publish-private-key"},
            }
            tables["Wifi_VIF_State"][v] = {
                "if_name": v,
                "vif_config": v,
                "enabled": True,
                "mode": vif["mode"],
                "mac": vif["expected_mac"],
                "ssid": "observed-" + v,
            }
    raw = {
        "tables": tables,
        "ready": True,
        "generation": 2,
        "revision": 9,
        "schema": SimpleNamespace(row=lambda _t, r: dict(r), fingerprint="unit-predecoded"),
    }
    return binding, raw


def report(binding, raw):
    return project(
        "pod-1", raw, binding, state_provenance="independent-simulated-manager:Wifi_VIF_State"
    )


def test_complete_observed_graph_maps_ids_and_excludes_station_from_ap_value(tmp_path):
    binding, raw = observation(tmp_path)
    result = report(binding, raw)
    assert result["ready"] and result["complete"] and result["snapshot_fresh"]
    assert result["generation"] == 2 and result["revision"] == 9
    assert [r["radio_id"] for r in result["radios"]] == ["radio-1", "radio-2"]
    assert [v["interface_id"] for r in result["radios"] for v in r["interfaces"]] == [
        "bss-1",
        "bss-guest",
        "backhaul-station",
        "bss-iot",
    ]
    value = decode_value(0x83, bytes.fromhex(result["operational_bss_value"]["value_hex"]))
    assert [[b.ssid for b in r.bsses] for r in value.radios] == [
        [b"observed-lab-ap", b"observed-guest-ap"],
        [b"observed-iot-ap"],
    ]
    assert [r.ruid.hex(":") for r in value.radios] == ["02:00:00:01:40:01", "02:00:00:01:40:02"]
    assert [r["channel"] for r in result["radios"]] == [None, None]
    assert not result["controller_onboarding_proven"] and not result["physical_pod_proven"]
    assert result["physical_links"] == "unknown" and result["capabilities"] == "not_inferred"
    serialized = json.dumps(result)
    assert (
        "desired-not-observed" not in serialized and "never-publish-private-key" not in serialized
    )
    assert "vif_config" not in serialized and "radio_config" not in serialized


def test_order_changes_and_uuid_recreation_preserve_logical_report_and_payload(tmp_path):
    binding, raw = observation(tmp_path)
    before = report(binding, raw)
    tables = raw["tables"]
    for table in ("Wifi_Radio_Config", "Wifi_Radio_State", "Wifi_VIF_Config", "Wifi_VIF_State"):
        tables[table] = {"new-" + uid: row for uid, row in reversed(list(tables[table].items()))}
        for row in tables[table].values():
            for field in ("radio_config", "vif_config"):
                if field in row:
                    row[field] = "new-" + row[field]
            for field in ("vif_configs", "vif_states"):
                if field in row:
                    row[field] = ["new-" + ref for ref in reversed(row[field])]
    raw["generation"] += 1
    after = report(binding, raw)
    assert after["ready"]
    assert after["radios"] == before["radios"]
    assert after["operational_bss_value"] == before["operational_bss_value"]
    assert after["binding_sha256"] == before["binding_sha256"]


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "pod",
        "missing-table",
        "serial",
        "two-nodes",
        "extra-radio",
        "missing-radio",
        "extra-vif",
        "missing-vif",
        "duplicate-name",
        "duplicate-radio-state",
        "duplicate-vif-state",
        "orphan-state",
        "missing-state",
        "config-membership",
        "state-membership",
        "radio-state-name",
        "vif-state-name",
        "radio-mac",
        "vif-mac",
        "zero-mac",
        "multicast-mac",
        "unknown-radio-enabled",
        "unknown-vif-enabled",
        "mode",
        "disabled-radio",
        "missing-ssid",
        "long-ssid",
        "empty-ssid",
        "surrogate-ssid",
    ],
)
def test_incomplete_or_changed_graph_never_returns_partial_payload(tmp_path, fault):
    binding, raw = observation(tmp_path)
    t = raw["tables"]
    rc, rs, vc, vs = (
        t[name]
        for name in ("Wifi_Radio_Config", "Wifi_Radio_State", "Wifi_VIF_Config", "Wifi_VIF_State")
    )
    if fault == "stale":
        raw["ready"] = False
    elif fault == "pod":
        binding["pod_id"] = "wrong-pod"
    elif fault == "missing-table":
        del t["Wifi_Radio_State"]
    elif fault == "serial":
        t["AWLAN_Node"]["node"]["serial_number"] = "wrong"
    elif fault == "two-nodes":
        t["AWLAN_Node"]["other"] = t["AWLAN_Node"]["node"]
    elif fault == "extra-radio":
        rc["extra"] = {"if_name": "unexpected"}
    elif fault == "missing-radio":
        del rc["lab-radio-2"]
    elif fault == "extra-vif":
        vc["extra"] = {"if_name": "unexpected"}
    elif fault == "missing-vif":
        del vc["guest-ap"]
    elif fault == "duplicate-name":
        vc["guest-ap"]["if_name"] = "lab-ap"
    elif fault == "duplicate-radio-state":
        rs["extra"] = rs["lab-radio"]
    elif fault == "duplicate-vif-state":
        vs["extra"] = vs["lab-ap"]
    elif fault == "orphan-state":
        vs["lab-ap"]["vif_config"] = "missing"
    elif fault == "missing-state":
        del vs["guest-ap"]
    elif fault == "config-membership":
        rc["lab-radio"]["vif_configs"].append("iot-ap")
    elif fault == "state-membership":
        rs["lab-radio"]["vif_states"].append("iot-ap")
    elif fault == "radio-state-name":
        rs["lab-radio"]["if_name"] = "wrong"
    elif fault == "vif-state-name":
        vs["lab-ap"]["if_name"] = "wrong"
    elif fault == "radio-mac":
        rs["lab-radio"]["mac"] = "02:00:00:00:99:01"
    elif fault == "vif-mac":
        vs["lab-ap"]["mac"] = "02:00:00:00:99:02"
    elif fault == "zero-mac":
        vs["lab-ap"]["mac"] = "00:00:00:00:00:00"
    elif fault == "multicast-mac":
        vs["lab-ap"]["mac"] = "03:00:00:00:99:02"
    elif fault == "unknown-radio-enabled":
        rs["lab-radio"]["enabled"] = None
    elif fault == "unknown-vif-enabled":
        vs["lab-ap"]["enabled"] = None
    elif fault == "mode":
        vs["lab-ap"]["mode"] = "sta"
    elif fault == "disabled-radio":
        rs["lab-radio"]["enabled"] = False
    elif fault == "missing-ssid":
        vs["lab-ap"].pop("ssid")
    elif fault == "long-ssid":
        vs["lab-ap"]["ssid"] = "é" * 17
    elif fault == "empty-ssid":
        vs["lab-ap"]["ssid"] = ""
    elif fault == "surrogate-ssid":
        vs["lab-ap"]["ssid"] = "\ud800"
    result = report(binding, raw)
    assert not result["ready"] and not result["complete"] and result["blockers"]
    assert result["radios"] == [] and result["operational_bss_value"] is None


def test_disabled_bsses_are_not_operational_and_zero_bss_radio_is_explicit(tmp_path):
    binding, raw = observation(tmp_path)
    for name in ("lab-ap", "guest-ap"):
        row = raw["tables"]["Wifi_VIF_State"][name]
        row["enabled"] = False
        row.pop("ssid")
    raw["tables"]["Wifi_Radio_State"]["lab-radio"]["enabled"] = False
    result = report(binding, raw)
    assert result["ready"]
    value = decode_value(0x83, bytes.fromhex(result["operational_bss_value"]["value_hex"]))
    assert [len(r.bsses) for r in value.radios] == [0, 1]


def test_explicit_topology_binding_required(tmp_path):
    _, raw = observation(tmp_path)
    assert report(None, raw)["blockers"] == ["topology_binding_not_configured"]


@pytest.mark.parametrize(
    "fault",
    [
        "radio-id",
        "vif-id",
        "vif-name",
        "ruid",
        "al-collision",
        "radio-mac",
        "vif-mac",
        "zero-mac",
        "designated-radio",
        "designated-bss",
        "interface-budget",
    ],
)
def test_config_rejects_ambiguous_or_excessive_identity_bindings(tmp_path, fault):
    c = config(tmp_path)
    first, second = c["pods"][0]["virtual_agent"]["topology"]["radios"]
    if fault == "radio-id":
        second["radio_id"] = first["radio_id"]
    elif fault == "vif-id":
        second["interfaces"][0]["interface_id"] = first["interfaces"][0]["interface_id"]
    elif fault == "vif-name":
        second["interfaces"][0]["if_name"] = first["interfaces"][0]["if_name"]
    elif fault == "ruid":
        second["ruid"] = first["ruid"]
    elif fault == "al-collision":
        first["ruid"] = c["pods"][0]["virtual_agent"]["al_mac"]
    elif fault == "radio-mac":
        second["expected_mac"] = first["expected_mac"]
    elif fault == "vif-mac":
        second["interfaces"][0]["expected_mac"] = first["interfaces"][0]["expected_mac"]
    elif fault == "zero-mac":
        first["expected_mac"] = "00:00:00:00:00:00"
    elif fault == "designated-radio":
        c["pods"][0]["radio_name"] = "wrong"
    elif fault == "designated-bss":
        c["pods"][0]["bss_id"] = "wrong"
    elif fault == "interface-budget":
        first["interfaces"] *= 33
    with pytest.raises(EmosaError):
        validate("config", c)
        validate_bindings(c)


def test_binding_persistence_is_atomic_order_independent_and_survives_endpoint_change(tmp_path):
    c = config(tmp_path)
    validate("config", c)
    validate_bindings(c)
    store = Store(tmp_path / "state")
    try:
        first = store.pin_topologies(c["pods"])
    finally:
        store.close()
    c["pods"][0]["endpoint"] = "unix:/private/changed-address.sock"
    radios = c["pods"][0]["virtual_agent"]["topology"]["radios"]
    radios.reverse()
    for radio in radios:
        radio["interfaces"].reverse()
    store = Store(tmp_path / "state")
    try:
        assert store.pin_topologies(c["pods"]) == first
        assert binding_digest(store.pin_topologies(c["pods"])["pod-1"]) == binding_digest(
            first["pod-1"]
        )
        before = store.db.execute(
            "SELECT value FROM metadata WHERE key=?", (REGISTRY_KEY,)
        ).fetchone()[0]
        other = copy.deepcopy(c["pods"][0])
        other.update(pod_id="pod-2", virtual_agent=agent_binding(2))
        changed = copy.deepcopy(c["pods"][0])
        changed["virtual_agent"]["al_mac"] = "02:00:00:99:30:01"
        with pytest.raises(EmosaError) as error:
            store.pin_topologies([other, changed])
        assert error.value.code == Reason.PRECONDITION_FAILED
        assert (
            store.db.execute("SELECT value FROM metadata WHERE key=?", (REGISTRY_KEY,)).fetchone()[
                0
            ]
            == before
        )
        # Removing a pod keeps its allocations reserved against reuse.
        store.pin_topologies([])
        renamed = copy.deepcopy(c["pods"][0])
        renamed["pod_id"] = "new-id"
        with pytest.raises(EmosaError):
            store.pin_topologies([renamed])
    finally:
        store.close()


def test_service_binding_rejection_releases_writer_lock_before_opening_sessions(tmp_path):
    c = config(tmp_path)
    store = Store(tmp_path / "state")
    store.pin_topologies(c["pods"])
    store.close()
    c["pods"][0]["virtual_agent"].pop("topology")
    with pytest.raises(EmosaError, match="persisted topology binding differs"):
        Application(c)
    Store(tmp_path / "state").close()


def test_topology_request_contract():
    validate(
        "local-api",
        {
            "schema_version": 1,
            "request_id": "one",
            "method": "topology",
            "params": {"pod_id": "pod-1"},
        },
    )


def test_manual_example_matches_existing_fixture_configuration(tmp_path):
    c = configuration(tmp_path, "unix:/private/simulation.sock")
    c["pods"][0]["virtual_agent"]["topology"] = json.loads(
        Path("examples/topology/single-radio.json").read_text()
    )
    validate("config", c)
    validate_bindings(c)
