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


# spec 3.10, the GTP station: written on a seeded pod, left alone where a station is
GTP_INET, GTP_CRED, GTP_STA, GTP_STATE = (
    f"00000000-0000-4000-8000-0000000004{n}" for n in ("01", "02", "03", "04")
)
GTP_KEY = "OnlySimulationGtp2026!"
GTP_PIN = "92:00:00:00:24:65"


class SeededPod(WiredPod):
    """A wired legacy pod as its vendor's seed has it: no station, wl1's Inet row off."""

    def __init__(self, inet=True):
        super().__init__()
        if inet:
            self.tables["Wifi_Inet_Config"] = {
                GTP_INET: {"if_name": "wl1", "if_type": "vif", "enabled": False}
            }

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(operations)
        return [
            {"count": 1} if op["op"] in ("mutate", "update", "delete") else {} for op in operations
        ]


def gtp(tmp_path, pod, bssid=None):
    from emosa.opensync.gtp_station import GtpStationBackend, GtpStationIntent

    vault = SecretStore(tmp_path / "secrets")
    vault.write_simulated("gtp", GTP_KEY)
    backend = GtpStationBackend("pod-1", pod, vault, serial=SERIAL, station="wl1", band="2.4G")
    return backend, GtpStationIntent("pod-1", "wl1", "site-gtp", "gtp", bssid), vault


def test_a_seeded_legacy_pods_gtp_station_is_written_in_one_transaction(tmp_path):
    pod = SeededPod()
    backend, intent, _ = gtp(tmp_path, pod)
    assert asyncio.run(backend.snapshot()).config["present"] is False
    plan = asyncio.run(backend.plan(intent))
    assert plan["action"] == "gtp-station" and plan["pinned"] is False
    attempt = {"transaction_id": "t1", "session_generation": 1}
    assert asyncio.run(backend.submit(intent, attempt)).status == "committed"
    (ops,) = pod.sent
    assert [op["op"] for op in ops] == ["wait", "wait", "insert", "insert", "mutate", "update"]
    assert ops[1]["where"] == [["if_name", "==", "wl1"]] and ops[1]["rows"] == []
    security = ["map", [["encryption", "WPA-PSK"], ["key", GTP_KEY]]]
    assert ops[2]["row"] == {"ssid": "site-gtp", "security": security, "onboard_type": "gre"}
    assert ops[3]["row"] == {
        "if_name": "wl1",
        "mode": "sta",
        "enabled": True,
        "vif_radio_idx": 0,
        "ssid": "site-gtp",
        "security": security,
        "credential_configs": ["set", [["named-uuid", "gtp_credential"]]],
    }  # vconf form, no pin by default
    assert ops[4]["mutations"] == [
        ["vif_configs", "insert", ["set", [["named-uuid", "gtp_station"]]]]
    ]
    assert ops[5]["row"] == {"enabled": True, "network": True}  # the seed's scheme stays


def test_a_pinned_gtp_station_on_a_pod_without_its_inet_row(tmp_path):
    pod = SeededPod(inet=False)
    backend, intent, _ = gtp(tmp_path, pod, bssid=GTP_PIN)
    attempt = {"transaction_id": "t1", "session_generation": 1}
    assert asyncio.run(backend.submit(intent, attempt)).status == "committed"
    (ops,) = pod.sent
    assert ops[3]["row"]["parent"] == GTP_PIN
    assert ops[5]["op"] == "insert" and ops[5]["row"] == {
        "if_name": "wl1",
        "if_type": "vif",
        "enabled": True,
        "network": True,
        "ip_assign_scheme": "dhcp",
    }


def test_a_station_already_there_is_left_alone(tmp_path):
    pod = SeededPod()
    pod.tables["Wifi_VIF_Config"] = {
        GTP_STA: {"if_name": "wl1", "mode": "sta", "enabled": True, "ssid": "hook-gtp"}
    }
    backend, intent, _ = gtp(tmp_path, pod)
    assert asyncio.run(backend.snapshot()).config["present"] is True
    with pytest.raises(EmosaError) as refused:
        asyncio.run(backend.plan(intent))
    assert refused.value.code == Reason.OWNERSHIP_CONFLICT
    assert not pod.sent


