# SPDX-License-Identifier: Apache-2.0
"""spec 3.10: legacy pods, the OpenSync 2.0 schema (no wpa_* columns or multi_ap), offline."""

import asyncio
import copy
import hashlib
import json

import pytest

from emosa.errors import EmosaError, Reason
from emosa.model import Intent
from emosa.opensync.pod_profile import LEGACY_VIF_GUARDS, PodBackend
from emosa.opensync.profiles import load as load_profile
from emosa.opensync.radio_scope import psk, wpa2_psk
from emosa.opensync.schema import (
    LEGACY,
    LEGACY_SCHEMA_SHA256,
    OPENSYNC_66,
    Schema,
    reference_path,
    reference_schema,
)
from emosa.secrets import SecretStore

pytestmark = pytest.mark.unit

PROFILE = "bcm-pod-2.4.3-gre-v1"
NODE, RADIO, RADIO_STATE = (f"00000000-0000-4000-8000-00000000010{n}" for n in (1, 2, 3))
VIF, VIF_STATE = "00000000-0000-4000-8000-000000000104", "00000000-0000-4000-8000-000000000105"
SERIAL = "BCMPOD0000000001"
SECURITY = ["map", [["encryption", "WPA-PSK"], ["key", "operator-psk"], ["oftag", "home--1"]]]


def test_the_legacy_reference_is_osync_2_0_0_pinned_by_hash():
    assert hashlib.sha256(reference_path(LEGACY).read_bytes()).hexdigest() == LEGACY_SCHEMA_SHA256
    raw = reference_schema(LEGACY)
    vif = raw["tables"]["Wifi_VIF_Config"]["columns"]
    assert "multi_ap" not in vif and "wpa_psks" not in vif and "security" in vif
    assert "multi_ap" not in raw["tables"]["Wifi_VIF_State"]["columns"]


def test_a_legacy_pod_qualifies_as_legacy_and_not_as_6_6():
    schema = Schema(reference_schema(LEGACY))
    schema.qualify_synthetic(generation=LEGACY)
    with pytest.raises(EmosaError) as refused:
        schema.qualify_synthetic(generation=OPENSYNC_66)
    assert refused.value.code == Reason.SCHEMA_MISMATCH


def test_a_6_6_pod_does_not_qualify_as_legacy():
    schema = Schema(json.loads(reference_path().read_text()))
    schema.qualify_synthetic()
    with pytest.raises(EmosaError) as refused:
        schema.qualify_synthetic(generation=LEGACY)
    assert refused.value.code == Reason.SCHEMA_MISMATCH


def with_mode_enum(values):
    raw = reference_schema(LEGACY)
    for table in ("Wifi_VIF_Config", "Wifi_VIF_State"):
        raw["tables"][table]["columns"]["mode"]["type"]["key"]["enum"] = ["set", values]
    return Schema(raw)


def test_a_narrower_enum_qualifies_while_it_keeps_what_emosa_writes():
    # the pods' own schema has no ap_vlan (13 October)
    with_mode_enum(["ap", "monitor", "sta"]).qualify_synthetic(generation=LEGACY)
    with pytest.raises(EmosaError, match="type changed"):
        with_mode_enum(["ap", "monitor"]).qualify_synthetic(generation=LEGACY)  # no "sta"
    with pytest.raises(EmosaError, match="type changed"):
        with_mode_enum(["ap", "monitor", "sta", "mesh"]).qualify_synthetic(generation=LEGACY)


def test_a_changed_column_type_does_not_qualify():
    raw = reference_schema(LEGACY)
    raw["tables"]["Wifi_VIF_Config"]["columns"]["ssid"]["type"] = "integer"
    with pytest.raises(EmosaError, match="type changed"):
        Schema(raw).qualify_synthetic(generation=LEGACY)


def test_the_legacy_encoding_is_the_security_map():
    row = {
        "if_name": "wl1.2",
        "security": {"encryption": "WPA-PSK", "key": "k", "oftag": "home--1"},
    }
    assert wpa2_psk(row) and psk(row) == "k"
    assert not wpa2_psk({**row, "security": {"encryption": "OPEN"}})
    assert not wpa2_psk({**row, "security": {"encryption": "WPA-PSK"}})  # no key
    # a 6.6 row with its security map empty is judged by its wpa_* columns, as before
    assert not wpa2_psk({"wpa": True, "security": {"encryption": "WPA-PSK", "key": "k"}})


