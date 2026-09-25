"""Data plane option 1 performed by the agent: the uplink switch as an operation."""

import asyncio
import copy
import json

import pytest

from emosa.agent.pod import uplink_bssid
from emosa.agent.uplink import DEADLINE, UplinkSwitch, m2_backhaul
from emosa.clock import ManualClock
from emosa.errors import EmosaError, Reason
from emosa.model import State
from emosa.opensync.easymesh_view import backhaul, device_view, topology
from emosa.opensync.schema import Schema, reference_path
from emosa.opensync.uplink import UplinkBackend, UplinkIntent, uplink_state
from emosa.secrets import SecretStore
from emosa.store import Store
from emosa.wire.autoconfiguration import PeerBinding
from emosa.wire.reports import _topology

pytestmark = pytest.mark.unit

SERIAL, STATION = "MVXPOD0000000001", "bhaul-sta-50"
RADIO, RADIO_STATE, VIF, VIF_STATE, BOOT_CRED, UPLINK = (
    f"00000000-0000-4000-8000-00000000000{n}" for n in range(1, 7)
)
PARENT = "02:00:00:00:19:01"
NODE = "00000000-0000-4000-8000-0000000000b1"


def bootstrap(start="00000000-0000-4000-8000-0000000000a1"):
    """A pod as OpenSync's bootstrap leaves it: bhaul-sta-50 on the GRE backhaul."""
    security = ["map", [["encryption", "WPA-PSK"], ["key", "bootstrap-psk"]]]
    # the template's AWLAN_Node keeps its UUID; the start scripts create the radios anew
    return {
        "AWLAN_Node": {NODE: {"serial_number": SERIAL, "model": "HWSIM_POD"}},
        "Wifi_Radio_Config": {start: {"if_name": "phy5", "freq_band": "5G", "enabled": True}},
        "Wifi_Radio_State": {
            RADIO_STATE: {
                "if_name": "phy5",
                "freq_band": "5G",
                "channel": 36,
                "mac": "02:00:00:00:15:00",
                "enabled": True,
                "vif_states": ["set", [["uuid", VIF_STATE]]],
            }
        },
        "Wifi_Credential_Config": {
            BOOT_CRED: {
                "ssid": "opensync-lab-bhaul",
                "security": security,
                "onboard_type": "gre",
                "enabled": True,
            }
        },
        "Wifi_VIF_Config": {
            VIF: {
                "if_name": STATION,
                "mode": "sta",
                "enabled": True,
                "ssid": "opensync-lab-bhaul",
                "security": security,
                "multi_ap": "none",
                "wds": False,
                "credential_configs": ["set", [["uuid", BOOT_CRED]]],
            }
        },
        "Wifi_VIF_State": {
            VIF_STATE: {
                "if_name": STATION,
                "mode": "sta",
                "enabled": True,
                "ssid": "opensync-lab-bhaul",
                "multi_ap": "none",
                "wds": False,
                "parent": "02:00:00:00:02:04",
                "mac": "02:00:00:00:15:01",
                "vif_config": ["uuid", VIF],
            }
        },
        "Connection_Manager_Uplink": {
            UPLINK: {"if_name": "g-bhaul-sta-50", "if_type": "gre", "is_used": True}
        },
    }


class Pod:
    """The pod's OVSDB, and what its managers do with a committed switch."""

    def __init__(self):
        self.schema = Schema(json.loads(reference_path().read_text()))
        self.tables, self.sent, self.generation, self.ready = bootstrap(), [], 1, True

    async def snapshot(self):
        return {
            "tables": copy.deepcopy(self.tables),
            "generation": self.generation,
            "revision": 1,
            "ready": self.ready,
            "schema": self.schema,
        }

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(operations)
        for op in operations:
            if op["op"] == "insert":
                self.tables[op["table"]]["00000000-0000-4000-8000-0000000000c1"] = op["row"]
            if op["op"] == "update" and op["table"] == "Wifi_VIF_Config":
                row = {**op["row"], "credential_configs": ["set", [["uuid", CRED]]]}
                self.tables["Wifi_VIF_Config"][VIF].update(row)
        return [{} if op["op"] in ("wait", "insert") else {"count": 1} for op in operations]

    def adopt(self, parent=PARENT):
        """owm joins the Multi-AP backhaul, cm uses the station: no GRE."""
        self.generation += 1  # the management session moved to the new uplink
        state = self.tables["Wifi_VIF_State"][VIF_STATE]
        state.update(
            ssid="emosa-mesh-bh", multi_ap="backhaul_sta", wds=True, parent=parent, bridge="br-home"
        )
        self.tables["Connection_Manager_Uplink"] = {
            UPLINK: {"if_name": STATION, "if_type": "vif", "is_used": True}
        }

    def restart(self, start):
        """OpenSync restarts: its database from the template and the bootstrap."""
        self.generation += 1
        self.tables = bootstrap(start)


