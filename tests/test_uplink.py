# SPDX-License-Identifier: Apache-2.0
"""Data plane option 1 performed by the agent: the uplink switch as an operation."""

import asyncio
import copy
import json

import pytest

from emosa.agent.pod import uplink_bssid
from emosa.agent.uplink import DEADLINE, HOLD_BACKOFF, UplinkSwitch, m2_backhaul
from emosa.clock import ManualClock
from emosa.errors import EmosaError, Reason
from emosa.model import State
from emosa.opensync.easymesh_view import backhaul, device_view, topology
from emosa.opensync.schema import Schema, reference_path
from emosa.opensync.uplink import BOOTSTRAP, MULTI_AP, UplinkBackend, UplinkIntent, uplink_state
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

    def adopt_gre(self, parent):
        """owm joins a GRE parent as a 3-address station (not Multi-AP); cm builds the gretap
        and uses it as the uplink (if_type gre), spec 8.6."""
        self.generation += 1
        state = self.tables["Wifi_VIF_State"][VIF_STATE]
        state.update(ssid="emosa-mesh-bh", multi_ap="none", wds=False, parent=parent, bridge="")
        self.tables["Connection_Manager_Uplink"] = {
            UPLINK: {"if_name": "g-" + STATION, "if_type": "gre", "is_used": True}
        }

    def revert(self):
        """cm, its uplink failed: the station back on its bootstrap credential, same start."""
        self.generation += 1
        fresh = bootstrap(next(iter(self.tables["Wifi_Radio_Config"])))
        for table in ("Wifi_VIF_Config", "Wifi_VIF_State", "Connection_Manager_Uplink"):
            self.tables[table] = fresh[table]

    def restart(self, start):
        """OpenSync restarts: its database from the template and the bootstrap."""
        self.generation += 1
        self.tables = bootstrap(start)


CRED = "00000000-0000-4000-8000-0000000000c1"


def rig(tmp_path, credentials=("emosa-mesh-bh", "backhaul"), bssid=PARENT, **options):
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
        **({"wall": clock.monotonic} | options),
    )
    return switch, pod, clock, store


def test_a_bootstrap_pod_is_on_gre_and_its_station_is_not_multi_ap(tmp_path):
    switch, pod, _, _ = rig(tmp_path)
    snap = asyncio.run(switch.backend.snapshot())
    assert snap.ready and snap.observed.fresh
    assert snap.config["uplink"] == "other" and snap.observed.values["uplink"] == "gre"
    assert switch.backend.facts["in_use"] == "g-bhaul-sta-50"


def test_the_bootstrap_upstream_is_recorded_once_and_persists(tmp_path):
    """Spec 8.6: the BSS the station is on before any EMOSA move is its bootstrap upstream,
    recorded on first observation and persisted so a steer back to it is a revert."""
    switch, pod, _, store = rig(tmp_path)
    assert switch.bootstrap is None
    asyncio.run(switch.tick())  # records from the pre-switch GRE bootstrap, before the switch
    assert switch.bootstrap == {"bssid": "02:00:00:00:02:04", "ssid": "opensync-lab-bhaul"}
    assert switch.status()["bootstrap"] == switch.bootstrap
    saved = json.loads((store.directory / "bootstrap.json").read_text())
    assert saved == {"bssid": "02:00:00:00:02:04", "ssid": "opensync-lab-bhaul"}
    # the pod adopts the configured upstream: the recorded bootstrap is not overwritten
    pod.adopt()
    asyncio.run(switch.tick())
    assert switch.bootstrap == {"bssid": "02:00:00:00:02:04", "ssid": "opensync-lab-bhaul"}


def test_a_recorded_bootstrap_is_loaded_on_restart(tmp_path):
    """The persisted bootstrap upstream is read back when the agent restarts (spec 8.6)."""
    (tmp_path / "uplink").mkdir(parents=True)
    (tmp_path / "uplink" / "bootstrap.json").write_text(
        json.dumps({"bssid": "02:00:00:00:02:04", "ssid": "opensync-lab-bhaul"})
    )
    switch, *_ = rig(tmp_path)
    assert switch.bootstrap == {"bssid": "02:00:00:00:02:04", "ssid": "opensync-lab-bhaul"}