def test_the_written_station_reads_back_as_its_target_once_wm_shows_it(tmp_path):
    pod = SeededPod()
    backend, intent, vault = gtp(tmp_path, pod)
    security = ["map", [["encryption", "WPA-PSK"], ["key", GTP_KEY]]]
    pod.tables["Wifi_Credential_Config"] = {
        GTP_CRED: {"ssid": "site-gtp", "security": security, "onboard_type": "gre"}
    }
    pod.tables["Wifi_VIF_Config"] = {
        GTP_STA: {
            "if_name": "wl1",
            "mode": "sta",
            "enabled": True,
            "ssid": "site-gtp",
            "security": security,
            "credential_configs": ["set", [["uuid", GTP_CRED]]],
        }
    }
    target = intent.target(vault)
    config = asyncio.run(backend.snapshot()).config
    assert config["joined"] is False and config["ssid"] == "site-gtp"
    assert not all(config.get(k) == v for k, v in target.items())  # not applied before wm
    pod.tables["Wifi_VIF_State"] = {GTP_STATE: {"if_name": "wl1", "mode": "sta"}}
    config = asyncio.run(backend.snapshot()).config
    assert all(config.get(k) == v for k, v in target.items())


def test_a_gtp_station_intent_is_refused_without_a_valid_ssid_or_pin():
    from emosa.opensync.gtp_station import GtpStationIntent

    for bad in (
        GtpStationIntent("pod-1", "wl1", "x" * 33, "gtp"),
        GtpStationIntent("pod-1", "wl1", "site-gtp", "gtp", "92:00:00:00:24:6G"),
        GtpStationIntent("pod-1", "wl1/x", "site-gtp", "gtp"),
    ):
        with pytest.raises(EmosaError) as refused:
            bad.validate()
        assert refused.value.code == Reason.INVALID_INPUT


# spec 3.10, the radio plan (radio_plan emosa): the seed's unclaimed APs left on, their Inet rows
# off, the radio planned
OP_AP1, OP_AP2, OP_INET, OP_STATE = (
    f"00000000-0000-4000-8000-0000000005{n}" for n in ("01", "02", "03", "04")
)


class OperatorPod(SeededPod):
    """The seed with the operator's APs on wl0 (wl0.1 with its own Inet row on) and wl1 on
    channel 1 at HT40, beside the profile's own VIFs and the GTP station."""

    def __init__(self):
        super().__init__()
        self.tables["Wifi_Radio_Config"][RADIO].update({"channel": 1, "ht_mode": "HT40"})
        self.tables["Wifi_VIF_Config"] = {
            OP_AP1: {"if_name": "wl0.1", "mode": "ap", "enabled": True},
            OP_AP2: {"if_name": "wl0.2", "mode": "ap", "enabled": True},
            VIF: {"if_name": "wl1.2", "mode": "ap", "enabled": True},
            GTP_STA: {"if_name": "wl1", "mode": "sta", "enabled": True},
        }
        self.tables["Wifi_VIF_State"] = {OP_STATE: {"if_name": "wl0.1", "enabled": True}}
        self.tables["Wifi_Inet_Config"][OP_INET] = {"if_name": "wl0.1", "enabled": True}


def radio_plan(pod, tmp_path):
    from emosa.opensync.radio_plan import RadioPlanBackend, RadioPlanIntent

    vault = SecretStore(tmp_path / "secrets")
    intent = RadioPlanIntent("pod-1", ("wl1.1", "wl1.2"), "2.4G", 6, "HT20")
    return RadioPlanBackend("pod-1", pod, vault, serial=SERIAL, intent=intent), intent, vault


def neutral(key):
    """An unclaimed AP as the plan leaves it: on, hidden, out of any bridge, EMOSA's own key."""
    return {
        "security": ["map", [["encryption", "WPA-PSK"], ["key", key]]],
        "ssid_broadcast": "disabled",
        "bridge": "",
    }