def test_the_legacy_profile_is_a_gre_parent_with_its_schema():
    profile = load_profile(PROFILE)
    assert profile.legacy and profile.schema == LEGACY
    assert profile.backhaul_mode == "gre-parent"
    assert not load_profile("opensync-lab-hwsim-6.6.1-v1").legacy


def test_a_legacy_profile_with_a_multi_ap_backhaul_is_refused(tmp_path):
    from emosa.opensync.profiles import files

    data = json.loads(files("emosa").joinpath(f"profiles/{PROFILE}.json").read_text())
    data["backhaul_mode"] = "multi-ap"
    path = tmp_path / "legacy-multi-ap.json"
    path.write_text(json.dumps(data))
    with pytest.raises(EmosaError) as refused:
        load_profile(str(path))
    assert refused.value.code == Reason.INVALID_INPUT


def tables(vif=True):
    """A legacy pod: its 2.4 GHz radio wl1, and (vif) its fronthaul wl1.2 as the cloud made it."""
    common = {"if_name": "wl1.2", "mode": "ap", "ssid": "operator-home", "enabled": True}
    raw = {
        "AWLAN_Node": {
            NODE: {"serial_number": SERIAL, "model": "BCM", "firmware_version": "2.4.3"}
        },
        "Wifi_Radio_Config": {
            RADIO: {
                "if_name": "wl1",
                "freq_band": "2.4G",
                "enabled": True,
                "vif_configs": ["set", [["uuid", VIF]] if vif else []],
            }
        },
        "Wifi_Radio_State": {
            RADIO_STATE: {
                "if_name": "wl1",
                "radio_config": ["uuid", RADIO],
                "vif_states": ["set", [["uuid", VIF_STATE]] if vif else []],
                "freq_band": "2.4G",
                "channel": 6,
                "mac": "02:00:00:00:41:00",
                "enabled": True,
                "country": "EU",
            }
        },
        "Wifi_VIF_Config": {VIF: {**common, "security": SECURITY}} if vif else {},
        "Wifi_VIF_State": {
            VIF_STATE: {
                **common,
                "security": SECURITY,
                "vif_config": ["uuid", VIF],
                "mac": "02:00:00:00:41:02",
                "associated_clients": ["set", []],
            }
        }
        if vif
        else {},
        "Wifi_Associated_Clients": {},
    }
    return raw


class Session:
    def __init__(self, raw_tables):
        self.schema = Schema(reference_schema(LEGACY))
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


def backend(tmp_path, vif=True):
    vault = SecretStore(tmp_path / "secrets")
    vault.write_simulated("new", "EmosaMesh2026!")
    profile = load_profile(PROFILE)
    return PodBackend("pod-1", Session(tables(vif)), vault, serial=SERIAL, profile=profile), vault


WANTED = ["map", [["encryption", "WPA-PSK"], ["key", "EmosaMesh2026!"], ["oftag", "home--1"]]]


def test_a_legacy_pods_fronthaul_is_read_from_its_security_map(tmp_path):
    pod, vault = backend(tmp_path)
    snap = asyncio.run(pod.snapshot())
    assert snap.ready and snap.observed.fresh
    assert snap.observed.values["security_mode"] == "wpa2-psk"
    assert snap.observed.values["credential_fingerprint"] == vault.fingerprint("operator-psk")


def test_a_legacy_update_writes_the_whole_security_map_under_its_guards(tmp_path):
    pod, _ = backend(tmp_path)
    asyncio.run(pod.context())
    intent = Intent("pod-1", "radio-1", "bss-1", "emosa-mesh", "new")
    plan = asyncio.run(pod.plan(intent))
    assert plan["fields"] == ["ssid", "security"]
    result = asyncio.run(pod.submit(intent, {"transaction_id": "t1", "session_generation": 1}))
    assert result.status == "committed"
    (ops,) = pod.session.sent
    assert [op["op"] for op in ops] == ["wait", "wait", "wait", "update"]
    assert ops[2]["columns"] == list(LEGACY_VIF_GUARDS)
    assert ops[3]["row"] == {"ssid": "emosa-mesh", "security": WANTED}  # no multi_ap, no wpa_psks