def test_a_gre_move_is_three_address_and_disables_no_other_station(tmp_path):
    """Spec 8.6 ownership: a GRE move leaves the station's multi_ap/wds (3-address) and disables
    no other station; it snapshots the pod's vconf columns (ssid, security, credential_configs)
    for the revert to restore."""
    switch, pod, _, _ = rig(tmp_path, bssid=None, parents=lambda: {PARENT})
    asyncio.run(
        switch.tick()
    )  # observe the bootstrap first (records it); a pure GRE child writes nothing
    assert not pod.sent
    assert switch.steer(PARENT) is None  # steered onto the known GRE parent
    asyncio.run(switch.tick())
    (sent,) = pod.sent
    updates = [op for op in sent if op["op"] == "update" and op["table"] == "Wifi_VIF_Config"]
    assert len(updates) == 1  # only the station; no other backhaul station disabled
    row = updates[0]["row"]
    assert row["ssid"] == "" and row["credential_configs"] == ["set", [["named-uuid", "backhaul"]]]
    assert "multi_ap" not in row and "wds" not in row  # left as the pod set them (3-address)
    credential = next(op for op in sent if op["op"] == "insert")["row"]
    assert credential["onboard_type"] == "gre" and credential["bssid"] == PARENT
    assert switch.latest().commit_evidence["restore"] == {
        "ssid": "opensync-lab-bhaul",
        "security": ["map", [["encryption", "WPA-PSK"], ["key", "bootstrap-psk"]]],
        "credential_configs": ["set", [["uuid", BOOT_CRED]]],
    }


def test_a_steer_back_to_the_bootstrap_reverts_the_move(tmp_path):
    """Spec 8.6: a steer to the recorded bootstrap BSS is a revert - EMOSA restores the station's
    vconf snapshot and deletes the credential it inserted; the station lands on the bootstrap."""
    switch, pod, _, _ = rig(tmp_path, bssid=None, parents=lambda: {PARENT})
    asyncio.run(switch.tick())  # observe and record the bootstrap
    boot = dict(switch.bootstrap)
    assert boot["bssid"] == "02:00:00:00:02:04"
    # onto the parent
    assert switch.steer(PARENT) is None
    asyncio.run(switch.tick())
    pod.adopt_gre(PARENT)
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED
    # back to the bootstrap: a revert, not a pin
    assert switch.steer(boot["bssid"]) is None
    assert switch.mode == BOOTSTRAP
    asyncio.run(switch.tick())
    revert = pod.sent[-1]
    assert ("delete", "Wifi_Credential_Config") in [(o["op"], o.get("table")) for o in revert]
    update = next(o for o in revert if o["op"] == "update" and o["table"] == "Wifi_VIF_Config")
    assert update["row"]["ssid"] == "opensync-lab-bhaul"  # vconf restored; the move marker cleared
    assert update["row"]["credential_configs"] == ["set", [["uuid", BOOT_CRED]]]
    # cm takes the station back to its bootstrap BSS; the revert is confirmed by observation
    pod.revert()
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED
    assert switch.mode == MULTI_AP and switch.bssid is None  # back to no EMOSA pin
    assert switch.steering_outcome(boot["bssid"]) is True


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
    for start in ("a3", "a4"):  # not again before its backoff, whatever the pod does
        pod.restart(f"00000000-0000-4000-8000-0000000000{start}")
        asyncio.run(switch.tick())
    assert len(pod.sent) == 1 and switch.status()["waiting"] == "held on option 2"


