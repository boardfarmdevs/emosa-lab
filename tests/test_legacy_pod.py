# SPDX-License-Identifier: Apache-2.0
"""spec 3.10: legacy pods, the OpenSync 2.0 schema (no wpa_* columns or multi_ap), offline."""

import asyncio
import copy
import dataclasses
import hashlib
import json

import pytest

from emosa.errors import EmosaError, Reason
from emosa.model import Intent
from emosa.opensync.pod_profile import LEGACY_VIF_GUARDS, PHY_UP, PodBackend
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
# the write that changes the AP's ssid or key: its oftag marked, a full reload of the authenticator
MARKED = ["map", [*WANTED[1][:2], ["oftag", "home--1~reload"]]]


def test_the_legacy_parent_profiles_broadcast_their_parent_ap(tmp_path):
    """Spec 3.10, 8.6: both legacy GRE profiles' parent AP wl1.1 broadcasts its SSID, as the Pis'
    b-ap-50 does. A child's station finds a hidden parent only by probing for it, and pi2's moves
    onto a converted pod's hidden wl1.1 spent 31 and 48 s of the 90 s deadline scanning (four
    empty scans), its move back to a broadcasting parent none. Only the parent AP changes: the
    fronthaul broadcasts as before and the profile's backhaul row stays hidden."""
    vault = SecretStore(tmp_path / "secrets")
    for name in (PROFILE, "bcm-pod-2.4.3-gre-5g-v1"):
        profile = load_profile(name)
        pod = PodBackend("pod-1", Session(tables()), vault, serial=SERIAL, profile=profile)
        ap = pod._parent_vif()
        assert (ap["ssid_broadcast"], ap["bridge"], ap["ap_bridge"]) == ("enabled", "", False), name
        assert "multi_ap" not in ap, name
        assert profile.fronthaul_vif["ssid_broadcast"] == "enabled", name
        assert profile.backhaul_row["ssid_broadcast"] == "disabled", name


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
    assert ops[3]["row"] == {"ssid": "emosa-mesh", "security": MARKED}  # no multi_ap, no wpa_psks


def test_a_cold_legacy_pods_fronthaul_is_created_in_its_encoding(tmp_path):
    pod, _ = backend(tmp_path, vif=False)
    asyncio.run(pod.context())
    intent = Intent("pod-1", "radio-1", "bss-1", "emosa-mesh", "new")
    result = asyncio.run(pod.submit(intent, {"transaction_id": "t1", "session_generation": 1}))
    assert result.status == "committed"
    insert = next(op for ops in pod.session.sent for op in ops if op["op"] == "insert")
    row = insert["row"]
    assert row["if_name"] == "wl1.2" and row["ssid"] == "emosa-mesh"
    assert row["security"] == MARKED  # made: its oftag marked too
    assert not {"multi_ap", "wpa", "wpa_psks", "wpa_key_mgmt"} & set(row)


def test_a_legacy_write_that_keeps_the_ssid_and_key_leaves_the_oftag(tmp_path):
    pod, vault = backend(tmp_path)
    vault.write_simulated("same", "operator-psk")
    asyncio.run(pod.context())
    intent = Intent("pod-1", "radio-1", "bss-1", "operator-home", "same")
    asyncio.run(pod.submit(intent, {"transaction_id": "t1", "session_generation": 1}))
    (ops,) = pod.session.sent
    assert ops[3]["row"]["security"] == [
        "map",
        [["encryption", "WPA-PSK"], ["key", "operator-psk"], ["oftag", "home--1"]],
    ]


def marked(raw, oftag, *, state=True, ssid="emosa-mesh"):
    """The pod's fronthaul after EMOSA's write with ``oftag``, wm having taken it (state) or not."""
    security = ["map", [["encryption", "WPA-PSK"], ["key", "EmosaMesh2026!"], ["oftag", oftag]]]
    raw["Wifi_VIF_Config"][VIF] |= {"ssid": ssid, "security": security}
    if state:
        raw["Wifi_VIF_State"][VIF_STATE] |= {"ssid": ssid, "security": security}
    return raw


def test_auth_reload_restores_the_oftag_once_wm_has_taken_the_write(tmp_path):
    pod, _ = backend(tmp_path)
    marked(pod.session.tables, "home--1~reload")
    assert asyncio.run(pod.auth_reload()) == ["wl1.2"]
    (ops,) = pod.session.sent
    assert ops[0]["op"] == "wait" and ops[0]["columns"] == ["if_name", "ssid", "security"]
    assert ops[1] == {
        "op": "mutate",
        "table": "Wifi_VIF_Config",
        "where": [["_uuid", "==", ["uuid", VIF]]],
        "mutations": [
            ["security", "delete", ["set", ["oftag"]]],
            ["security", "insert", ["map", [["oftag", "home--1"]]]],
        ],
    }
    assert pod.auth_reloads == 1


def test_auth_reload_removes_a_bare_marker(tmp_path):
    pod, _ = backend(tmp_path)
    marked(pod.session.tables, "~reload")  # a row without an oftag of its own (a backhaul's)
    assert asyncio.run(pod.auth_reload()) == ["wl1.2"]
    assert pod.session.sent[0][1]["mutations"] == [["security", "delete", ["set", ["oftag"]]]]