def test_a_cold_legacy_pods_fronthaul_is_created_in_its_encoding(tmp_path):
    pod, _ = backend(tmp_path, vif=False)
    asyncio.run(pod.context())
    intent = Intent("pod-1", "radio-1", "bss-1", "emosa-mesh", "new")
    result = asyncio.run(pod.submit(intent, {"transaction_id": "t1", "session_generation": 1}))
    assert result.status == "committed"
    insert = next(op for ops in pod.session.sent for op in ops if op["op"] == "insert")
    row = insert["row"]
    assert row["if_name"] == "wl1.2" and row["ssid"] == "emosa-mesh"
    assert row["security"] == WANTED
    assert not {"multi_ap", "wpa", "wpa_psks", "wpa_key_mgmt"} & set(row)


# spec 8.4 on a legacy pod: the Ethernet uplink bridged by OpenSync's patch pair
UPLINK, WAN, HOME = (f"00000000-0000-4000-8000-0000000002{n}" for n in ("01", "02", "03"))
PW, PH = "00000000-0000-4000-8000-000000000211", "00000000-0000-4000-8000-000000000212"


class WiredPod:
    def __init__(self, ports=()):
        self.schema = Schema(reference_schema(LEGACY))
        self.sent = []
        port_rows = {PW: {"name": "patch-w2h"}, PH: {"name": "patch-h2w"}}
        self.tables = {
            "AWLAN_Node": {NODE: {"serial_number": SERIAL}},
            "Wifi_Radio_Config": {RADIO: {"if_name": "wl1", "freq_band": "2.4G"}},
            "Connection_Manager_Uplink": {
                UPLINK: {"if_name": "eth0", "if_type": "eth", "is_used": True}
            },
            "Port": {u: port_rows[u] for u in ports},
            "Bridge": {
                WAN: {"name": "br-wan", "ports": ["set", [["uuid", PW]] if PW in ports else []]},
                HOME: {"name": "br-home", "ports": ["set", [["uuid", PH]] if PH in ports else []]},
            },
        }

    async def snapshot(self):
        return {
            "tables": copy.deepcopy(self.tables),
            "generation": 1,
            "revision": 1,
            "ready": True,
            "schema": self.schema,
        }

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(operations)
        return [{"count": 1} if op["op"] == "mutate" else {} for op in operations]


def wired(pod):
    from emosa.opensync.wired import WiredBackend, WiredIntent

    backend = WiredBackend(
        "pod-1", pod, serial=SERIAL, port="eth0", legacy=True, home_bridge="br-home"
    )
    return backend, WiredIntent("pod-1", "eth0", "br-home")


def test_a_legacy_pods_uplink_is_bridged_by_writing_the_patch_pair():
    pod = WiredPod()
    backend, intent = wired(pod)
    assert asyncio.run(backend.snapshot()).config["bridge"] is None
    plan = asyncio.run(backend.plan(intent))
    assert plan["action"] == "wired-uplink-patch-pair" and plan["wan_bridge"] == "br-wan"
    attempt = {"transaction_id": "t1", "session_generation": 1}
    assert asyncio.run(backend.submit(intent, attempt)).status == "committed"
    (ops,) = pod.sent
    assert [op["op"] for op in ops] == ["wait"] * 4 + ["insert"] * 4 + ["mutate"] * 2
    assert "bridge" not in ops[1]["columns"]  # a legacy pod's row has none
    assert [op["where"] for op in ops[2:4]] == [
        [["name", "==", "patch-w2h"]],
        [["name", "==", "patch-h2w"]],
    ]
    interfaces = {op["row"]["name"]: op["row"] for op in ops[4:6]}
    assert interfaces["patch-w2h"] == {
        "name": "patch-w2h",
        "type": "patch",
        "options": ["map", [["peer", "patch-h2w"]]],
    }
    assert interfaces["patch-h2w"]["options"] == ["map", [["peer", "patch-w2h"]]]
    assert [(op["where"], op["mutations"][0][2]) for op in ops[8:]] == [
        ([["name", "==", "br-wan"]], ["set", [["named-uuid", "patch_w_port"]]]),
        ([["name", "==", "br-home"]], ["set", [["named-uuid", "patch_h_port"]]]),
    ]