def test_the_radio_plan_neutralizes_unclaimed_aps_and_plans_the_wired_radio(tmp_path):
    # an AP is never disabled: the 2.4.3 pods' vendor healthcheck reboots a pod whose AP stops
    # beaconing (14 October). It keeps beaconing, hidden, out of br-home, on EMOSA's own key, its
    # Inet row off (inet_vif leaves the link to wm)
    pod = OperatorPod()
    backend, intent, vault = radio_plan(pod, tmp_path)
    config = asyncio.run(backend.snapshot()).config
    assert config["unclaimed"] == ["wl0.1", "wl0.2"] and config["pending"] is True
    assert config["unclaimed_enabled"] == ["wl0.1", "wl0.2"]
    assert config["unclaimed_inet_on"] == ["wl0.1"]
    assert config["unclaimed_exposed"] == ["wl0.1", "wl0.2"]
    assert config["wired"] is True and config["radio_planned"] is False
    plan = asyncio.run(backend.plan(intent))
    attempt = {"transaction_id": "t1", "session_generation": 1}
    assert asyncio.run(backend.submit(intent, attempt)).status == "committed"
    key = vault.generated("emosa-unclaimed")
    assert len(key) == 32 and key not in json.dumps(plan)  # the plan names no key
    (ops,) = pod.sent
    updates = [(op["table"], op["where"], op["row"]) for op in ops if op["op"] == "update"]
    assert updates == [
        ("Wifi_VIF_Config", [["_uuid", "==", ["uuid", OP_AP1]]], neutral(key)),
        ("Wifi_VIF_Config", [["_uuid", "==", ["uuid", OP_AP2]]], neutral(key)),
        ("Wifi_Inet_Config", [["_uuid", "==", ["uuid", OP_INET]]], {"enabled": False}),
        (
            "Wifi_Radio_Config",
            [["_uuid", "==", ["uuid", RADIO]]],
            {"channel": 6, "ht_mode": "HT20"},
        ),
    ]  # never "enabled", never the station wl1 nor the profile's wl1.2
    radio_wait = [op for op in ops if op["op"] == "wait" and op["table"] == "Wifi_Radio_Config"]
    assert radio_wait[0]["rows"] == [{"if_name": "wl1", "channel": 1, "ht_mode": "HT40"}]


def test_the_radio_plan_leaves_the_radio_to_its_station_when_not_wired(tmp_path):
    pod = OperatorPod()
    pod.tables["Connection_Manager_Uplink"][UPLINK] = {
        "if_name": "g-wl1",
        "if_type": "gre",
        "is_used": True,
    }
    backend, intent, _ = radio_plan(pod, tmp_path)
    config = asyncio.run(backend.snapshot()).config
    assert config["wired"] is False and config["radio_planned"] is True
    attempt = {"transaction_id": "t1", "session_generation": 1}
    asyncio.run(backend.submit(intent, attempt))
    (ops,) = pod.sent
    assert not [op for op in ops if op["table"] == "Wifi_Radio_Config"]


def test_a_planned_pod_has_nothing_to_write(tmp_path):
    pod = OperatorPod()  # its unclaimed APs on, as the plan leaves them
    backend, intent, vault = radio_plan(pod, tmp_path)
    key = vault.generated("emosa-unclaimed")
    for uuid in (OP_AP1, OP_AP2):
        pod.tables["Wifi_VIF_Config"][uuid].update(neutral(key))
    pod.tables["Wifi_Inet_Config"][OP_INET]["enabled"] = False
    pod.tables["Wifi_Radio_Config"][RADIO].update({"channel": 6, "ht_mode": "HT20"})
    config = asyncio.run(backend.snapshot()).config
    assert config["pending"] is False and config["unclaimed_exposed"] == []
    assert all(config.get(k) == v for k, v in intent.target(vault).items())


STATS_CLIENT, STATS_SURVEY = (f"00000000-0000-4000-8000-0000000006{n}" for n in ("01", "02"))
OPERATOR_MQTT = {"broker": "cloud.example.net", "port": "443", "topics": "operator/stats"}


def with_operator_cloud(pod):
    """The seed's way to its operator's cloud: qm's broker, headers and topics, and the
    statistics rows it samples for them."""
    pod.tables["AWLAN_Node"][NODE].update(
        {
            "mqtt_settings": ["map", sorted(map(list, OPERATOR_MQTT.items()))],
            "mqtt_headers": ["map", [["locationId", "site-1"], ["nodeId", "node-1"]]],
            "mqtt_topics": ["map", []],
        }
    )
    pod.tables["Wifi_Stats_Config"] = {
        STATS_CLIENT: {"stats_type": "client", "radio_type": "2.4G"},
        STATS_SURVEY: {"stats_type": "survey", "radio_type": "5G"},
    }
    return pod