def test_a_held_pod_switches_again_after_its_backoff_doubling_to_the_cap(tmp_path):
    # spec 8.3: a failed switch's hold is bounded; each retry its own switch, on the same start
    switch, pod, clock, store = rig(tmp_path, hold_backoff=600, hold_backoff_cap=1000)
    asyncio.run(switch.tick())
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())
    held = switch.held()
    assert held["attempt"] == 1 and held["retry_at"] - held["held_at"] == 600 and len(pod.sent) == 1
    clock.advance(599)
    asyncio.run(switch.tick())
    assert switch.held() and len(pod.sent) == 1 and switch.status()["waiting"] == "held on option 2"
    clock.advance(2)
    asyncio.run(switch.tick())  # the backoff over: the switch made again, same start
    assert not switch.held() and len(pod.sent) == 2
    assert switch.latest().state == State.CONFIG_COMMITTED
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())  # not confirmed again: held, the backoff doubled to its cap
    held = switch.held()
    assert held["attempt"] == 2 and held["retry_at"] - held["held_at"] == 1000
    clock.advance(1001)
    asyncio.run(switch.tick())
    assert len(pod.sent) == 3
    pod.adopt()
    asyncio.run(switch.tick())  # applied on its retry: the hold cleared, the backoff with it
    assert switch.latest().state == State.OBSERVED_APPLIED
    assert store.ownership("pod-1") is None and not switch.held()


def test_another_managers_change_holds_until_a_new_admission_not_a_backoff(tmp_path):
    switch, pod, clock, store = rig(tmp_path)
    asyncio.run(switch.tick())
    pod.adopt()
    asyncio.run(switch.tick())
    switch.hold(switch.latest(), "the station's configuration was changed by another manager")
    clock.advance(10 * HOLD_BACKOFF)
    asyncio.run(switch.tick())
    assert switch.held() and "retry_at" not in switch.held()


def test_a_hold_from_before_bounded_holds_gets_its_backoff(tmp_path):
    switch, pod, clock, store = rig(tmp_path)
    store.conflict(
        "pod-1", {"operation_id": None, "reason": "switch not confirmed within the deadline"}
    )
    asyncio.run(switch.tick())
    held = switch.held()
    assert held["attempt"] == 1 and held["retry_at"] == clock.monotonic() + HOLD_BACKOFF
    assert len(pod.sent) == 0
    clock.advance(HOLD_BACKOFF)
    asyncio.run(switch.tick())
    assert len(pod.sent) == 1 and not switch.held()


def test_only_an_operator_release_lets_a_held_pod_switch_again(tmp_path):
    # the release takes effect on the pod's next start: one switch per start and credential
    switch, pod, clock, store = rig(tmp_path)
    asyncio.run(switch.tick())
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())
    assert switch.held() and len(pod.sent) == 1
    store.release("pod-1")
    asyncio.run(switch.tick())
    assert not switch.held() and len(pod.sent) == 1
    pod.restart("00000000-0000-4000-8000-0000000000a2")
    asyncio.run(switch.tick())
    assert len(pod.sent) == 2 and switch.latest().state == State.CONFIG_COMMITTED
    pod.adopt()
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED


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
    # spec 9 (the rdk-1010 proof, 12 Oct): a fronthaul radio with no backhaul slot keeps no
    # backhaul BSS in its set; the 5 GHz radio's own set has it
    radio_b = {"role": "backhaul", "ssid": "emosa-5g-bh", "secret_ref": "wsc-ab-3"}
    no_backhaul = Journal(Op(State.OBSERVED_APPLIED, [fronthaul]))
    five = Journal(Op(State.OBSERVED_APPLIED, [radio_b]))
    assert m2_backhaul(no_backhaul, five) == ("emosa-5g-bh", "wsc-ab-3")
    # the fronthaul's set first when it has one
    assert m2_backhaul(Journal(applied), five) == ("emosa-mesh-bh", "wsc-ab-1")
    assert m2_backhaul(no_backhaul, Journal()) is None


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


# The controller's Backhaul Steering (emosa.wire.backhaul_steering): an extender's backhaul BSS.
NEXT = "02:00:00:00:29:01"


def on_easymesh(tmp_path, **options):
    switch, pod, clock, store = rig(tmp_path, **options)
    asyncio.run(switch.tick())
    pod.adopt()
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED
    return switch, pod, clock, store