@pytest.mark.parametrize(
    "case",
    ["not taken", "old ssid in state", "unmarked"],
)
def test_auth_reload_waits_for_wm_and_leaves_other_rows(tmp_path, case):
    pod, _ = backend(tmp_path)
    raw = pod.session.tables
    if case == "not taken":
        marked(raw, "home--1~reload", state=False)
    elif case == "old ssid in state":
        marked(raw, "home--1~reload")
        raw["Wifi_VIF_State"][VIF_STATE]["ssid"] = "operator-home"
    else:
        marked(raw, "home--1")
    assert asyncio.run(pod.auth_reload()) == []
    assert pod.session.sent == [] and pod.auth_reloads == 0


def test_auth_reload_is_a_legacy_pods_only(tmp_path):
    vault = SecretStore(tmp_path / "secrets")
    profile = load_profile("opensync-lab-hwsim-6.6.1-v1")
    pod = PodBackend("pod-1", Session(tables()), vault, serial=SERIAL, profile=profile)
    marked(pod.session.tables, "home--1~reload")
    assert asyncio.run(pod.auth_reload()) == [] and pod.session.sent == []
    row = pod._secured({"ssid": "s", "security": ["map", [["oftag", "home--1"]]]}, "k")
    assert row["wpa_psks"] == ["map", [["key", "k"]]]  # no marker: the 6.6 encoding as before


# spec 3.10: an AP of EMOSA's in its bridge, the bridge's Open vSwitch Port written by EMOSA
BRIDGE, HOME_PORT, FH_PORT = (f"00000000-0000-4000-8000-0000000001{n}" for n in ("10", "11", "12"))


def bridged(raw, ports=(), members=()):
    """The fronthaul naming br-home (its State shows it whatever the ports: the pod's NVRAM), and
    br-home with its own port and ``members``; ``ports`` further Ports outside any bridge."""
    raw["Wifi_VIF_Config"][VIF]["bridge"] = "br-home"
    raw["Wifi_VIF_State"][VIF_STATE]["bridge"] = "br-home"
    inside = {"br-home": HOME_PORT, **dict(members)}
    raw["Bridge"] = {
        BRIDGE: {"name": "br-home", "ports": ["set", [["uuid", u] for u in inside.values()]]}
    }
    raw["Port"] = {u: {"name": n} for n, u in {**inside, **dict(ports)}.items()}
    return raw


def test_an_ap_not_in_its_bridge_gets_its_port_and_its_oftag_marked(tmp_path):
    pod, _ = backend(tmp_path)
    raw = bridged(pod.session.tables)
    assert asyncio.run(pod.bridge_ports()) == ["wl1.2"]
    (ops,) = pod.session.sent
    assert ops[0] == {
        "op": "wait",
        "table": "Bridge",
        "where": [["_uuid", "==", ["uuid", BRIDGE]]],
        "columns": ["name", "ports"],
        "until": "==",
        "rows": [{"name": "br-home", "ports": raw["Bridge"][BRIDGE]["ports"]}],
        "timeout": 0,
    }
    assert ops[1]["table"] == "Wifi_VIF_Config"
    assert ops[1]["columns"] == ["if_name", "enabled", "bridge", "security"]
    assert ops[2] == {
        "op": "wait",
        "table": "Port",
        "where": [["name", "==", "wl1.2"]],
        "columns": ["name"],
        "until": "==",
        "rows": [],
        "timeout": 0,
    }
    assert ops[3:6] == [
        {"op": "insert", "table": "Interface", "uuid-name": "ap0_if", "row": {"name": "wl1.2"}},
        {
            "op": "insert",
            "table": "Port",
            "uuid-name": "ap0_port",
            "row": {"name": "wl1.2", "interfaces": ["set", [["named-uuid", "ap0_if"]]]},
        },
        {
            "op": "mutate",
            "table": "Bridge",
            "where": [["_uuid", "==", ["uuid", BRIDGE]]],
            "mutations": [["ports", "insert", ["set", [["named-uuid", "ap0_port"]]]]],
        },
    ]
    assert ops[6]["mutations"] == [
        ["security", "delete", ["set", ["oftag"]]],
        ["security", "insert", ["map", [["oftag", "home--1~reload"]]]],
    ]
    assert len(ops) == 7 and pod.ports_added == 1


def test_an_ap_already_marked_gets_its_port_alone_and_its_restore_waits_for_it(tmp_path):
    pod, _ = backend(tmp_path)
    raw = bridged(marked(pod.session.tables, "home--1~reload"))
    assert asyncio.run(pod.auth_reload()) == []  # the port first
    assert asyncio.run(pod.bridge_ports()) == ["wl1.2"]
    (ops,) = pod.session.sent
    assert len(ops) == 6 and ops[-1]["table"] == "Bridge"  # no second marker
    bridged(raw, members={"wl1.2": FH_PORT})  # the port there, as wm sees it
    assert asyncio.run(pod.auth_reload()) == ["wl1.2"]


@pytest.mark.parametrize(
    "case",
    ["in its bridge", "a port elsewhere", "not taken", "off", "no bridge", "not the profile's"],
)
def test_bridge_ports_leaves_an_ap_it_need_not_or_may_not_bridge(tmp_path, case):
    pod, _ = backend(tmp_path)
    raw = bridged(pod.session.tables)
    if case == "in its bridge":
        bridged(raw, members={"wl1.2": FH_PORT})
    elif case == "a port elsewhere":  # another manager's
        bridged(raw, ports={"wl1.2": FH_PORT})
    elif case == "not taken":
        raw["Wifi_VIF_State"] = {}
    elif case == "off":
        raw["Wifi_VIF_Config"][VIF]["enabled"] = False
    elif case == "no bridge":
        raw["Bridge"] = {}
    else:
        for table in ("Wifi_VIF_Config", "Wifi_VIF_State"):
            next(iter(raw[table].values()))["if_name"] = "wl1.3"
    assert asyncio.run(pod.bridge_ports()) == []
    assert pod.session.sent == [] and pod.ports_added == 0


