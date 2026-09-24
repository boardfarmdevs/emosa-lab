"""opensync-lab-hwsim-6.6.1-v1: the real OpenSync 6.6 VIF encoding, offline."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from emosa.errors import EmosaError, Reason
from emosa.model import Intent
from emosa.opensync.pod_profile import PodBackend, wpa2_psk
from emosa.opensync.schema import Schema, reference_path
from emosa.secrets import SecretStore

pytestmark = pytest.mark.unit

NODE, RADIO, RADIO_STATE = (f"00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))
VIF, VIF_STATE = "00000000-0000-4000-8000-000000000004", "00000000-0000-4000-8000-000000000005"
SERIAL = "MVXPOD0000000001"
SECURITY = {
    "wpa": True,
    "wpa_key_mgmt": "wpa-psk",  # ow_ovsdb.c: OSW_AKM_RSN_PSK -> "wpa-psk"
    "wpa_psks": ["map", [["key--1", "opensync-lab-home-psk"]]],
    "security": ["map", []],
    "rsn_pairwise_ccmp": True,
    "wpa_pairwise_tkip": False,
    "wpa_pairwise_ccmp": False,
}


def tables(**vif):
    common = {"if_name": "home-ap-24", "mode": "ap", "ssid": "opensync-lab-home", "enabled": True}
    return {
        "AWLAN_Node": {
            NODE: {"serial_number": SERIAL, "model": "HWSIM_POD", "firmware_version": "6.6.1.0"}
        },
        "Wifi_Radio_Config": {
            RADIO: {
                "if_name": "wlan0",
                "freq_band": "2.4G",
                "enabled": True,
                "vif_configs": ["set", [["uuid", VIF]]],
            }
        },
        "Wifi_Radio_State": {
            RADIO_STATE: {
                "if_name": "wlan0",
                "radio_config": ["uuid", RADIO],
                "vif_states": ["set", [["uuid", VIF_STATE]]],
                "freq_band": "2.4G",
                "channel": 6,
                "mac": "02:00:00:00:1a:00",
                "enabled": True,
                "country": "US",
            }
        },
        "Wifi_VIF_Config": {VIF: {**common, **SECURITY, **vif}},
        "Wifi_VIF_State": {
            VIF_STATE: {
                **common,
                **SECURITY,
                "vif_config": ["uuid", VIF],
                "mac": "02:00:00:00:1a:01",
                "associated_clients": ["set", []],
            }
        },
        "Wifi_Associated_Clients": {},
    }


class Session:
    def __init__(self, raw_tables):
        self.schema = Schema(json.loads(reference_path().read_text()))
        self.tables, self.sent = raw_tables, []

    async def snapshot(self):
        return {
            "tables": copy.deepcopy(self.tables),
            "generation": 1,
            "revision": 7,
            "ready": True,
            "schema": self.schema,
        }

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(operations)
        replies = {"wait": {}, "select": {"rows": []}, "insert": {"uuid": ["uuid", VIF]}}
        return [replies.get(op["op"], {"count": 1}) for op in operations]


def cold():
    """A restarted pod: bootstrap radios, no fronthaul VIF (the cloud creates it)."""
    raw = tables()
    del raw["Wifi_VIF_Config"][VIF], raw["Wifi_VIF_State"][VIF_STATE]
    raw["Wifi_Radio_Config"][RADIO]["vif_configs"] = ["set", []]
    raw["Wifi_Radio_State"][RADIO_STATE]["vif_states"] = ["set", []]
    return raw


def backend(tmp_path, **vif):
    vault = SecretStore(tmp_path / "secrets")
    vault.write_simulated("new", "EmosaMesh2026!")
    return PodBackend("pod-1", Session(tables(**vif)), vault, serial=SERIAL), vault


def intent():
    return Intent("pod-1", "radio-1", "bss-1", "emosa-mesh", "new")


def test_osw_encoding_is_wpa2_psk_and_simulator_encoding_is_not():
    decoded = {
        "wpa": True,
        "wpa_key_mgmt": ["wpa-psk"],
        "rsn_pairwise_ccmp": True,
        "wpa_psks": {"key": "k"},
    }
    assert wpa2_psk(decoded)
    assert not wpa2_psk({**decoded, "wpa_key_mgmt": ["wpa2-psk"]})  # synthetic simulator form
    assert not wpa2_psk({**decoded, "rsn_pairwise_ccmp": False})  # WPA1
    assert not wpa2_psk({**decoded, "wpa_psks": {"key": "a", "key-1": "b"}})  # multi-PSK
    assert not wpa2_psk({**decoded, "wpa_psks": {}})


def test_snapshot_reads_the_pod_state_and_binds_its_radio(tmp_path):
    pod, vault = backend(tmp_path)
    snap = asyncio.run(pod.snapshot())
    assert snap.ready and snap.observed.fresh
    assert snap.observed.values["security_mode"] == "wpa2-psk"
    assert snap.observed.values["credential_fingerprint"] == vault.fingerprint(
        "opensync-lab-home-psk"
    )
    assert snap.observed.provenance == "opensync-owm:Wifi_VIF_State"
    assert pod.identity == {
        "radio_mac": "02:00:00:00:1a:00",
        "bssid": "02:00:00:00:1a:01",
        "channel": 6,
        "radio_if_name": "wlan0",
    }


def test_submit_replaces_the_psk_slot_under_serial_and_graph_guards(tmp_path):
    pod, _ = backend(tmp_path)
    asyncio.run(pod.context())
    result = asyncio.run(pod.submit(intent(), {"transaction_id": "t1", "session_generation": 1}))
    assert (
        result.status == "committed" and result.evidence["profile"] == "opensync-lab-hwsim-6.6.1-v1"
    )
    (ops,) = pod.session.sent
    assert [op["op"] for op in ops] == ["wait", "wait", "wait", "update", "mutate"]
    assert ops[0]["rows"][0]["serial_number"] == SERIAL
    assert ops[3]["row"] == {"ssid": "emosa-mesh"}
    assert ops[4]["mutations"] == [
        ["wpa_psks", "delete", ["set", ["key--1"]]],
        ["wpa_psks", "insert", ["map", [["key", "EmosaMesh2026!"]]]],
    ]
    assert "tx_chainmask" not in json.dumps(ops)
    assert all(op["table"] != "Wifi_Radio_Config" for op in ops if op["op"] != "wait")


def test_other_serial_or_unqualified_security_is_refused(tmp_path):
    pod, _ = backend(tmp_path)
    pod.expected_serial = "MVXPOD-OTHER"
    with pytest.raises(EmosaError) as error:
        asyncio.run(pod.context())
    assert error.value.code == Reason.NOT_READY
    pod, _ = backend(tmp_path / "sim", wpa_key_mgmt="wpa2-psk")
    with pytest.raises(EmosaError) as error:
        asyncio.run(pod.plan(intent()))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


def test_request_outside_the_bound_vif_is_refused(tmp_path):
    pod, _ = backend(tmp_path)
    with pytest.raises(EmosaError) as error:
        asyncio.run(pod.plan(Intent("pod-1", "radio-2", "bss-1", "x", "new")))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


def test_cold_pod_fronthaul_is_created_only_from_the_intent(tmp_path):
    vault = SecretStore(tmp_path / "secrets")
    vault.write_simulated("new", "EmosaMesh2026!")
    pod = PodBackend("pod-1", Session(cold()), vault, serial=SERIAL)
    snap = asyncio.run(pod.snapshot())
    # ready to plan (pod and radio bound), but nothing observed as applied
    assert snap.ready and not snap.observed.fresh and pod.identity["bssid"] is None
    assert pod.identity["radio_mac"] == "02:00:00:00:1a:00"
    asyncio.run(pod.context())  # the radio alone anchors a cold pod
    assert asyncio.run(pod.plan(intent()))["action"] == "create"
    result = asyncio.run(pod.submit(intent(), {"transaction_id": "t1", "session_generation": 1}))
    assert result.status == "committed" and result.evidence["action"] == "create"
    select, ops = pod.session.sent
    assert select[0]["op"] == "select" and select[0]["table"] == "Wifi_Inet_Config"
    assert [(op["op"], op["table"]) for op in ops] == [
        ("wait", "AWLAN_Node"),
        ("wait", "Wifi_Radio_Config"),
        ("wait", "Wifi_VIF_Config"),
        ("insert", "Wifi_VIF_Config"),
        ("mutate", "Wifi_Radio_Config"),
        ("update", "Wifi_Radio_Config"),
        ("wait", "Wifi_Inet_Config"),
        ("insert", "Wifi_Inet_Config"),
    ]
    assert ops[2]["rows"] == []  # no VIF of that name may appear concurrently
    vif = ops[3]["row"]
    assert (vif["if_name"], vif["ssid"], vif["wpa_key_mgmt"]) == (
        "home-ap-24",
        "emosa-mesh",
        "wpa-psk",
    )
    assert vif["wpa_psks"] == ["map", [["key", "EmosaMesh2026!"]]]
    assert ops[4]["mutations"] == [["vif_configs", "insert", ["set", [["named-uuid", "fh"]]]]]
    assert ops[5]["row"] == {"channel": 6, "ht_mode": "HT20", "enabled": True}
    assert "tx_chainmask" not in json.dumps(ops)


EXTRA = "00000000-0000-4000-8000-000000000006"
RDK_SET = (  # RDK unified-wifi-mesh's M2 set, besides its private fronthaul
    ("fronthaul", "iot_ssid"),
    ("fronthaul", "lnf_radius"),
    ("fronthaul", "hotspot"),
    ("backhaul", "mesh_backhaul"),
)


def multi(tmp_path, raw=None):
    vault = SecretStore(tmp_path / "secrets")
    vault.write_simulated("new", "EmosaMesh2026!")
    for n, _ in enumerate(RDK_SET):
        vault.write_simulated(f"x{n}", f"ExtraKey{n}-2026")
    session = Session(raw or tables())
    return PodBackend("pod-1", session, vault, serial=SERIAL, multi_bss=True), vault


def rdk_intent(count=4):
    extra = tuple(
        {"role": role, "ssid": ssid, "secret_ref": f"x{n}"}
        for n, (role, ssid) in enumerate(RDK_SET[:count])
    )
    return Intent("pod-1", "radio-1", "bss-1", "private_ssid", "new", additional=extra)


def with_slot_vif(name="svc-d-ap-24"):
    raw = tables()
    raw["Wifi_VIF_Config"][EXTRA] = {
        "if_name": name,
        "mode": "ap",
        "ssid": "old",
        "enabled": True,
        **SECURITY,
    }
    raw["Wifi_Radio_Config"][RADIO]["vif_configs"] = ["set", [["uuid", VIF], ["uuid", EXTRA]]]
    return raw


def test_multi_bss_maps_the_rdk_set_onto_the_platform_vifs(tmp_path):
    pod, _ = multi(tmp_path)
    assert pod.max_bss == 5
    plan = asyncio.run(pod.plan(rdk_intent()))
    assert plan["additional_slots"] == {
        "svc-d-ap-24": "fronthaul",
        "svc-e-ap-24": "fronthaul",
        "fh-24": "fronthaul",
        "b-ap-24": "backhaul",
    }
    asyncio.run(pod.context())
    result = asyncio.run(pod.submit(rdk_intent(), {"transaction_id": "t", "session_generation": 1}))
    assert result.status == "committed" and result.evidence["additional_bss_count"] == 4
    _, ops = pod.session.sent  # Inet select, then one transaction
    inserts = {
        op["row"]["if_name"]: op["row"]
        for op in ops
        if op["op"] == "insert" and op["table"] == "Wifi_VIF_Config"
    }
    assert set(inserts) >= {"svc-d-ap-24", "svc-e-ap-24", "fh-24", "b-ap-24"}
    assert (
        inserts["b-ap-24"]["multi_ap"] == "backhaul_bss"
        and inserts["b-ap-24"]["vif_radio_idx"] == 1
    )
    assert inserts["fh-24"]["ssid"] == "hotspot" and inserts["fh-24"]["vif_radio_idx"] == 6
    assert inserts["svc-d-ap-24"]["wpa_psks"] == ["map", [["key", "ExtraKey0-2026"]]]
    assert "tx_chainmask" not in json.dumps(ops)


def test_slot_vifs_outside_the_new_set_are_removed(tmp_path):
    pod, _ = multi(tmp_path, with_slot_vif())
    single = Intent("pod-1", "radio-1", "bss-1", "emosa-mesh", "new", additional=())
    asyncio.run(pod.context())
    asyncio.run(pod.submit(single, {"transaction_id": "t", "session_generation": 1}))
    ops = pod.session.sent[-1]
    assert {
        "op": "delete",
        "table": "Wifi_VIF_Config",
        "where": [["_uuid", "==", ["uuid", EXTRA]]],
    } in ops
    removal = [op for op in ops if op["op"] == "mutate" and op["table"] == "Wifi_Radio_Config"]
    assert removal[0]["mutations"] == [["vif_configs", "delete", ["set", [["uuid", EXTRA]]]]]


def test_more_bsses_of_a_role_than_slots_is_unsupported(tmp_path):
    pod, _ = multi(tmp_path)
    too_many = Intent(
        "pod-1",
        "radio-1",
        "bss-1",
        "a",
        "new",
        additional=tuple({"role": "backhaul", "ssid": f"b{n}", "secret_ref": "x0"} for n in (1, 2)),
    )
    with pytest.raises(EmosaError) as error:
        asyncio.run(pod.plan(too_many))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


def test_extra_bsses_are_observed_from_their_own_state(tmp_path):
    pod, vault = multi(tmp_path, with_slot_vif())
    snap = asyncio.run(pod.snapshot())
    assert snap.config["additional"] == [
        ["fronthaul", "old", vault.fingerprint("opensync-lab-home-psk")]
    ]
    assert snap.observed.values["additional"] == []  # configured, but no State yet
    single, _ = backend(tmp_path / "single")
    assert "additional" not in asyncio.run(single.snapshot()).config


def test_single_bss_radio_refuses_additional_bsses(tmp_path):
    pod, _ = backend(tmp_path)
    with pytest.raises(EmosaError) as error:
        asyncio.run(pod.plan(rdk_intent(1)))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION


def test_the_pod_layout_is_profile_data(tmp_path):
    from emosa.opensync import profiles

    bundled = profiles.load()
    assert bundled.id == profiles.DEFAULT and bundled.fronthaul_if == "home-ap-24"
    data = json.loads(
        (Path(profiles.__file__).parents[1] / "profiles" / f"{profiles.DEFAULT}.json").read_text()
    )
    data.update(id="other-pod-v1")
    data["fronthaul"]["if_name"] = "wl0.2"
    data["radio"]["channel"] = 11
    path = tmp_path / "other.json"
    path.write_text(json.dumps(data))
    vault = SecretStore(tmp_path / "secrets")
    pod = PodBackend(
        "pod-1", Session(tables()), vault, serial=SERIAL, profile=profiles.load(str(path))
    )
    assert (pod.if_name, pod.channel, pod.profile.id) == ("wl0.2", 11, "other-pod-v1")
    data["radio"]["band"] = "5G"  # not a band the EasyMesh mapping covers yet
    path.write_text(json.dumps(data))
    with pytest.raises(EmosaError):
        profiles.load(str(path))