def test_the_controller_moves_the_backhaul_station_to_another_upstream_bss(tmp_path):
    switch, pod, clock, _ = on_easymesh(tmp_path)
    assert switch.steer(NEXT.upper()) is None
    assert switch.steering_outcome(NEXT) is None
    asyncio.run(switch.tick())
    assert (
        len(pod.sent) == 2 and pod.sent[1][2]["row"]["bssid"] == NEXT
    )  # the same switch, re-pinned
    assert switch.steering_outcome(NEXT) is None  # not on the target yet
    pod.adopt(parent=NEXT)
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED and switch.steering_outcome(NEXT) is True
    status = switch.status()
    assert (status["bssid"], status["configured_bssid"], status["parent"]) == (NEXT, PARENT, NEXT)
    # and back to the gateway: a switch of its own, although that upstream was used on this start
    assert switch.steer(PARENT) is None
    asyncio.run(switch.tick())
    assert len(pod.sent) == 3 and pod.sent[2][2]["row"]["bssid"] == PARENT
    pod.adopt()
    asyncio.run(switch.tick())
    assert switch.steering_outcome(PARENT) is True and not switch.held()


GRE_PARENT = "72:00:00:00:6a:00"


def test_a_gre_child_is_steered_onto_a_parent_as_a_3_address_gre_station(tmp_path):
    # A GRE child (spec 8.6) has no Multi-AP configured upstream (bssid=None); EMOSA writes
    # nothing until the controller steers it, then onto a fleet parent it joins 3-address.
    switch, pod, _, _ = rig(tmp_path, bssid=None, parents=lambda: frozenset({GRE_PARENT}))
    asyncio.run(switch.tick())  # on its GTP bootstrap (gre); no proactive switch
    assert pod.sent == []
    assert switch.steer(GRE_PARENT) is None
    asyncio.run(switch.tick())
    cred = pod.sent[-1][2]["row"]  # the inserted Wifi_Credential_Config
    assert cred["onboard_type"] == "gre" and cred["bssid"] == GRE_PARENT
    assert switch.steering_outcome(GRE_PARENT) is None  # not on it yet
    pod.adopt_gre(parent=GRE_PARENT)
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED
    assert switch.steering_outcome(GRE_PARENT) is True and switch.status()["uplink"] == "gre"


def test_a_steer_to_a_non_parent_is_a_multi_ap_join_never_gre(tmp_path):
    # never GRE onto a foreign AP: a target not in the fleet's parent set is a Multi-AP join.
    switch, pod, _, _ = on_easymesh(tmp_path)  # parents default to the empty set
    assert switch.steer(NEXT) is None
    asyncio.run(switch.tick())
    assert pod.sent[-1][2]["row"]["onboard_type"] == "multi_ap"


def test_a_move_to_the_current_upstream_is_done_at_once(tmp_path):
    switch, pod, _, _ = on_easymesh(tmp_path)
    assert switch.steer(PARENT) is None and switch.steering_outcome(PARENT) is True
    asyncio.run(switch.tick())
    assert len(pod.sent) == 1


def test_a_move_that_is_not_confirmed_returns_to_the_previous_upstream_without_a_hold(tmp_path):
    switch, pod, clock, _ = on_easymesh(tmp_path)
    assert switch.steer(NEXT) is None
    asyncio.run(switch.tick())
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())
    assert switch.steering_outcome(NEXT) == "not confirmed within the deadline"
    assert not switch.held() and switch.status()["bssid"] == PARENT
    # the pod lost its uplink and restarted to its bootstrap: its next start goes back
    pod.restart("00000000-0000-4000-8000-0000000000a2")
    asyncio.run(switch.tick())
    assert pod.sent[-1][2]["row"]["bssid"] == PARENT


def test_no_move_off_the_gtp_path_or_while_held_or_switching(tmp_path):
    switch, pod, clock, store = rig(tmp_path)
    asyncio.run(switch.backend.snapshot())
    assert switch.steer(NEXT) == "not_on_easymesh_backhaul"  # still on its bootstrap GRE
    switch, pod, clock, store = on_easymesh(tmp_path / "held")
    store.conflict("pod-1", {"reason": "test"})
    assert switch.steer(NEXT) == "held_on_option_2"
    switch, pod, clock, store = on_easymesh(tmp_path / "busy")
    assert switch.steer(NEXT) is None
    asyncio.run(switch.tick())  # the move's switch is under way
    assert switch.steer("02:00:00:00:39:01") == "switch_in_progress"


