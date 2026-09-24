"""The OVSDB -> EasyMesh translation, on a real OpenSync 6.6.1.0 pod's rows."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from emosa.agent.pod import MONITOR, PodReportSource
from emosa.errors import EmosaError
from emosa.opensync.easymesh_view import device_view, inventory, radio_capabilities, topology
from emosa.opensync.pod_profile import PodBackend
from emosa.opensync.schema import Schema, reference_path
from emosa.secrets import SecretStore
from emosa.wire.autoconfiguration import PeerBinding
from test_pod_profile import Session

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures/opensync/pod-6.6.1-hwsim-tables.json"
SERIAL = "MVXPOD023F87E628DD"
RUID_24, RUID_5 = bytes.fromhex("020000000100"), bytes.fromhex("020000000500")
BSSID = bytes.fromhex("820000000100")
STATIONS = (bytes.fromhex("020000000a00"), bytes.fromhex("020000001000"))
AGENT, CONTROLLER = bytes.fromhex("0272f97f0785"), bytes.fromhex("020000e00001")


def raw_tables():
    return json.loads(FIXTURE.read_text())["tables"]


def decode(raw):
    schema = Schema(json.loads(reference_path().read_text()))
    return {t: {u: schema.row(t, r) for u, r in rows.items()} for t, rows in raw.items()}


def vif_state(raw, if_name):
    return next(r for r in raw["Wifi_VIF_State"].values() if r["if_name"] == if_name)


def test_a_real_pod_is_one_device_two_radios_one_bss_and_its_uplink():
    view = device_view(decode(raw_tables()))
    assert (view.serial, view.model) == (SERIAL, "HWSIM_POD")
    assert view.firmware.startswith("6.6.1.0")
    radio = view.radio(RUID_24)
    assert (radio.band, radio.channel, radio.tx_power, radio.enabled) == ("2.4G", 6, 30, True)
    (bss,) = radio.bsses
    assert (bss.if_name, bss.bssid, bss.ssid, bss.role) == (
        "home-ap-24",
        BSSID,
        "emosa-mesh",
        "fronthaul",
    )
    assert bss.stations == STATIONS and bss.report_flags == 0x40
    five = view.radio(RUID_5)
    assert five.bsses == () and [u.if_name for u in five.uplinks] == ["bhaul-sta-50"]
    assert five.uplinks[0].ssid == "opensync-lab-bhaul"


@pytest.mark.parametrize(
    "multi_ap,flags", [("backhaul_bss", 0x80), ("none", 0x40), ("fronthaul_bss", 0x40)]
)
def test_the_bss_role_comes_from_the_pods_own_multi_ap_state(multi_ap, flags):
    raw = raw_tables()
    vif_state(raw, "home-ap-24")["multi_ap"] = multi_ap
    (bss,) = device_view(decode(raw)).radio(RUID_24).bsses
    assert bss.report_flags == flags


@pytest.mark.parametrize("change", ["disabled", "not-on-radio", "no-mac", "inactive-clients"])
def test_only_what_the_pods_state_shows_is_represented(change):
    raw = raw_tables()
    vif = vif_state(raw, "home-ap-24")
    if change == "disabled":
        vif["enabled"] = False
    elif change == "no-mac":
        vif["mac"] = ["set", []]
    elif change == "not-on-radio":
        for radio in raw["Wifi_Radio_State"].values():
            if radio["freq_band"] == "2.4G":
                radio["vif_states"] = ["set", []]
    else:
        for client in raw["Wifi_Associated_Clients"].values():
            client["state"] = "idle"
    radio = device_view(decode(raw)).radio(RUID_24)
    if change == "inactive-clients":
        assert radio.bsses[0].stations == ()
    else:
        assert radio.bsses == ()


def test_no_single_identity_is_not_a_device():
    raw = raw_tables()
    raw["AWLAN_Node"] = {}
    with pytest.raises(EmosaError):
        device_view(decode(raw))


def test_the_view_as_easymesh_payloads():
    view = device_view(decode(raw_tables()))
    radio = view.radio(RUID_24)
    caps = radio_capabilities(radio, channel=6, max_bss=1, max_eirp=30)
    (opclass,) = caps.radios[0].basic.operating_classes
    assert caps.radios[0].basic.ruid == RUID_24 and caps.radios[0].basic.max_bss == 1
    assert opclass.operating_class == 81 and 6 not in opclass.non_operable_channels
    with pytest.raises(EmosaError):
        radio_capabilities(view.radio(RUID_5), channel=44, max_bss=1, max_eirp=23)
    assert inventory(view, radio).serial_number == SERIAL.encode()
    facts = topology(
        agent_al=AGENT,
        controller_al=CONTROLLER,
        radio=radio,
        channel=6,
        bsses=radio.bsses,
        ages={STATIONS[0]: 12.9, STATIONS[1]: 99999},
    )
    assert facts.device.al_mac == AGENT
    assert [i.mac for i in facts.device.interfaces] == [AGENT, BSSID]
    (configured,) = facts.configuration.radios
    assert [(b.bssid, b.flags, b.ssid) for b in configured.bsses] == [(BSSID, 0x40, b"emosa-mesh")]
    (clients,) = facts.clients.bsses
    assert [(c.mac, c.association_seconds) for c in clients.clients] == [
        (STATIONS[0], 12),
        (STATIONS[1], 65535),
    ]


def test_the_agent_reports_the_real_pod_through_the_view(tmp_path):
    vault = SecretStore(tmp_path / "secrets")
    backend = PodBackend("pod-1", Session(copy.deepcopy(raw_tables())), vault, serial=SERIAL)
    binding = PeerBinding("em1", 1, AGENT, CONTROLLER, (CONTROLLER,))
    report = PodReportSource(backend, binding, "pod-1")
    assert asyncio.run(report.refresh())
    assert report.facts["bsses"] == [
        {"role": "fronthaul", "bssid": BSSID.hex(":"), "ssid": "emosa-mesh"}
    ]
    assert report.facts["stations"] == [m.hex(":") for m in STATIONS]
    snapshot = report.source.current()
    assert snapshot.capabilities.radios[0].basic.ruid == RUID_24
    assert [r.channel for r in snapshot.operating_radios] == [6]
    assert report.inventory.serial_number == SERIAL.encode()


def test_a_managed_backhaul_bss_is_reported_as_backhaul(tmp_path):
    # The role is read from State, so the agent must monitor it there.
    assert "multi_ap" in MONITOR["Wifi_VIF_State"]
    raw = raw_tables()
    config_uuid, state_uuid = (
        "00000000-0000-4000-8000-0000000000b1",
        "00000000-0000-4000-8000-0000000000b2",
    )
    home = next(r for r in raw["Wifi_VIF_Config"].values() if r["if_name"] == "home-ap-24")
    raw["Wifi_VIF_Config"][config_uuid] = {**home, "if_name": "b-ap-24", "multi_ap": "backhaul_bss"}
    raw["Wifi_VIF_State"][state_uuid] = {
        **vif_state(raw, "home-ap-24"),
        "if_name": "b-ap-24",
        "vif_config": ["uuid", config_uuid],
        "mac": "82:00:00:00:01:01",
        "ssid": "emosa-mesh-bh",
        "multi_ap": "backhaul_bss",
        "associated_clients": ["set", []],
    }
    for table, uuid, column in (
        ("Wifi_Radio_Config", config_uuid, "vif_configs"),
        ("Wifi_Radio_State", state_uuid, "vif_states"),
    ):
        radio = next(r for r in raw[table].values() if r["freq_band"] == "2.4G")
        present = radio[column][1] if radio[column][0] == "set" else [radio[column]]
        radio[column] = ["set", [*present, ["uuid", uuid]]]
    vault = SecretStore(tmp_path / "secrets")
    backend = PodBackend("pod-1", Session(raw), vault, serial=SERIAL, multi_bss=True)
    report = PodReportSource(
        backend, PeerBinding("em1", 1, AGENT, CONTROLLER, (CONTROLLER,)), "pod-1"
    )
    assert asyncio.run(report.refresh())
    assert [(b["role"], b["ssid"]) for b in report.facts["bsses"]] == [
        ("fronthaul", "emosa-mesh"),
        ("backhaul", "emosa-mesh-bh"),
    ]
    (configured,) = report.source.current().topology.configuration.radios
    assert [b.flags for b in configured.bsses] == [0x40, 0x80]
