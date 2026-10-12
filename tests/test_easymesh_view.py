# SPDX-License-Identifier: Apache-2.0
"""The OVSDB -> EasyMesh translation, on a real OpenSync 6.6.1.0 pod's rows."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from emosa.agent.pod import MONITOR, PodReportSource
from emosa.errors import EmosaError
from emosa.opensync.easymesh_view import (
    MEDIA,
    backhaul,
    bound_radios,
    device_view,
    inventory,
    radio_capabilities,
    radios_capabilities,
    topology,
)
from emosa.opensync.pod_profile import PodBackend
from emosa.opensync.schema import Schema, reference_path
from emosa.secrets import SecretStore
from emosa.wire.autoconfiguration import PeerBinding
from emosa.wire.reports import capability_tlvs
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


def test_a_gre_parents_ap_is_a_backhaul_bss():
    """Spec 8.6: the parent AP is not Multi-AP (multi_ap none), and still a backhaul BSS."""
    raw = raw_tables()
    vif_state(raw, "home-ap-24")["multi_ap"] = "none"
    (bss,) = device_view(decode(raw), parent_aps={"home-ap-24"}).radio(RUID_24).bsses
    assert bss.report_flags == 0x80


def test_a_3address_station_under_gre_is_an_honest_wireless_backhaul():
    """Spec 8.6: a GRE child's uplink is a 3-address station (multi_ap none) associated to the
    parent AP. The pod reports it as its 802.11 backhaul so the controller sees the true medium,
    but keeps multi_ap false: it is not claimed as a Multi-AP (4-address) bSTA."""
    raw = raw_tables()
    sta = vif_state(raw, "bhaul-sta-50")
    sta["parent"] = "72:00:00:00:6a:00"  # associated to the parent AP b-ap
    sta["multi_ap"] = "none"  # 3-address, not a Multi-AP backhaul STA
    device = device_view(decode(raw))
    bh = backhaul(device, "bhaul-sta-50")
    assert bh is not None
    assert (bh.band, bh.channel) == ("5G", 44)
    assert bh.station.parent == bytes.fromhex("720000006a00")
    assert bh.station.multi_ap is False  # honest: the medium is 802.11, the role is not Multi-AP
    # an unassociated station (no parent) is still not a backhaul
    sta["parent"] = ["set", []]
    assert backhaul(device_view(decode(raw)), "bhaul-sta-50") is None


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
    # the second radio of spec 9: a 5 GHz radio is now mapped (UNII-1, opclass 115)
    five = radio_capabilities(view.radio(RUID_5), channel=44, max_bss=1, max_eirp=23)
    (five_opclass,) = five.radios[0].basic.operating_classes
    assert five.radios[0].basic.ruid == RUID_5 and five_opclass.operating_class == 115
    assert 44 not in five_opclass.non_operable_channels
    assert set(five_opclass.non_operable_channels) == {36, 40, 48}
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


def test_bound_radios_adds_the_profiles_second_radio():
    """Spec 9: EMOSA binds the fronthaul plus each profile radio found on the device by band.
    A plain child's second radio has no EMOSA slot yet advertises one BSS (an AP Radio Basic
    Capabilities TLV may not advertise none: the report would not encode); a parent's b-ap-50 is
    that one."""
    view = device_view(decode(raw_tables()))
    fronthaul = view.radio(RUID_24)
    child = bound_radios(
        view.radios, fronthaul, 6, additional=({"band": "5G"},), slots=(), slot_bands=()
    )
    assert [(r.ruid, ch, mbss) for r, ch, mbss in child] == [(RUID_24, 6, 1), (RUID_5, 44, 1)]
    caps = radios_capabilities(tuple((r, ch, mbss, 20) for r, ch, mbss in child))
    assert [t.value[:6] for t in capability_tlvs(caps) if t.kind == 0x85] == [RUID_24, RUID_5]
    parent = bound_radios(
        view.radios,
        fronthaul,
        6,
        additional=({"band": "5G"},),
        slots=(("b-ap-50", "backhaul", 1), ("fh-24", "fronthaul", 6)),
        slot_bands=(("b-ap-50", "5G"),),
    )
    assert [(r.ruid, ch, mbss) for r, ch, mbss in parent] == [(RUID_24, 6, 2), (RUID_5, 44, 1)]
    # no additional radios: just the fronthaul
    only = bound_radios(view.radios, fronthaul, 6, additional=(), slots=(), slot_bands=())
    assert [r.ruid for r, _, _ in only] == [RUID_24]


def test_radios_capabilities_reports_each_bound_radio():
    """Spec 9 second radio: EMOSA reports every bound radio in the AP Capability Report, each
    operable only on its own channel (the fronthaul 2.4 GHz and the 5 GHz second radio)."""
    view = device_view(decode(raw_tables()))
    caps = radios_capabilities(
        (
            (view.radio(RUID_24), 6, 1, 30),
            (view.radio(RUID_5), 44, 1, 23),
        )
    )
    assert [r.basic.ruid for r in caps.radios] == [RUID_24, RUID_5]
    assert caps.radios[0].basic.operating_classes[0].operating_class == 81
    assert caps.radios[1].basic.operating_classes[0].operating_class == 115
    assert 44 not in caps.radios[1].basic.operating_classes[0].non_operable_channels


def test_topology_reports_a_second_radios_bss():
    """Spec 9: a further bound radio that carries an EMOSA BSS (a parent's b-ap-50 on 5 GHz) is its
    own Operational and Configured radio, so the controller models the BSS on that radio."""
    view = device_view(decode(raw_tables()))
    fronthaul, five = view.radio(RUID_24), view.radio(RUID_5)
    (bss,) = fronthaul.bsses
    facts = topology(
        agent_al=AGENT,
        controller_al=CONTROLLER,
        radio=fronthaul,
        channel=6,
        bsses=fronthaul.bsses,
        ages={STATIONS[0]: 1.0, STATIONS[1]: 1.0},
        extra_radios=((five, (bss,), 44),),
    )
    assert [r.ruid for r in facts.operational.radios] == [RUID_24, RUID_5]
    assert [r.ruid for r in facts.configuration.radios] == [RUID_24, RUID_5]
    assert [b.bssid for b in facts.configuration.radios[1].bsses] == [bss.bssid]


def test_topology_reports_every_bound_radio_and_a_second_radios_bss_on_its_band():
    """Spec 9: a child's second radio (its station only) is an Operational and Configured radio
    with no BSS, as the capability report lists it; a parent's BSS on the 5 GHz radio is a 5 GHz
    interface whose stations (a GRE child on b-ap-50) are associated clients."""
    view = device_view(decode(raw_tables()))
    fronthaul, five = view.radio(RUID_24), view.radio(RUID_5)
    (bss,) = fronthaul.bsses
    common = {"agent_al": AGENT, "controller_al": CONTROLLER, "radio": fronthaul, "channel": 6}
    ages = {STATIONS[0]: 1.0, STATIONS[1]: 1.0}
    child = topology(**common, bsses=fronthaul.bsses, ages=ages, extra_radios=((five, (), 36),))
    assert [(r.ruid, r.bsses) for r in child.operational.radios][1] == (RUID_5, ())
    assert [(r.ruid, r.bsses) for r in child.configuration.radios][1] == (RUID_5, ())
    parent = topology(**common, bsses=(), ages=ages, extra_radios=((five, (bss,), 36),))
    (interface,) = [i for i in parent.device.interfaces if i.mac == bss.bssid]
    assert interface.media_type == MEDIA["5G"] and interface.media_specific[8] == 36
    (clients,) = parent.clients.bsses
    assert clients.bssid == bss.bssid and {c.mac for c in clients.clients} == set(bss.stations)


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