WAN_BRIDGE, WAN_PORT, HOME_INET, WAN_INET = (
    f"00000000-0000-4000-8000-0000000001{n}" for n in ("20", "21", "22", "23")
)


def hooked(raw):
    """A hooked boot's bridges (15 October): the fronthaul in br-home's ports, br-home's Inet row
    down, br-wan's NAT on, no patch pair."""
    bridged(raw, members={"wl1.2": FH_PORT})
    raw["Bridge"][WAN_BRIDGE] = {"name": "br-wan", "ports": ["uuid", WAN_PORT]}
    raw["Port"][WAN_PORT] = {"name": "br-wan"}
    raw["Wifi_Inet_Config"] = {
        HOME_INET: {"if_name": "br-home", "if_type": "bridge", "enabled": True, "network": False},
        WAN_INET: {"if_name": "br-wan", "if_type": "bridge", "enabled": True, "NAT": True},
    }
    return raw


def test_a_hooked_boots_bridge_mode_is_written_in_one_guarded_transaction(tmp_path):
    pod, _ = backend(tmp_path)
    hooked(pod.session.tables)
    assert asyncio.run(pod.bridge_mode()) == [
        "br-home up",
        "br-wan NAT off",
        "patch pair",
        "wl1.2 group_rekey, ft_psk",
    ]
    (ops,) = pod.session.sent
    assert len(ops) == 15 and pod.bridge_modes == 1
    assert ops[0]["op"] == "wait" and ops[0]["columns"] == ["if_name", "enabled", "network"]
    assert ops[1] == {
        "op": "update",
        "table": "Wifi_Inet_Config",
        "where": [["_uuid", "==", ["uuid", HOME_INET]]],
        "row": {"enabled": True, "network": True, "NAT": False},
    }
    assert ops[3]["row"] == {"NAT": False}
    assert [o["table"] for o in ops[4:12]] == ["Port"] * 2 + ["Interface"] * 2 + ["Port"] * 2 + [
        "Bridge"
    ] * 2
    assert ops[6]["row"] == {
        "name": "patch-w2h",
        "type": "patch",
        "options": ["map", [["peer", "patch-h2w"]]],
    }
    assert ops[13]["row"] == {"group_rekey": 86400, "ft_psk": 0}
    assert ops[14]["mutations"][1] == ["security", "insert", ["map", [["oftag", "home--1~reload"]]]]


def test_auth_reload_waits_for_the_bridge_mode(tmp_path):
    pod, _ = backend(tmp_path)
    raw = hooked(marked(pod.session.tables, "home--1~reload"))
    assert asyncio.run(pod.auth_reload()) == []
    raw["Wifi_Inet_Config"][HOME_INET] |= {"network": True, "NAT": False}
    raw["Wifi_Inet_Config"][WAN_INET]["NAT"] = False
    raw["Port"] |= {"p-w": {"name": "patch-w2h"}, "p-h": {"name": "patch-h2w"}}
    raw["Wifi_VIF_Config"][VIF] |= {"group_rekey": 86400, "ft_psk": 0}
    assert asyncio.run(pod.bridge_mode()) == [] and pod.session.sent == []
    assert asyncio.run(pod.auth_reload()) == ["wl1.2"]


def test_an_older_profile_still_gives_the_home_aps_the_clouds_rekey_and_ft(tmp_path):
    # an agent binary deployed alone reads the image's profile, without group_rekey or ft_psk
    pod, _ = backend(tmp_path)
    row = {k: v for k, v in pod.profile.fronthaul_vif.items() if k not in ("group_rekey", "ft_psk")}
    pod.profile = dataclasses.replace(pod.profile, fronthaul_vif=row)
    hooked(pod.session.tables)
    assert "wl1.2 group_rekey, ft_psk" in asyncio.run(pod.bridge_mode())
    (ops,) = pod.session.sent
    assert {
        "op": "update",
        "table": "Wifi_VIF_Config",
        "where": [["_uuid", "==", ["uuid", VIF]]],
        "row": {"group_rekey": 86400, "ft_psk": 0},
    } in ops


@pytest.mark.parametrize("case", ["no wan row", "another's half pair", "the pod's plan"])
def test_bridge_mode_leaves_what_it_does_not_own(tmp_path, case):
    pod, _ = backend(tmp_path)
    raw = hooked(pod.session.tables)
    if case == "no wan row":
        del raw["Wifi_Inet_Config"][WAN_INET]
        assert asyncio.run(pod.bridge_mode()) == [] and pod.session.sent == []
        return
    if case == "another's half pair":
        raw["Port"]["p-w"] = {"name": "patch-w2h"}
        assert "patch pair" not in asyncio.run(pod.bridge_mode())
        return
    pod.profile = dataclasses.replace(pod.profile, radio_plan="pod")
    assert asyncio.run(pod.bridge_mode()) == [] and pod.session.sent == []


