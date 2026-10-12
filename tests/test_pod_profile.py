# SPDX-License-Identifier: Apache-2.0
"""opensync-lab-hwsim-6.6.1-v1: the real OpenSync 6.6 VIF encoding, offline."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from emosa.errors import EmosaError, Reason
from emosa.model import Intent, RadioIntent
from emosa.opensync.pod_profile import PodBackend, RadioBackend, wpa2_psk
from emosa.opensync.schema import Schema, reference_path
from emosa.secrets import SecretStore

pytestmark = pytest.mark.unit

NODE, RADIO, RADIO_STATE = (f"00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))
VIF, VIF_STATE = "00000000-0000-4000-8000-000000000004", "00000000-0000-4000-8000-000000000005"
RADIO_5G, RADIO_STATE_5G = (f"00000000-0000-4000-8000-00000000000{n}" for n in (6, 7))
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


def with_second_radio(raw, *, channel=36):
    """Add a 5 GHz radio (spec 9's second radio) to a one-radio fixture, with no VIFs of its own."""
    raw["Wifi_Radio_Config"][RADIO_5G] = {
        "if_name": "wlan1",
        "freq_band": "5G",
        "enabled": True,
        "vif_configs": ["set", []],
    }
    raw["Wifi_Radio_State"][RADIO_STATE_5G] = {
        "if_name": "wlan1",
        "radio_config": ["uuid", RADIO_5G],
        "vif_states": ["set", []],
        "freq_band": "5G",
        "channel": channel,
        "mac": "02:00:00:00:1b:00",
        "enabled": True,
        "country": "US",
    }
    return raw


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
    assert ops[3]["row"] == {"ssid": "emosa-mesh", "multi_ap": "fronthaul_bss"}  # the M2's role
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
    assert vif["multi_ap"] == "fronthaul_bss"  # the role its M2 gives it (finding 21)
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
    assert {inserts[n]["multi_ap"] for n in ("svc-d-ap-24", "svc-e-ap-24", "fh-24")} == {
        "fronthaul_bss"
    }
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


def gre_parent(tmp_path, *, underlay="169.254.2.0/24", raw=None, **gre):
    """A multi-BSS pod whose profile makes a backhaul BSS its GRE parent AP (spec 8.6)."""
    from emosa.opensync import profiles

    data = json.loads(
        (Path(profiles.__file__).parents[1] / "profiles" / f"{profiles.DEFAULT}.json").read_text()
    )
    data.update(id="gre-parent-pod-v1", backhaul_mode="gre-parent", gre_parent=gre)
    path = tmp_path / "gre-parent.json"
    path.write_text(json.dumps(data))
    _, vault = multi(tmp_path)
    profile = profiles.load(str(path))
    return PodBackend(
        "pod-1",
        Session(raw or tables()),
        vault,
        serial=SERIAL,
        multi_bss=True,
        profile=profile,
        underlay=underlay,
    )


def submitted(pod, intent):
    asyncio.run(pod.context())
    result = asyncio.run(pod.submit(intent, {"transaction_id": "t", "session_generation": 1}))
    assert result.status == "committed"
    rows = {}
    for op in pod.session.sent[-1]:
        if op["op"] in ("insert", "update") and "if_name" in op.get("row", {}):
            rows[op["table"], op["row"]["if_name"]] = op["row"]
    return rows


def test_a_gre_parent_pods_backhaul_bss_is_its_parent_ap(tmp_path):
    pod = gre_parent(tmp_path)
    rows = submitted(pod, rdk_intent())
    ap = rows["Wifi_VIF_Config", "b-ap-24"]
    assert (ap["ssid"], ap["multi_ap"], ap["bridge"], ap["ap_bridge"], ap["ssid_broadcast"]) == (
        "mesh_backhaul",
        "none",
        "",
        False,
        "disabled",
    )
    assert ap["wpa_psks"] == ["map", [["key", "ExtraKey3-2026"]]] and ap["vif_radio_idx"] == 1
    assert rows["Wifi_Inet_Config", "b-ap-24"] == {
        "if_name": "b-ap-24",
        "if_type": "vif",
        "enabled": True,
        "network": True,
        "NAT": False,
        "mtu": 1600,
        "ip_assign_scheme": "static",
        "inet_addr": "169.254.2.1",
        "netmask": "255.255.255.0",
        "dhcpd": [
            "map",
            [["start", "169.254.2.10"], ["stop", "169.254.2.250"], ["lease_time", "12h"]],
        ],
    }
    # the fronthaul slots stay Multi-AP, and the parent AP is a backhaul BSS for the
    # configured set and the view
    assert rows["Wifi_VIF_Config", "fh-24"]["multi_ap"] == "fronthaul_bss"
    assert pod.role({"if_name": "b-ap-24", "multi_ap": "none"}) == "backhaul"
    assert pod.parent_aps == {"b-ap-24"}


def test_a_profile_may_declare_additional_radios(tmp_path):
    """Spec 9 / 8.6 second radio: a profile may list radios EMOSA binds beyond the fronthaul's
    `radio` (the radio carrying the uplink station, and, for a parent, the parent AP on it)."""
    from emosa.opensync import profiles

    data = json.loads(
        (Path(profiles.__file__).parents[1] / "profiles" / f"{profiles.DEFAULT}.json").read_text()
    )
    data["radios"] = [{"band": "5G", "ht_mode": "HT40", "chipset": "mac80211_hwsim"}]
    path = tmp_path / "two-radio.json"
    path.write_text(json.dumps(data))
    profile = profiles.load(str(path))
    assert profile.band == "2.4G"  # the fronthaul radio is unchanged
    assert profile.radios == ({"band": "5G", "ht_mode": "HT40", "chipset": "mac80211_hwsim"},)


def test_a_profile_without_additional_radios_has_an_empty_radios_tuple():
    from emosa.opensync import profiles

    assert profiles.load().radios == ()
    assert profiles.load().slot_bands == ()  # default: every slot on the fronthaul radio


PARENT_AP_50 = "00000000-0000-4000-8000-000000000008"
BACKHAUL_50 = {"role": "backhaul", "ssid": "mesh_backhaul", "secret_ref": "x3"}


def parent_5g(tmp_path, *, raw=None, multi_bss=True):
    """A GRE parent whose parent AP b-ap-50 is on its 5 GHz radio (spec 9, the Pis' adapter B):
    the fronthaul's scope and the 5 GHz radio's scope over one pod."""
    from emosa.opensync import profiles

    data = json.loads(
        (Path(profiles.__file__).parents[1] / "profiles" / f"{profiles.DEFAULT}.json").read_text()
    )
    data.update(id="gre-parent-5g", backhaul_mode="gre-parent", gre_parent={})
    data["radios"] = [{"band": "5G", "ht_mode": "HT40", "chipset": "mac80211_hwsim"}]
    data["extra_slots"] = [
        {"if_name": "b-ap-50", "role": "backhaul", "vif_radio_idx": 1, "band": "5G"},
        {"if_name": "fh-24", "role": "fronthaul", "vif_radio_idx": 6},
    ]
    path = tmp_path / "gre-5g.json"
    path.write_text(json.dumps(data))
    profile = profiles.load(str(path))
    _, vault = multi(tmp_path)
    session = Session(raw or with_second_radio(tables()))
    common = {"serial": SERIAL, "multi_bss": multi_bss, "profile": profile}
    fronthaul = PodBackend("pod-1", session, vault, underlay="169.254.2.0/24", **common)
    radio = RadioBackend("pod-1", session, vault, band="5G", underlay="169.254.2.0/24", **common)
    return fronthaul, radio, vault


def with_parent_ap_50(raw=None):
    """The 5 GHz radio carrying b-ap-50, as an earlier M2 of that radio made it (Config + State)."""
    raw = raw or with_second_radio(tables())
    raw["Wifi_VIF_Config"][PARENT_AP_50] = {
        "if_name": "b-ap-50",
        "mode": "ap",
        "ssid": "mesh_backhaul",
        "enabled": True,
        "multi_ap": "none",
        **SECURITY,
        "wpa_psks": ["map", [["key", "ExtraKey3-2026"]]],
    }
    raw["Wifi_Radio_Config"][RADIO_5G]["vif_configs"] = ["set", [["uuid", PARENT_AP_50]]]
    raw["Wifi_VIF_State"]["00000000-0000-4000-8000-000000000009"] = {
        **raw["Wifi_VIF_Config"][PARENT_AP_50],
        "vif_config": ["uuid", PARENT_AP_50],
        "mac": "72:00:00:00:1b:00",
        "associated_clients": ["set", []],
    }
    return raw


def test_each_radio_scope_owns_the_slots_on_its_own_radio(tmp_path):
    """Spec 9: one M2 configures one radio, so each radio's scope owns the slots on that radio:
    the fronthaul's scope fh-24, the 5 GHz radio's scope b-ap-50. The report and the GRE parent
    see every slot of the pod."""
    fronthaul, radio, _ = parent_5g(tmp_path)
    assert [name for name, _, _ in fronthaul.slots] == ["fh-24"] and fronthaul.max_bss == 2
    assert [name for name, _, _ in radio.slots] == ["b-ap-50"] and radio.max_bss == 1
    assert {name for name, _, _ in fronthaul.pod_slots} == {"fh-24", "b-ap-50"}
    assert fronthaul.parent_aps == radio.parent_aps == {"b-ap-50"}
    snap = asyncio.run(radio.snapshot())
    assert snap.ready and radio.identity["radio_mac"] == "02:00:00:00:1b:00"
    assert radio.radio_id == "radio-5G" and radio.if_name is None
    assert snap.config == {"additional": []}
    # a radio the profile does not bind, or the fronthaul's own, is no radio scope
    for band in ("2.4G", "6G"):
        with pytest.raises(EmosaError):
            RadioBackend(
                "pod-1", radio.session, radio.vault, serial=SERIAL, band=band, profile=radio.profile
            )


def test_a_parent_ap_on_5g_is_created_by_its_radios_own_scope(tmp_path):
    """Spec 9: the 5 GHz radio's M2 set, fitted to its slot b-ap-50, creates the parent AP on
    that radio: inserted into its vif_configs, with the underlay's Inet row; the radio's channel
    and ht_mode are never written (the radio follows the pod's station) and only the radio's
    identity is guarded (the pod's own station is in its vif_configs too)."""
    _, radio, _ = parent_5g(tmp_path)
    rows = submitted(radio, RadioIntent("pod-1", "radio-5G", (BACKHAUL_50,)))
    ap = rows["Wifi_VIF_Config", "b-ap-50"]
    assert (ap["ssid"], ap["multi_ap"], ap["bridge"], ap["vif_radio_idx"]) == (
        "mesh_backhaul",
        "none",
        "",
        1,
    )
    assert rows["Wifi_Inet_Config", "b-ap-50"]["inet_addr"] == "169.254.2.1"
    ops = radio.session.sent[-1]
    radio_ops = [op for op in ops if op["table"] == "Wifi_Radio_Config"]
    assert [op["op"] for op in radio_ops] == ["wait", "mutate"]
    assert radio_ops[0]["columns"] == ["if_name"]
    assert radio_ops[1]["where"] == [["_uuid", "==", ["uuid", RADIO_5G]]]
    assert radio_ops[1]["mutations"] == [
        ["vif_configs", "insert", ["set", [["named-uuid", "extra0"]]]]
    ]
    assert not any(
        op["table"] == "Wifi_VIF_Config" and op.get("row", {}).get("if_name") == "fh-24"
        for op in ops
    )


def test_the_fronthauls_scope_leaves_the_second_radios_slots_alone(tmp_path):
    """Spec 9: the 2.4 GHz radio's M2 set never writes b-ap-50: its backhaul BSS has no slot on
    that radio (left out, spec 3.4), and b-ap-50 on the 5 GHz radio is not one of its slots."""
    fronthaul, _, _ = parent_5g(tmp_path, raw=with_parent_ap_50())
    snap = asyncio.run(fronthaul.snapshot())
    assert snap.config["additional"] == []  # fh-24 absent; b-ap-50 is the other scope's
    submitted(fronthaul, Intent("pod-1", "radio-1", "bss-1", "emosa-mesh", "new", additional=()))
    assert not any(
        PARENT_AP_50 in json.dumps(op) or RADIO_5G in json.dumps(op)
        for op in fronthaul.session.sent[-1]
    )


def test_a_radio_set_without_its_backhaul_bss_removes_the_parent_ap(tmp_path):
    """Spec 9 / 8.6 withdrawal by band: the 5 GHz radio's M2 set without a backhaul BSS fits none
    of its slots, so its scope deletes b-ap-50 from that radio (and the 2.4 GHz radio is not
    touched)."""
    _, radio, vault = parent_5g(tmp_path, raw=with_parent_ap_50())
    snap = asyncio.run(radio.snapshot())
    applied = [["backhaul", "mesh_backhaul", vault.fingerprint("ExtraKey3-2026")]]
    assert snap.config["additional"] == snap.observed.values["additional"] == applied
    asyncio.run(radio.context())
    asyncio.run(
        radio.submit(
            RadioIntent("pod-1", "radio-5G", ()), {"transaction_id": "t", "session_generation": 1}
        )
    )
    ops = radio.session.sent[-1]
    assert {
        "op": "delete",
        "table": "Wifi_VIF_Config",
        "where": [["_uuid", "==", ["uuid", PARENT_AP_50]]],
    } in ops
    removal = [op for op in ops if op["op"] == "mutate" and op["table"] == "Wifi_Radio_Config"]
    assert removal == [
        {
            "op": "mutate",
            "table": "Wifi_Radio_Config",
            "where": [["_uuid", "==", ["uuid", RADIO_5G]]],
            "mutations": [["vif_configs", "delete", ["set", [["uuid", PARENT_AP_50]]]]],
        }
    ]
    assert RADIO not in json.dumps(ops)


def test_a_radio_scope_journals_its_own_operations(tmp_path):
    """Spec 9: a radio scope's engine takes RadioIntents through the journal (the operation
    schema's radio-scope variant) to the pod, and finds an applied set applied."""
    from emosa.reconcile import Engine
    from emosa.store import Store

    _, radio, _ = parent_5g(tmp_path, raw=with_parent_ap_50())
    engine = Engine(
        Store(tmp_path / "radio-5G"), radio.vault, {"pod-1": radio}, intent_type=RadioIntent
    )

    async def run(intent, key):
        op = engine.request(intent, source="test", key=key, run_id="r")
        return await engine.execute(op.operation_id)

    same = asyncio.run(run(RadioIntent("pod-1", "radio-5G", (BACKHAUL_50,)), "k1"))
    assert same.state == "OBSERVED_APPLIED" and same.changed is False
    withdrawn = asyncio.run(run(RadioIntent("pod-1", "radio-5G", ()), "k2"))
    assert withdrawn.state == "CONFIG_COMMITTED" and withdrawn.intent["additional"] == []
    with pytest.raises(EmosaError):  # a fronthaul intent is not this scope's
        asyncio.run(radio.plan(Intent("pod-1", "radio-1", "bss-1", "a", "new", additional=())))


def test_a_slot_on_a_radio_the_profile_does_not_bind_is_refused(tmp_path):
    from emosa.opensync import profiles

    data = json.loads(
        (Path(profiles.__file__).parents[1] / "profiles" / f"{profiles.DEFAULT}.json").read_text()
    )
    data["extra_slots"] = [
        {"if_name": "b-ap-50", "role": "backhaul", "vif_radio_idx": 1, "band": "5G"}
    ]
    path = tmp_path / "no-5g.json"
    path.write_text(json.dumps(data))
    with pytest.raises(EmosaError):
        profiles.load(str(path))


def test_an_extra_slot_may_name_the_radio_its_vif_is_on(tmp_path):
    """Spec 9: a slot's `band` places its VIF on a non-fronthaul radio (the parent AP on 5 GHz)."""
    from emosa.opensync import profiles

    data = json.loads(
        (Path(profiles.__file__).parents[1] / "profiles" / f"{profiles.DEFAULT}.json").read_text()
    )
    data["radios"] = [{"band": "5G", "ht_mode": "HT40", "chipset": "mac80211_hwsim"}]
    data["extra_slots"] = [
        {"if_name": "b-ap-50", "role": "backhaul", "vif_radio_idx": 1, "band": "5G"},
        {"if_name": "fh-24", "role": "fronthaul", "vif_radio_idx": 6},
    ]
    path = tmp_path / "slot-band.json"
    path.write_text(json.dumps(data))
    profile = profiles.load(str(path))
    # only the banded slot is listed; fh-24 (no band) defaults to the fronthaul radio
    assert profile.slot_bands == (("b-ap-50", "5G"),)


def test_a_gre_parent_slot_present_gets_the_underlays_inet_row(tmp_path):
    raw = with_slot_vif("b-ap-24")
    raw["Wifi_VIF_Config"][EXTRA].update(multi_ap="backhaul_bss", bridge="br-home")
    pod = gre_parent(tmp_path, raw=raw)

    async def inet_names(*_args, **_kwargs):
        return [{"rows": [{"if_name": "b-ap-24"}]}]

    session_transact = pod.session.transact

    async def transact(operations, *args, **kwargs):
        if operations[0]["op"] == "select" and operations[0]["table"] == "Wifi_Inet_Config":
            return await inet_names()
        return await session_transact(operations, *args, **kwargs)

    pod.session.transact = transact
    submitted(pod, rdk_intent())
    update = next(
        op
        for op in pod.session.sent[-1]
        if op["op"] == "update"
        and op["table"] == "Wifi_VIF_Config"
        and op["where"] == [["_uuid", "==", ["uuid", EXTRA]]]
    )
    # from a Multi-AP backhaul BSS in br-home to the parent AP
    assert (update["row"]["multi_ap"], update["row"]["bridge"]) == ("none", "")
    inet = next(
        op
        for op in pod.session.sent[-1]
        if op["table"] == "Wifi_Inet_Config" and op.get("where") == [["if_name", "==", "b-ap-24"]]
    )
    assert inet["op"] == "update" and inet["row"]["inet_addr"] == "169.254.2.1"


def test_the_profile_may_broadcast_the_parent_and_set_its_inet_columns(tmp_path):
    pod = gre_parent(
        tmp_path,
        vif={"ssid_broadcast": "enabled", "multi_ap": "backhaul_bss", "bridge": "br-home"},
        inet={"mtu": 1500, "inet_addr": "10.0.0.1"},
    )
    rows = submitted(pod, rdk_intent())
    ap, inet = rows["Wifi_VIF_Config", "b-ap-24"], rows["Wifi_Inet_Config", "b-ap-24"]
    # its choice of broadcast; never Multi-AP or bridged, and the address is the underlay's
    assert (ap["ssid_broadcast"], ap["multi_ap"], ap["bridge"]) == ("enabled", "none", "")
    assert (inet["mtu"], inet["inet_addr"]) == (1500, "169.254.2.1")


def test_a_gre_parent_without_an_underlay_is_refused(tmp_path):
    pod = gre_parent(tmp_path, underlay=None)
    with pytest.raises(EmosaError) as error:
        asyncio.run(pod.plan(rdk_intent()))
    assert error.value.code == Reason.UNSUPPORTED_OPERATION
    asyncio.run(pod.plan(rdk_intent(3)))  # a set without a backhaul BSS needs none


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


def test_a_pod_whose_database_was_rebuilt_is_the_same_pod(tmp_path):
    """OpenSync restarts rebuild the database: new UUIDs, same serial and radio."""
    pod, _ = backend(tmp_path)
    first = asyncio.run(pod.context())
    rebuilt = json.loads(
        json.dumps(pod.session.tables)
        .replace(NODE, "00000000-0000-4000-8000-0000000000a1")
        .replace(RADIO, "00000000-0000-4000-8000-0000000000a2")
    )
    pod.session.tables = rebuilt
    second = asyncio.run(pod.context())
    assert second.binding_token != first.binding_token  # a new graph, accepted
