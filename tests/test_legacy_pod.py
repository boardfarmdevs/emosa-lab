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