CRED = "00000000-0000-4000-8000-0000000000c1"


def rig(tmp_path, credentials=("emosa-mesh-bh", "backhaul"), bssid=PARENT):
    vault = SecretStore(tmp_path / "secrets")
    vault.write_simulated("backhaul", "EmosaMesh2026!")
    pod, clock = Pod(), ManualClock()
    backend = UplinkBackend("pod-1", pod, vault, serial=SERIAL, station=STATION)
    store = Store(tmp_path / "uplink")
    switch = UplinkSwitch(
        "pod-1",
        backend,
        store,
        vault,
        lambda: credentials,
        bssid=bssid,
        run_id="pod-1",
        clock=clock,
    )
    return switch, pod, clock, store


def test_a_bootstrap_pod_is_on_gre_and_its_station_is_not_multi_ap(tmp_path):
    switch, pod, _, _ = rig(tmp_path)
    snap = asyncio.run(switch.backend.snapshot())
    assert snap.ready and snap.observed.fresh
    assert snap.config["uplink"] == "other" and snap.observed.values["uplink"] == "gre"
    assert switch.backend.facts["in_use"] == "g-bhaul-sta-50"


def test_the_switch_is_one_guarded_transaction_with_a_sole_multi_ap_credential(tmp_path):
    switch, pod, _, _ = rig(tmp_path)
    asyncio.run(switch.tick())
    (sent,) = pod.sent
    assert [op["op"] for op in sent] == ["wait", "wait", "insert", "update"]
    assert sent[0]["rows"] == [{"_uuid": ["uuid", NODE], "serial_number": SERIAL}]
    assert sent[1]["where"] == [["_uuid", "==", ["uuid", VIF]]]
    credential = sent[2]["row"]
    assert credential["onboard_type"] == "multi_ap" and credential["ssid"] == "emosa-mesh-bh"
    assert credential["bssid"] == PARENT  # pinned: never any BSS with that SSID
    assert credential["security"] == ["map", [["encryption", "WPA-PSK"], ["key", "EmosaMesh2026!"]]]
    station = sent[3]["row"]
    assert station["ssid"] == "" and station["security"] == ["map", []]  # credential-list mode
    assert station["credential_configs"] == ["set", [["named-uuid", "backhaul"]]]  # no gre beside
    assert switch.latest().state == State.CONFIG_COMMITTED


def test_applied_only_when_cm_uses_the_multi_ap_station(tmp_path):
    switch, pod, clock, _ = rig(tmp_path)
    asyncio.run(switch.tick())
    clock.advance(5)
    asyncio.run(switch.tick())
    assert switch.latest().state == State.CONFIG_COMMITTED  # joined nothing yet
    pod.adopt()
    asyncio.run(switch.tick())
    op = switch.latest()
    assert op.state == State.OBSERVED_APPLIED and op.application_evidence["fresh"]
    status = switch.status()
    assert status["option"] == 1 and status["in_use"] == STATION and status["parent"] == PARENT
    asyncio.run(switch.tick())
    assert len(pod.sent) == 1 and len(switch.store.operations()) == 1  # nothing more to do


def test_an_unconfirmed_switch_times_out_and_holds_the_pod_on_option_2(tmp_path):
    switch, pod, clock, store = rig(tmp_path)
    asyncio.run(switch.tick())
    pod.ready = False  # the pod lost its management path, and restarts to its bootstrap
    clock.advance(DEADLINE - 1)
    asyncio.run(switch.tick())
    assert switch.latest().state == State.CONFIG_COMMITTED
    pod.restart("00000000-0000-4000-8000-0000000000a2")
    pod.ready = True
    asyncio.run(switch.tick())
    assert switch.latest().state == State.CONFIG_COMMITTED  # a new start is not the switch
    clock.advance(2)
    asyncio.run(switch.tick())
    op = switch.latest()
    assert op.state == State.TIMED_OUT and op.reason == Reason.APPLY_TIMEOUT
    assert switch.held() and switch.status()["option"] == 2
    for start in ("a3", "a4"):  # never again on its own, whatever the pod does
        pod.restart(f"00000000-0000-4000-8000-0000000000{start}")
        asyncio.run(switch.tick())
    assert len(pod.sent) == 1 and switch.status()["waiting"] == "held on option 2"