def test_the_radio_plan_clears_the_operators_cloud(tmp_path):
    # a converted pod talks only to EMOSA: qm's broker, headers and topics emptied, the
    # statistics rows removed, each as it is, in the plan's one write
    pod = with_operator_cloud(OperatorPod())
    backend, intent, _ = radio_plan(pod, tmp_path)
    config = asyncio.run(backend.snapshot()).config
    assert config["operator_cloud"] == ["mqtt_headers", "mqtt_settings", "Wifi_Stats_Config"]
    plan = asyncio.run(backend.plan(intent))
    assert plan["cloud_cleared"] == config["operator_cloud"]
    assert "AWLAN_Node.mqtt_settings" in plan["fields"] and "Wifi_Stats_Config" in plan["fields"]
    attempt = {"transaction_id": "t1", "session_generation": 1}
    assert asyncio.run(backend.submit(intent, attempt)).status == "committed"
    (ops,) = pod.sent
    node = [op for op in ops if op["table"] == "AWLAN_Node"]
    assert node[1]["columns"] == ["mqtt_headers", "mqtt_settings", "mqtt_topics"]
    assert node[1]["rows"] == [
        {
            "mqtt_headers": ["map", [["locationId", "site-1"], ["nodeId", "node-1"]]],
            "mqtt_settings": ["map", sorted(map(list, OPERATOR_MQTT.items()))],
            "mqtt_topics": ["map", []],
        }
    ]
    assert node[2]["op"] == "update" and node[2]["row"] == {
        "mqtt_headers": ["map", []],
        "mqtt_settings": ["map", []],
        "mqtt_topics": ["map", []],
    }
    stats = [op for op in ops if op["table"] == "Wifi_Stats_Config"]
    assert [(op["op"], op["where"]) for op in stats] == [
        (kind, [["_uuid", "==", ["uuid", uuid]]])
        for uuid in (STATS_CLIENT, STATS_SURVEY)
        for kind in ("wait", "delete")
    ]
    assert stats[0]["rows"] == [{"stats_type": "client", "radio_type": "2.4G"}]


def test_a_cleared_cloud_is_part_of_the_applied_plan(tmp_path):
    pod = with_operator_cloud(OperatorPod())
    backend, intent, vault = radio_plan(pod, tmp_path)
    key = vault.generated("emosa-unclaimed")
    for uuid in (OP_AP1, OP_AP2):
        pod.tables["Wifi_VIF_Config"][uuid].update(neutral(key))
    pod.tables["Wifi_Inet_Config"][OP_INET]["enabled"] = False
    pod.tables["Wifi_Radio_Config"][RADIO].update({"channel": 6, "ht_mode": "HT20"})
    config = asyncio.run(backend.snapshot()).config
    assert config["pending"] is True  # the cloud is still the operator's
    assert not all(config.get(k) == v for k, v in intent.target(vault).items())
    for column in ("mqtt_settings", "mqtt_headers"):
        pod.tables["AWLAN_Node"][NODE][column] = ["map", []]
    pod.tables["Wifi_Stats_Config"] = {}
    config = asyncio.run(backend.snapshot()).config
    assert config["pending"] is False and config["operator_cloud"] == []
    assert all(config.get(k) == v for k, v in intent.target(vault).items())


def test_the_agent_monitors_every_column_the_radio_plan_compares():
    # the session sees only the columns monitored: the neutral AP's hidden flag, bridge and key,
    # the radio's channel and width, the Inet rows and cm's uplink (the box found the hidden flag
    # missing: the plan was written and never seen applied), the cloud's and statistics' columns
    from emosa.agent.pod import MONITOR

    for table, columns in {
        "Wifi_VIF_Config": ("if_name", "mode", "enabled", "ssid_broadcast", "bridge", "security"),
        "Wifi_Radio_Config": ("channel", "ht_mode"),
        "Wifi_Inet_Config": ("if_name", "enabled"),
        "Connection_Manager_Uplink": ("if_name", "if_type", "is_used"),
        "AWLAN_Node": ("mqtt_settings", "mqtt_headers", "mqtt_topics"),
        "Wifi_Stats_Config": ("stats_type", "radio_type"),
    }.items():
        assert set(columns) <= set(MONITOR[table]), table


def test_emosas_own_credential_is_made_once_and_kept(tmp_path):
    vault = SecretStore(tmp_path / "secrets")
    first = vault.generated("emosa-unclaimed")
    assert len(first) == 32 and first.isalnum()
    assert SecretStore(tmp_path / "secrets").generated("emosa-unclaimed") == first
    with pytest.raises(EmosaError):
        vault.generated("wsc-" + "0" * 32)  # only an emosa- reference


def test_a_6_6_profile_may_not_take_the_radio_plan(tmp_path):
    from importlib.resources import files

    data = json.loads(
        files("emosa").joinpath("profiles/opensync-lab-hwsim-6.6.1-v1.json").read_text()
    )
    data["radio_plan"] = "emosa"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data))
    with pytest.raises(EmosaError) as refused:
        load_profile(str(path))
    assert refused.value.code == Reason.INVALID_INPUT