def test_bridge_ports_is_a_legacy_pods_only(tmp_path):
    vault = SecretStore(tmp_path / "secrets")
    profile = load_profile("opensync-lab-hwsim-6.6.1-v1")
    pod = PodBackend("pod-1", Session(tables()), vault, serial=SERIAL, profile=profile)
    bridged(pod.session.tables)
    assert asyncio.run(pod.bridge_ports()) == [] and pod.session.sent == []


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


def test_a_gtp_stations_write_is_one_per_start_and_credential(tmp_path):
    """spec 3.10 (19 October, the bench): a start EMOSA continues (the same start marker after an
    EMOSA restart) gets one more write when the station's credential changed; the same
    credential keeps the key, so it is not written again."""
    from emosa.agent.gtp_station import GtpStation
    from emosa.opensync.gtp_station import GtpStationIntent
    from emosa.store import Store

    pod = SeededPod()
    backend, intent, vault = gtp(tmp_path, pod)
    asyncio.run(backend.snapshot())

    def key(i):
        scope = GtpStation("pod-1", backend, Store(tmp_path / "gtp"), vault, i, run_id="t")
        return scope.start_write()[1]

    assert key(intent) == key(GtpStationIntent("pod-1", "wl1", "site-gtp", "gtp"))
    assert key(intent) != key(GtpStationIntent("pod-1", "wl1", "site-gtp-2", "gtp"))
    vault.write_simulated("gtp2", GTP_KEY + "x")
    assert key(intent) != key(GtpStationIntent("pod-1", "wl1", "site-gtp", "gtp2"))


def started(scope_for, source, configurations, attempt=0):
    """Each configuration in turn on one start of the pod, each by a scope EMOSA started anew
    (``attempt``: the GTP station's removals so far): [(a write wanted, its key)], each wanted
    write requested."""
    found = []
    for configuration in configurations:
        scope = scope_for(configuration)
        if attempt:
            scope.attempt = attempt
        done, key = scope.start_write()
        if done is None:
            scope.engine.request(scope.intent, source=source, key=key, run_id="t")
        found.append((done is None, key))
        scope.store.close()
    return found


def test_a_plan_or_credential_and_its_revert_on_one_start_are_three_writes(tmp_path):
    """spec 3.10 (19 October, a rollback on the bench): the radio plan's and the GTP station's
    configurations A, then B, then A again on one start are three writes, each numbered past the
    start's latest (A's second write not taken for its first); A again is none. The GTP station's
    removal is one more write, and EMOSA started anew (its removals counted from 0) not."""
    from emosa.agent import gtp_station
    from emosa.agent import radio_plan as plan_scope
    from emosa.opensync.gtp_station import GtpStationIntent
    from emosa.opensync.radio_plan import RadioPlanBackend, RadioPlanIntent
    from emosa.store import Store

    def numbered(found):
        (a, ka), (b, kb), (again, kagain), (unchanged, kunchanged) = found
        assert (a, b, again, unchanged) == (True, True, True, False)
        assert "#" not in ka and kb.endswith("#1") and kagain == f"{ka}#2" and kunchanged is None
        assert kb.partition("#")[0] != ka

    pod = OperatorPod()
    vault = SecretStore(tmp_path / "secrets")
    plans = {
        "A": RadioPlanIntent("pod-1", ("wl1.1", "wl1.2"), "2.4G", 6, "HT20"),
        "B": RadioPlanIntent("pod-1", ("wl1.1", "wl1.2"), "2.4G", 11, "HT20"),
    }

    def plan(name):
        backend = RadioPlanBackend("pod-1", pod, vault, serial=SERIAL, intent=plans[name])
        asyncio.run(backend.snapshot())
        return plan_scope.RadioPlan("pod-1", backend, Store(tmp_path / "plan"), vault, run_id="t")

    numbered(started(plan, plan_scope.SOURCE, "ABAA"))

    seeded = SeededPod()
    backend, intent, vault = gtp(tmp_path, seeded)
    asyncio.run(backend.snapshot())
    vault.write_simulated("gtp2", GTP_KEY + "x")
    credentials = {"A": intent, "B": GtpStationIntent("pod-1", "wl1", "site-gtp", "gtp2")}

    def station(name):
        store = Store(tmp_path / "gtp")
        return gtp_station.GtpStation("pod-1", backend, store, vault, credentials[name], run_id="t")

    first = started(station, gtp_station.SOURCE, "ABAA")
    numbered(first)
    ((removed, key),) = started(station, gtp_station.SOURCE, "A", attempt=1)
    assert removed and key == f"{first[0][1]}:1#3"
    # EMOSA anew, its removals from 0
    assert started(station, gtp_station.SOURCE, "A") == [(False, None)]


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


def held_by_gtp(pod, channel=11):
    """The GTP station on wl1 associated (its State with a parent), the radio on its channel."""
    pod.tables["Wifi_VIF_State"][GTP_STATE] = {
        "if_name": "wl1",
        "mode": "sta",
        "enabled": True,
        "mac": "02:00:00:00:41:09",
        "parent": "92:00:00:00:24:01",
    }
    pod.tables.setdefault("Wifi_Radio_State", {})[RADIO_STATE] = {
        "if_name": "wl1",
        "radio_config": ["uuid", RADIO],
        "vif_states": ["set", [["uuid", GTP_STATE]]],
        "freq_band": "2.4G",
        "channel": channel,
        "mac": "02:00:00:00:41:00",
        "enabled": True,
    }