def test_the_switch_is_applied_again_after_every_re_onboarding(tmp_path):
    switch, pod, clock, _ = rig(tmp_path)
    asyncio.run(switch.tick())
    pod.adopt()
    asyncio.run(switch.tick())
    first = switch.latest()
    assert first.state == State.OBSERVED_APPLIED
    pod.restart("00000000-0000-4000-8000-0000000000a2")  # e.g. a power cut: back on GRE
    asyncio.run(switch.tick())
    second = switch.latest()
    assert second.operation_id != first.operation_id and second.state == State.CONFIG_COMMITTED
    assert second.plan["instance"] != first.plan["instance"] and not switch.held()
    pod.adopt()
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED


def test_another_manager_changing_the_station_holds_the_pod(tmp_path):
    switch, pod, _, _ = rig(tmp_path)
    asyncio.run(switch.tick())
    pod.adopt()
    asyncio.run(switch.tick())
    pod.tables["Wifi_VIF_Config"][VIF]["ssid"] = "someone-else"  # same start, other writer
    asyncio.run(switch.tick())
    assert switch.held()["reason"].startswith("the station's configuration was changed")
    assert switch.latest().reason == Reason.OWNERSHIP_CONFLICT and len(pod.sent) == 1


def test_no_switch_without_credentials_or_a_working_uplink(tmp_path):
    switch, pod, _, _ = rig(tmp_path, credentials=None)
    asyncio.run(switch.tick())
    assert not pod.sent and switch.status()["waiting"] == "no EasyMesh backhaul credentials"
    switch, pod, _, _ = rig(tmp_path / "c", credentials=("emosa-mesh-bh", "absent"))
    asyncio.run(switch.tick())
    assert not pod.sent and switch.status()["waiting"].startswith("backhaul credentials unusable")
    switch, pod, _, _ = rig(tmp_path / "b")
    pod.tables["Connection_Manager_Uplink"][UPLINK]["is_used"] = False
    asyncio.run(switch.tick())
    assert not pod.sent and switch.status()["waiting"] == "no working uplink yet"
    with pytest.raises(EmosaError) as exc:
        asyncio.run(
            switch.backend.plan(UplinkIntent("pod-1", STATION, "x", "backhaul", bssid=PARENT))
        )
    assert exc.value.code == Reason.NOT_READY
    with pytest.raises(EmosaError) as exc:
        asyncio.run(
            switch.backend.plan(
                UplinkIntent("pod-1", "bhaul-sta-24", "x", "backhaul", bssid=PARENT)
            )
        )
    assert exc.value.code == Reason.UNSUPPORTED_OPERATION
    with pytest.raises(EmosaError) as exc:  # unpinned
        asyncio.run(switch.backend.plan(UplinkIntent("pod-1", STATION, "x", "backhaul")))
    assert exc.value.code == Reason.INVALID_INPUT


def test_credentials_come_from_the_applied_m2_backhaul_bss():
    class Op:
        def __init__(self, state, additional):
            self.state, self.intent = state, {"ssid": "emosa-mesh", "additional": additional}

    class Journal:
        def __init__(self, *ops):
            self.ops = list(ops)

        def operations(self):
            return self.ops

    backhaul_bss = {"role": "backhaul", "ssid": "emosa-mesh-bh", "secret_ref": "wsc-ab-1"}
    fronthaul = {"role": "fronthaul", "ssid": "guest", "secret_ref": "wsc-ab-2"}
    assert m2_backhaul(Journal()) is None
    assert m2_backhaul(Journal(Op(State.OBSERVED_APPLIED, [fronthaul]))) is None
    applied = Op(State.OBSERVED_APPLIED, [fronthaul, backhaul_bss])
    assert m2_backhaul(Journal(applied, Op(State.REJECTED, []))) == ("emosa-mesh-bh", "wsc-ab-1")


def test_the_backhaul_is_reported_only_while_it_is_the_easymesh_uplink():
    pod = Pod()
    schema = pod.schema

    def decoded():
        return {t: {u: schema.row(t, r) for u, r in rows.items()} for t, rows in pod.tables.items()}

    assert uplink_state(decoded(), STATION)["kind"] == "gre"
    pod.adopt()
    rows = decoded()
    assert uplink_state(rows, STATION)["kind"] == "multi-ap"
    view = backhaul(device_view(rows), STATION)
    assert view.band == "5G" and view.channel == 36 and view.station.parent.hex(":") == PARENT
    agent, controller = bytes.fromhex("020000e00010"), bytes.fromhex("020000e00001")
    radio = device_view(rows).radios[0]
    facts = topology(
        agent_al=agent,
        controller_al=controller,
        radio=radio,
        channel=36,
        bsses=(),
        ages={},
        uplink=view,
    )
    (_, station) = facts.device.interfaces
    assert station.mac == view.station.mac and station.media_type == 0x0104
    assert station.media_specific == view.station.parent + bytes([0x40, 0, 36, 0])  # non-AP STA
    assert facts.bridges.tuples == ((agent, view.station.mac),)
    assert facts.backhaul_stations == ((radio.ruid, view.station.mac),)
    assert facts.neighbors1905[0].local_interface == agent  # 1905 frames still go via EMOSA
    binding = PeerBinding("em0", 1, agent, controller, (controller,))
    tlvs = _topology(facts, binding)  # the Topology Response carries it
    assert view.station.mac + b"\x01\x04\x0a" + view.station.parent in tlvs[0].value


