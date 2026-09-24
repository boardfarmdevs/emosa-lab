"""opensync-lab-hwsim-6.6.1-v1: the real OpenSync 6.6 VIF encoding, offline."""

import asyncio
import copy
import json

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