def test_the_radio_plan_leaves_a_radio_its_station_holds_to_the_station(tmp_path):
    """spec 3.10 (the bench, 20 October): the GTP station holds wl1 on 11, where the gateway
    moved it; the plan writes no channel (the pod's OpenSync would pull the radio back to it)."""
    pod = OperatorPod()
    held_by_gtp(pod)
    backend, intent, _ = radio_plan(pod, tmp_path)
    config = asyncio.run(backend.snapshot()).config
    assert config["wired"] is True and config["station_channel"] == 11
    assert config["radio_planned"] is True and config["pending"] is True  # its other changes
    attempt = {"transaction_id": "t1", "session_generation": 1}
    assert asyncio.run(backend.submit(intent, attempt)).status == "committed"
    (ops,) = pod.sent
    assert not [op for op in ops if op["table"] == "Wifi_Radio_Config"]


@pytest.mark.parametrize("wired", [True, False])
def test_a_followed_stations_channel_is_the_radio_written_alone(tmp_path, wired):
    """spec 3.10: the plan following the station (follows): the radio's channel and width, that
    write alone (the plan's own was written once on the start), on whatever uplink."""
    pod = OperatorPod()
    held_by_gtp(pod)
    if not wired:
        pod.tables["Connection_Manager_Uplink"][UPLINK] = {
            "if_name": "g-wl1",
            "if_type": "gre",
            "is_used": True,
        }
    backend, intent, _ = radio_plan(pod, tmp_path)
    follow = dataclasses.replace(intent, channel=11, ht_mode="HT20", follows=True)
    assert follow.record()["follows"] is True and "follows" not in intent.record()
    backend.intent = follow
    config = asyncio.run(backend.snapshot()).config
    assert config["pending"] is True and config["radio_planned"] is False
    attempt = {"transaction_id": "t1", "session_generation": 1}
    assert asyncio.run(backend.submit(follow, attempt)).status == "committed"
    (ops,) = pod.sent
    updates = [(op["table"], op["row"]) for op in ops if op["op"] == "update"]
    assert updates == [("Wifi_Radio_Config", {"channel": 11, "ht_mode": "HT20"})]
    pod.tables["Wifi_Radio_Config"][RADIO].update({"channel": 11, "ht_mode": "HT20"})
    config = asyncio.run(backend.snapshot()).config
    assert config["pending"] is False and config["radio_planned"] is True


def test_the_plan_follows_its_station_once_per_hold_down_with_the_latest_channel(tmp_path):
    """spec 3.10: the plan takes the station's channel at once, then at most once per
    FOLLOW_HOLD, the latest channel when the station moved again within it, none when it came
    back to the Config's; the followed channel kept while no station holds the radio (a gateway's
    outage); a new start of the pod's OpenSync has the profile's plan again."""
    from emosa.agent import radio_plan as plan_scope
    from emosa.store import Store

    pod = OperatorPod()
    backend, intent, vault = radio_plan(pod, tmp_path)
    clock = [0.0]
    plan = plan_scope.RadioPlan(
        "pod-1", backend, Store(tmp_path / "plan"), vault, run_id="t", monotonic=lambda: clock[0]
    )
    backend.instance = "start-1"

    def at(t, station, channel):
        clock[0] = t
        plan._follow({"station_channel": station, "station_ht_mode": "HT20", "channel": channel})
        return (plan.intent.channel, plan.intent.follows, plan.follow_count)

    assert at(0, 11, 6) == (11, True, 1)  # at once
    assert backend.intent == plan.intent
    assert at(5, 1, 11) == (11, True, 1)  # within the hold-down: waits
    assert at(10, 13, 11) == (11, True, 1)
    assert at(20, 13, 11) == (13, True, 2)  # its end: the latest
    assert at(30, None, 13) == (13, True, 2)  # the station gone: kept
    assert at(100, 1, 13) == (1, True, 3)  # back on another channel
    assert at(105, 13, 1) == (1, True, 3)
    assert at(110, 1, 1) == (1, True, 3)  # back on the Config's: nothing waits
    assert at(125, 1, 1) == (1, True, 3)
    backend.instance = "start-2"  # a new start: its seed's radio
    assert at(126, None, 1) == (intent.channel, False, 3)
    assert plan.intent == intent


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


SEED_STA, SEED_CRED, SEED_INET, RADIO0 = (
    f"00000000-0000-4000-8000-0000000006{n}" for n in ("11", "12", "13", "14")
)


def with_seed_station(pod):
    """The seed's onboarding station on wl0 (credential-list mode, the operator's credential, its
    Inet row, its radio's reference) beside the GTP station on wl1 (on the GTP's SSID)."""
    pod.tables["Wifi_VIF_Config"][GTP_STA]["ssid"] = "site-gtp"
    pod.tables["Wifi_VIF_Config"][SEED_STA] = {
        "if_name": "wl0",
        "mode": "sta",
        "enabled": True,
        "credential_configs": ["set", [["uuid", SEED_CRED]]],  # a credential list, no SSID
    }
    pod.tables["Wifi_Credential_Config"] = {
        SEED_CRED: {"ssid": "operator-backhaul", "onboard_type": "gre"}
    }
    pod.tables["Wifi_Inet_Config"][SEED_INET] = {"if_name": "wl0", "enabled": False}
    pod.tables["Wifi_Radio_Config"][RADIO0] = {
        "if_name": "wl0",
        "freq_band": "5G",
        "vif_configs": ["set", [["uuid", SEED_STA]]],
    }
    return pod