def test_a_kept_upstream_not_reached_falls_back_to_the_configured_one_without_a_hold(tmp_path):
    switch, pod, clock, store = on_easymesh(tmp_path)
    switch.steer(NEXT)
    asyncio.run(switch.tick())
    pod.adopt(parent=NEXT)
    asyncio.run(switch.tick())
    assert switch.steering_outcome(NEXT) is True
    # the pod started again (OpenSync to its bootstrap) where NEXT can no longer be reached
    pod.restart("00000000-0000-4000-8000-0000000000a2")
    asyncio.run(switch.tick())
    assert pod.sent[-1][2]["row"]["bssid"] == NEXT  # the kept upstream first
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())
    timed_out = switch.latest()
    assert timed_out.state == State.TIMED_OUT and timed_out.intent["bssid"] == NEXT
    assert not switch.held() and switch.status()["bssid"] == PARENT
    assert not (store.directory / "target.json").exists()
    # nothing written while the station still carries the failed credential: cm would
    # revert a switch written now with it
    assert pod.sent[-1][2]["row"]["bssid"] == NEXT
    assert switch.status()["waiting"] == "the pod returning to its bootstrap uplink"
    pod.revert()  # cm: the station back on its bootstrap credential, the same start
    asyncio.run(switch.tick())
    assert pod.sent[-1][2]["row"]["bssid"] == PARENT  # then the configured one
    pod.adopt()
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED and not switch.held()


def test_a_gre_childs_failed_kept_switch_is_undone_while_its_pod_stays_up(tmp_path):
    """Spec 8.3 "Moved", 8.6 (13 October): a GRE child's kept parent is gone on its next start,
    and the pod stays up on its fallback station (cm's uplink elsewhere, OpenSync not restarted).
    After the switch's deadline the agent undoes its own write - the station's snapshot back, its
    credential deleted - confirmed once the credential is off the station; then nothing more."""
    switch, pod, clock, store = rig(tmp_path, bssid=None, parents=lambda: frozenset({GRE_PARENT}))
    asyncio.run(switch.tick())  # the bootstrap recorded
    assert switch.steer(GRE_PARENT) is None
    asyncio.run(switch.tick())
    pod.adopt_gre(parent=GRE_PARENT)
    asyncio.run(switch.tick())
    assert switch.steering_outcome(GRE_PARENT) is True  # the controller answered
    pod.restart("00000000-0000-4000-8000-0000000000a2")  # the parent gone meanwhile
    asyncio.run(switch.tick())
    assert pod.sent[-1][2]["row"]["bssid"] == GRE_PARENT  # the kept target re-applied
    # the station cannot join; the pod's fallback station keeps cm's uplink working
    fallback = {"if_name": "g-bhaul-sta-24", "if_type": "gre", "is_used": True}
    pod.tables["Connection_Manager_Uplink"] = {UPLINK: fallback}
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())  # the kept switch times out: the target dropped, no hold
    assert not switch.held() and not (store.directory / "target.json").exists()
    undo = pod.sent[-1]
    assert ("delete", "Wifi_Credential_Config") in [(o["op"], o.get("table")) for o in undo]
    update = next(o for o in undo if o["op"] == "update" and o["table"] == "Wifi_VIF_Config")
    assert update["row"]["ssid"] == "opensync-lab-bhaul"  # the snapshot back
    assert update["row"]["credential_configs"] == ["set", [["uuid", BOOT_CRED]]]
    # owm takes the restored row; cm stays on the fallback station
    pod.tables["Wifi_VIF_Config"] = bootstrap()["Wifi_VIF_Config"]
    pod.generation += 1
    asyncio.run(switch.tick())
    done = switch.latest()
    assert done.state == State.OBSERVED_APPLIED and done.intent["mode"] == BOOTSTRAP
    sent = len(pod.sent)
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())
    assert len(pod.sent) == sent and not switch.held()
    assert switch.status()["waiting"] == "no upstream (awaiting a Backhaul Steering Request)"