def test_a_legacy_pods_pair_in_place_is_its_bridge_and_half_a_pair_is_refused():
    backend, _ = wired(WiredPod(ports=(PW, PH)))
    assert asyncio.run(backend.snapshot()).config["bridge"] == "br-home"
    backend, intent = wired(WiredPod(ports=(PW,)))
    with pytest.raises(EmosaError) as refused:
        asyncio.run(backend.plan(intent))
    assert refused.value.code == Reason.OWNERSHIP_CONFLICT


# spec 3.10, 8.6 on a legacy pod: a GRE child's move pinned by its station's parent
STA, STA_STATE, GRE_UPLINK = (f"00000000-0000-4000-8000-0000000003{n}" for n in ("01", "02", "03"))
PARENT_AP = "72:00:00:00:0a:01"


class GrePod(WiredPod):
    def __init__(self):
        super().__init__()
        self.tables["Wifi_Radio_Config"][RADIO]["vif_configs"] = ["uuid", STA]
        self.tables["Wifi_VIF_Config"] = {
            STA: {"if_name": "wl1", "mode": "sta", "enabled": True, "ssid": "pod-onboarding"}
        }
        self.tables["Wifi_VIF_State"] = {
            STA_STATE: {
                "if_name": "wl1",
                "mode": "sta",
                "enabled": True,
                "ssid": "pod-onboarding",
                "parent": "02:00:00:00:1b:02",
                "mac": "02:00:00:00:41:01",
            }
        }
        self.tables["Connection_Manager_Uplink"] = {
            GRE_UPLINK: {"if_name": "g-wl1", "if_type": "gre", "is_used": True}
        }

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(operations)
        return [{"count": 1} if op["op"] == "update" else {} for op in operations]


def test_a_legacy_gre_childs_move_pins_its_stations_parent(tmp_path):
    from emosa.opensync.uplink import GRE, UplinkBackend, UplinkIntent

    vault = SecretStore(tmp_path / "secrets")
    vault.write_simulated("backhaul", "EmosaBackhaul2026!")
    pod = GrePod()
    backend = UplinkBackend("pod-1", pod, vault, serial=SERIAL, station="wl1", legacy=True)
    intent = UplinkIntent("pod-1", "wl1", "emosa-lab-bh", "backhaul", mode=GRE, bssid=PARENT_AP)
    attempt = {"transaction_id": "t1", "session_generation": 1}
    result = asyncio.run(backend.submit(intent, attempt))
    assert result.status == "committed"
    (ops,) = pod.sent
    insert = next(op for op in ops if op["op"] == "insert")
    assert insert["row"] == {
        "ssid": "emosa-lab-bh",
        "security": ["map", [["encryption", "WPA-PSK"], ["key", "EmosaBackhaul2026!"]]],
        "onboard_type": "gre",
    }  # no bssid, priority or enabled: a legacy pod's credential has none
    update = next(op for op in ops if op["op"] == "update")
    assert update["row"]["parent"] == PARENT_AP and update["row"]["ssid"] == ""
    assert "multi_ap" not in update["row"]
    assert result.evidence["restore"]["ssid"] == "pod-onboarding"  # the revert's snapshot


def test_a_placed_radios_channel_must_be_one_emosa_maps(tmp_path):
    # spec 9: a radio the profile places is reported on its declared channel from the start
    from emosa.opensync.profiles import files

    assert load_profile("bcm-pod-2.4.3-gre-5g-v1").radios[0]["channel"] == 36
    data = json.loads(files("emosa").joinpath("profiles/bcm-pod-2.4.3-gre-5g-v1.json").read_text())
    data["radios"][0]["channel"] = 108  # DFS: never reported, so never configured
    path = tmp_path / "dfs.json"
    path.write_text(json.dumps(data))
    with pytest.raises(EmosaError) as refused:
        load_profile(str(path))
    assert refused.value.code == Reason.INVALID_INPUT