def plan_with_gtp(pod, tmp_path, gtp_ssid="site-gtp"):
    from dataclasses import replace

    backend, intent, vault = radio_plan(pod, tmp_path)
    backend.intent = replace(intent, gtp_ssid=gtp_ssid)
    return backend, backend.intent


def test_a_wired_converted_pods_seed_stations_are_deleted_and_its_gtp_station_kept(tmp_path):
    # the seed's onboarding stations associate to nothing on a converted site and fail the
    # vendor healthcheck (14 October): deleted with their Inet, credentials and radio reference
    pod = with_seed_station(OperatorPod())
    backend, intent = plan_with_gtp(pod, tmp_path)
    config = asyncio.run(backend.snapshot()).config
    assert config["foreign_stations"] == ["wl0"] and config["pending"] is True
    assert asyncio.run(backend.plan(intent))["stations_removed"] == ["wl0"]
    attempt = {"transaction_id": "t1", "session_generation": 1}
    assert asyncio.run(backend.submit(intent, attempt)).status == "committed"
    (ops,) = pod.sent
    where = [["_uuid", "==", ["uuid", SEED_STA]]]
    station = [o for o in ops if o.get("where") == where]
    assert [o["op"] for o in station] == ["wait", "delete"]
    assert station[0]["rows"] == [{"if_name": "wl0", "mode": "sta", "ssid": ["set", []]}]
    assert {
        "op": "delete",
        "table": "Wifi_Credential_Config",
        "where": [["_uuid", "==", ["uuid", SEED_CRED]]],
    } in ops
    assert {
        "op": "delete",
        "table": "Wifi_Inet_Config",
        "where": [["_uuid", "==", ["uuid", SEED_INET]]],
    } in ops
    mutate = next(o for o in ops if o["op"] == "mutate")
    assert mutate["where"] == [["_uuid", "==", ["uuid", RADIO0]]]
    assert mutate["mutations"] == [["vif_configs", "delete", ["set", [["uuid", SEED_STA]]]]]
    touched = {json.dumps(o.get("where")) for o in ops}
    assert json.dumps([["_uuid", "==", ["uuid", GTP_STA]]]) not in touched  # the GTP station kept
    assert json.dumps([["_uuid", "==", ["uuid", GTP_INET]]]) not in touched


def test_a_station_on_the_gtps_ssid_by_its_credential_is_kept(tmp_path):
    # a start-up hook's station in credential-list mode: its linked credential on the GTP's SSID
    pod = with_seed_station(OperatorPod())
    pod.tables["Wifi_Credential_Config"][SEED_CRED]["ssid"] = "site-gtp"
    backend, _ = plan_with_gtp(pod, tmp_path)
    assert asyncio.run(backend.snapshot()).config["foreign_stations"] == []


def test_stations_are_left_alone_unwired_or_without_a_gtp(tmp_path):
    pod = with_seed_station(OperatorPod())
    backend, _ = plan_with_gtp(pod, tmp_path, gtp_ssid=None)
    assert asyncio.run(backend.snapshot()).config["foreign_stations"] == []
    pod.tables["Connection_Manager_Uplink"][UPLINK] = {
        "if_name": "g-wl1",
        "if_type": "gre",
        "is_used": True,
    }
    backend, _ = plan_with_gtp(pod, tmp_path)
    assert asyncio.run(backend.snapshot()).config["foreign_stations"] == []


IDLE_5G = {"band": "5G", "channel": 36, "ht_mode": "HT20"}
RADIO0_STATE = "00000000-0000-4000-8000-000000000615"


def with_dark_radio(pod, state=None):
    """The seed's wl0 as a hooked boot leaves it, its operator's cloud never come: no channel in
    its Config, its APs wl0.1 and wl0.2 enabled (14 October: "wl0.2 is not beaconing"), and its
    State as wm shows it (``state``; None: no State row yet)."""
    pod.tables["Wifi_Radio_Config"][RADIO0] = {
        "if_name": "wl0",
        "freq_band": "5G",
        "enabled": True,
        "vif_configs": ["set", [["uuid", OP_AP1], ["uuid", OP_AP2]]],
    }
    if state is not None:
        pod.tables["Wifi_Radio_State"] = {
            RADIO0_STATE: {"if_name": "wl0", "freq_band": "5G", **state}
        }
    return pod


def plan_with_idle(pod, tmp_path, idle=(IDLE_5G,)):
    from dataclasses import replace

    backend, intent, vault = radio_plan(pod, tmp_path)
    backend.intent = replace(intent, idle_radios=idle)
    return backend, backend.intent, vault