def test_a_gre_childs_failed_move_from_its_bootstrap_is_undone(tmp_path):
    """Spec 8.3, 8.6: a GRE child's move onto a parent it never joins returns to no upstream,
    without a hold; the agent undoes its own write on the station, as for a failed kept target."""
    switch, pod, clock, _ = rig(tmp_path, bssid=None, parents=lambda: frozenset({GRE_PARENT}))
    asyncio.run(switch.tick())
    assert switch.steer(GRE_PARENT) is None
    asyncio.run(switch.tick())
    pod.tables["Connection_Manager_Uplink"] = {
        UPLINK: {"if_name": "g-bhaul-sta-24", "if_type": "gre", "is_used": True}
    }
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())
    assert switch.steering_outcome(GRE_PARENT) == "not confirmed within the deadline"
    assert not switch.held()
    undo = pod.sent[-1]
    assert ("delete", "Wifi_Credential_Config") in [(o["op"], o.get("table")) for o in undo]
    pod.tables["Wifi_VIF_Config"] = bootstrap()["Wifi_VIF_Config"]
    pod.generation += 1
    asyncio.run(switch.tick())
    assert switch.latest().state == State.OBSERVED_APPLIED and switch.bssid is None


def test_the_controllers_steer_back_still_needs_the_station_on_the_bootstrap(tmp_path):
    """Spec 8.6: the undo's lighter confirmation is the agent's own; a revert the controller asked
    for is confirmed only with the station on the bootstrap BSS (it answers the controller)."""
    switch, pod, clock, _ = rig(tmp_path, bssid=None, parents=lambda: {PARENT})
    asyncio.run(switch.tick())
    boot = dict(switch.bootstrap)
    assert switch.steer(PARENT) is None
    asyncio.run(switch.tick())
    pod.adopt_gre(PARENT)
    asyncio.run(switch.tick())
    assert switch.steer(boot["bssid"]) is None
    asyncio.run(switch.tick())  # the revert written
    # the station's row is back, but cm stays on another uplink: not confirmed
    pod.tables["Wifi_VIF_Config"] = bootstrap()["Wifi_VIF_Config"]
    fallback = {"if_name": "g-bhaul-sta-24", "if_type": "gre", "is_used": True}
    pod.tables["Connection_Manager_Uplink"] = {UPLINK: fallback}
    pod.generation += 1
    asyncio.run(switch.tick())
    assert switch.latest().state == State.CONFIG_COMMITTED
    assert switch.steering_outcome(boot["bssid"]) is None


def test_the_configured_upstream_not_reached_after_a_kept_one_holds(tmp_path):
    switch, pod, clock, store = on_easymesh(tmp_path)
    switch.steer(NEXT)
    asyncio.run(switch.tick())
    pod.adopt(parent=NEXT)
    asyncio.run(switch.tick())
    pod.restart("00000000-0000-4000-8000-0000000000a2")
    asyncio.run(switch.tick())
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())  # the kept one timed out
    pod.restart("00000000-0000-4000-8000-0000000000a3")  # OpenSync restarted: a new start
    asyncio.run(switch.tick())  # the configured one now
    clock.advance(DEADLINE + 1)
    asyncio.run(switch.tick())
    assert switch.latest().state == State.TIMED_OUT and switch.held()
    assert len(pod.sent) == 4  # the first switch, the move, the kept one, the configured one


def test_the_moved_upstream_is_kept_for_later_starts_while_the_configuration_is_unchanged(tmp_path):
    switch, pod, clock, store = on_easymesh(tmp_path)
    switch.steer(NEXT)
    asyncio.run(switch.tick())
    pod.adopt(parent=NEXT)
    asyncio.run(switch.tick())

    def again(bssid):  # the agent restarted, with this configured upstream
        return UplinkSwitch(
            "pod-1",
            switch.backend,
            store,
            switch.engine.vault,
            lambda: ("emosa-mesh-bh", "backhaul"),
            bssid=bssid,
            run_id="pod-1",
            clock=clock,
        )

    assert again(PARENT).bssid == NEXT
    assert again("02:00:00:00:33:01").bssid == "02:00:00:00:33:01"  # the operator pinned another