def test_the_switch_waits_until_the_fronthaul_is_served_and_idle(tmp_path):
    switch, pod, _, _ = rig(tmp_path)
    settled = [False]
    switch.settled = lambda: settled[0]
    asyncio.run(switch.tick())
    assert not pod.sent and switch.status()["waiting"] == "fronthaul not settled"
    settled[0] = True
    asyncio.run(switch.tick())
    assert len(pod.sent) == 1


def test_a_switch_times_out_even_when_the_pod_cannot_be_read_after_an_agent_restart(tmp_path):
    switch, pod, clock, store = rig(tmp_path)
    asyncio.run(switch.tick())
    assert switch.latest().state == State.CONFIG_COMMITTED
    # The agent restarts (a new process with the same journal) and the pod has
    # not come back: its database cannot be read at all.
    store.close()

    async def gone():
        raise ConnectionError("no pod")

    pod.snapshot = gone
    vault = switch.engine.vault
    backend = UplinkBackend("pod-1", pod, vault, serial=SERIAL, station=STATION)
    again = UplinkSwitch(
        "pod-1",
        backend,
        Store(tmp_path / "uplink"),
        vault,
        lambda: ("emosa-mesh-bh", "backhaul"),
        bssid=PARENT,
        run_id="pod-1",
        clock=clock,
    )
    asyncio.run(again.tick())
    assert again.latest().state == State.CONFIG_COMMITTED  # the deadline has not passed
    clock.advance(DEADLINE + 1)
    asyncio.run(again.tick())
    assert again.latest().state == State.TIMED_OUT and again.held()


OWN_BACKHAUL_BSS = "72:00:00:00:00:00"


def with_own_backhaul_bss(pod):
    """The pod runs the controller's backhaul BSS itself (multi-BSS M2 set): b-ap-24."""
    pod.tables["Wifi_VIF_State"]["00000000-0000-4000-8000-0000000000d1"] = {
        "if_name": "b-ap-24",
        "mode": "ap",
        "enabled": True,
        "ssid": "emosa-mesh-bh",
        "multi_ap": "backhaul_bss",
        "mac": OWN_BACKHAUL_BSS,
    }


def test_a_switch_to_one_of_the_pods_own_bsses_is_refused_before_any_write(tmp_path):
    # Live, the unpinned station joined its own b-ap-24: br-home looped and the
    # host ran out of memory within seconds (data-plane.md §5.6).
    for n, own in enumerate((OWN_BACKHAUL_BSS, "02:00:00:00:15:01")):
        switch, pod, _, _ = rig(tmp_path / str(n), bssid=own)
        with_own_backhaul_bss(pod)
        asyncio.run(switch.tick())
        op = switch.latest()
        assert not pod.sent and op.state == State.REJECTED and op.reason == Reason.INVALID_INPUT
        asyncio.run(switch.tick())
        assert switch.held() and not pod.sent  # a configuration error: never retried on its own


def test_applied_only_on_the_pinned_upstream_bss(tmp_path):
    switch, pod, clock, _ = rig(tmp_path)
    with_own_backhaul_bss(pod)
    asyncio.run(switch.tick())
    pod.adopt(parent="02:00:00:00:77:01")  # some other BSS with the backhaul SSID
    asyncio.run(switch.tick())
    assert switch.latest().state == State.CONFIG_COMMITTED
    pod.adopt()
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED
    assert switch.status()["bssid"] == switch.status()["parent"] == PARENT


def test_a_multi_ap_uplink_configuration_needs_the_upstream_bssid():
    assert uplink_bssid({"mode": "multi-ap", "bssid": "02:00:00:00:09:0A"}) == "02:00:00:00:09:0a"
    for config in ({"mode": "multi-ap"}, {"mode": "multi-ap", "bssid": "not-a-mac"}):
        with pytest.raises(EmosaError) as exc:
            uplink_bssid(config)
        assert exc.value.code == Reason.INVALID_INPUT