def test_an_idle_radio_carrying_an_enabled_ap_is_turned_on_at_the_profiles_channel(tmp_path):
    # every enabled AP beacons: the 2.4.3 pods' vendor healthcheck reboots a pod one of whose
    # APs does not, and the 2.4 profile neither binds nor places wl0
    pod = with_dark_radio(OperatorPod(), state={"enabled": True})  # up, no channel
    backend, intent, vault = plan_with_idle(pod, tmp_path)
    config = asyncio.run(backend.snapshot()).config
    assert config["dark_radios"] == ["wl0"] and config["pending"] is True
    plan = asyncio.run(backend.plan(intent))
    assert plan["radios_on"] == ["wl0"] and "Wifi_Radio_Config.enabled" in plan["fields"]
    attempt = {"transaction_id": "t1", "session_generation": 1}
    assert asyncio.run(backend.submit(intent, attempt)).status == "committed"
    (ops,) = pod.sent
    where = [["_uuid", "==", ["uuid", RADIO0]]]
    radio0 = [o for o in ops if o.get("where") == where]
    assert [o["op"] for o in radio0] == ["wait", "update"]
    assert radio0[0]["rows"] == [
        {"if_name": "wl0", "enabled": True, "channel": ["set", []], "ht_mode": ["set", []]}
    ]
    assert radio0[1]["row"] == {"enabled": True, "channel": 36, "ht_mode": "HT20"}
    assert intent.record()["idle_radios"] == [IDLE_5G]
    # applied once wm shows the radio on its channel, the rest of the plan applied too
    key = vault.generated("emosa-unclaimed")
    for uuid in (OP_AP1, OP_AP2):
        pod.tables["Wifi_VIF_Config"][uuid].update(neutral(key))
    pod.tables["Wifi_Inet_Config"][OP_INET]["enabled"] = False
    pod.tables["Wifi_Radio_Config"][RADIO].update({"channel": 6, "ht_mode": "HT20"})
    pod.tables["Wifi_Radio_Config"][RADIO0].update(radio0[1]["row"])
    assert asyncio.run(backend.snapshot()).config["dark_radios"] == ["wl0"]  # not yet in State
    assert not all(
        asyncio.run(backend.snapshot()).config.get(k) == v for k, v in intent.target(vault).items()
    )
    pod.tables["Wifi_Radio_State"][RADIO0_STATE]["channel"] = 36
    config = asyncio.run(backend.snapshot()).config
    assert config["dark_radios"] == [] and config["pending"] is False
    assert all(config.get(k) == v for k, v in intent.target(vault).items())


def test_an_idle_radio_is_left_alone_beaconing_or_with_no_enabled_ap(tmp_path):
    # wm shows it on a channel, its Config with none, as the recorded seed: never moved
    pod = with_dark_radio(OperatorPod(), state={"enabled": True, "channel": 108})
    backend, _, _ = plan_with_idle(pod, tmp_path)
    assert asyncio.run(backend.snapshot()).config["dark_radios"] == []
    # no State row yet: dark
    backend, _, _ = plan_with_idle(with_dark_radio(OperatorPod()), tmp_path)
    assert asyncio.run(backend.snapshot()).config["dark_radios"] == ["wl0"]
    # off, but its APs disabled
    pod = with_dark_radio(OperatorPod(), state={"enabled": False, "channel": 36})
    for uuid in (OP_AP1, OP_AP2):
        pod.tables["Wifi_VIF_Config"][uuid]["enabled"] = False
    backend, _, _ = plan_with_idle(pod, tmp_path)
    assert asyncio.run(backend.snapshot()).config["dark_radios"] == []
    # a profile with no idle radio
    backend, _, _ = plan_with_idle(with_dark_radio(OperatorPod()), tmp_path, idle=())
    assert asyncio.run(backend.snapshot()).config["dark_radios"] == []


def test_an_idle_radio_is_one_the_profile_neither_binds_nor_places(tmp_path):
    from importlib.resources import files

    from emosa.opensync.radio_plan import RadioPlanIntent

    assert load_profile("bcm-pod-2.4.3-gre-v1").idle_radios == (IDLE_5G,)
    five = json.loads(files("emosa").joinpath("profiles/bcm-pod-2.4.3-gre-5g-v1.json").read_text())
    two = json.loads(files("emosa").joinpath("profiles/bcm-pod-2.4.3-gre-v1.json").read_text())
    for name, data in (
        ("placed", {**five, "idle_radios": [IDLE_5G]}),
        ("bound", {**two, "idle_radios": [{**IDLE_5G, "band": "2.4G"}]}),
        ("twice", {**two, "idle_radios": [IDLE_5G, IDLE_5G]}),
        ("unmapped", {**two, "idle_radios": [{**IDLE_5G, "channel": 100}]}),
        ("no plan", {**two, "radio_plan": "pod"}),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(data))
        with pytest.raises(EmosaError) as refused:
            load_profile(str(path))
        assert refused.value.code == Reason.INVALID_INPUT, name
    for idle in (({**IDLE_5G, "band": "2.4G"},), (IDLE_5G, IDLE_5G), ({"band": "5G"},), ("5G",)):
        with pytest.raises(EmosaError):
            RadioPlanIntent("pod-1", ("wl1.2",), "2.4G", 6, "HT20", idle_radios=idle).validate()


def test_the_agent_monitors_every_column_the_radio_plan_compares():
    # the session sees only the columns monitored: the neutral AP's hidden flag, bridge and key,
    # the radio's channel and width, the Inet rows and cm's uplink (the box found the hidden flag
    # missing: the plan was written and never seen applied), the cloud's and statistics' columns
    from emosa.agent.pod import MONITOR

    for table, columns in {
        "Wifi_VIF_Config": (
            "if_name",
            "mode",
            "enabled",
            "ssid",
            "ssid_broadcast",
            "bridge",
            "security",
            "credential_configs",
        ),
        "Wifi_Radio_Config": ("channel", "ht_mode", "enabled", "vif_configs", "freq_band"),
        "Wifi_Radio_State": ("if_name", "enabled", "channel"),
        "Wifi_Inet_Config": ("if_name", "enabled"),
        "Connection_Manager_Uplink": ("if_name", "if_type", "is_used"),
        "AWLAN_Node": ("mqtt_settings", "mqtt_headers", "mqtt_topics"),
        "Wifi_Stats_Config": ("stats_type", "radio_type"),
        "Wifi_Credential_Config": ("ssid",),
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


RADIO_5G, VIF_5G = "00000000-0000-4000-8000-000000000151", "00000000-0000-4000-8000-000000000152"


def backend_5g(tmp_path):
    """A legacy pod on the 5 GHz profile: wl1 with wl1.2, and wl0 with its home AP wl0.2; no
    station row and no Inet row on either radio's own netdev (a hooked boot's, before EMOSA's
    station)."""
    vault = SecretStore(tmp_path / "secrets")
    raw = tables()
    raw["Wifi_Radio_Config"][RADIO_5G] = {
        "if_name": "wl0",
        "freq_band": "5G",
        "enabled": True,
        "vif_configs": ["set", [["uuid", VIF_5G]]],
    }
    raw["Wifi_VIF_Config"][VIF_5G] = {
        "if_name": "wl0.2",
        "mode": "ap",
        "ssid": "operator-home",
        "enabled": True,
        "security": SECURITY,
    }
    profile = load_profile("bcm-pod-2.4.3-gre-5g-v1")
    return PodBackend("pod-1", Session(raw), vault, serial=SERIAL, profile=profile, multi_bss=True)


def test_the_5ghz_home_aps_radio_gets_its_own_netdev_raised(tmp_path):
    # spec 3.10 (16 October), on by default: a tap Inet row for wl0 (wl0.2's radio), nm sets it up;
    # wl1, the profile's station netdev, never gets one, also while its station has no row
    pod = backend_5g(tmp_path)
    assert pod.phy_netdev_up is True
    assert asyncio.run(pod.phy_netdev()) == ["wl0 raised"]
    (ops,) = pod.session.sent
    assert ops[0]["op"] == "wait" and ops[0]["where"] == [["if_name", "==", "wl0"]]
    assert ops[1]["row"] == {"if_name": "wl0", **PHY_UP} and PHY_UP["if_type"] == "tap"
    assert pod.phy_ports == {"wl0": None, "wl1": None} and pod.phy_writes == 1


def test_the_station_netdev_is_never_raised(tmp_path):
    # spec 3.10: wl1 carries the home AP wl1.2 and no station row (before EMOSA writes its station
    # at a hooked boot, or after the station's guard removed it): a tap row there would be kept by
    # the station's writer, which would bring the station back with no address, so none
    pod, _ = backend(tmp_path)
    assert asyncio.run(pod.phy_netdev()) == [] and pod.session.sent == []
    assert pod.phy_ports == {"wl1": None} and pod.phy_writes == 0


@pytest.mark.parametrize(
    "row",
    [
        # an AP EMOSA did not make on the radio (a cloud-made pod's, whose onboarding raised it)
        {"if_name": "wl0.1", "mode": "ap", "enabled": True},
        # a station on the radio's own netdev (wm raises the netdev with it)
        {"if_name": "wl0", "mode": "sta", "enabled": True},
    ],
)
def test_a_radio_with_another_vif_gets_no_netdev_row(tmp_path, row):
    pod = backend_5g(tmp_path)
    other = "00000000-0000-4000-8000-000000000150"
    pod.session.tables["Wifi_VIF_Config"][other] = row
    radio = pod.session.tables["Wifi_Radio_Config"][RADIO_5G]
    radio["vif_configs"] = ["set", [*radio["vif_configs"][1], ["uuid", other]]]
    assert asyncio.run(pod.phy_netdev()) == [] and pod.session.sent == []
    assert pod.phy_ports == {"wl0": None, "wl1": None} and pod.phy_writes == 0


def test_a_radio_with_an_inet_row_of_its_own_gets_no_netdev_row(tmp_path):
    pod = backend_5g(tmp_path)
    own = {"if_name": "wl0", "if_type": "vif", "enabled": True, "network": True}
    pod.session.tables["Wifi_Inet_Config"] = {"00000000-0000-4000-8000-000000000140": own}
    assert asyncio.run(pod.phy_netdev()) == [] and pod.session.sent == []


def test_the_netdev_rows_off_removes_only_the_tap_rows_on_radios(tmp_path):
    # spec 3.10: the fleet's phy_netdev_up false: EMOSA's row on wl1 goes, a tap that is no radio's
    # stays
    pod, _ = backend(tmp_path)
    pod.phy_netdev_up = False
    ours, cloud = "00000000-0000-4000-8000-000000000141", "00000000-0000-4000-8000-000000000142"
    pod.session.tables["Wifi_Inet_Config"] = {
        ours: {"if_name": "wl1", "if_type": "tap", "enabled": True, "network": True},
        cloud: {"if_name": "br-home.dhcp", "if_type": "tap", "enabled": True, "network": True},
    }
    pod.session.tables["Wifi_Master_State"] = {
        "00000000-0000-4000-8000-000000000143": {"if_name": "wl1", "port_state": "active"}
    }
    assert asyncio.run(pod.phy_netdev()) == ["wl1 row removed"]
    (ops,) = pod.session.sent
    assert ops == [
        {"op": "delete", "table": "Wifi_Inet_Config", "where": [["_uuid", "==", ["uuid", ours]]]}
    ]
    assert pod.phy_ports == {"wl1": "active"}
