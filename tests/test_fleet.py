"""The fleet front port: one persisted virtual agent per pod that connects."""

import asyncio
import json
import threading
import time

import pytest

from emosa.agent.fleet import Fleet, Registry, agent_config, derive_al
from emosa.opensync.session import OvsSession
from emosa_lab.simulation.database import SimDatabase

CONTROLLER = "02:00:00:e0:00:01"


def fleet_config(tmp_path, **extra):
    return {
        "listen": "punix:" + str(tmp_path / "fleet.sock"),
        "advertise": "10.101.0.1",
        "ports": [6651, 6653],
        "controller_al": CONTROLLER,
        "state_root": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "etc"),
        **extra,
    }


def make_fleet(tmp_path, started, **extra):
    config = fleet_config(tmp_path, **extra)
    for key in ("state_root", "config_dir"):
        (tmp_path / config[key].rsplit("/", 1)[1]).mkdir(exist_ok=True)
    return Fleet(
        config,
        starter=lambda pod, changed: started.append((pod, changed)),
        stopper=lambda pod: started.append((pod, "stopped")),
    )


class Pod:
    """A pod connection at the transaction level: AWLAN_Node select and update."""

    def __init__(self, serial, *, rows=1):
        self.row = {"id": "node-" + serial, "serial_number": serial, "model": "m"}
        self.rows, self.updates = rows, []

    def __call__(self, conn, method, params, deadline):
        op = params[1]
        if op["op"] == "select":
            return [{"rows": [dict(self.row) for _ in range(self.rows)]}]
        self.updates.append(op["row"])
        return [{"count": 1}]


@pytest.mark.unit
def test_al_mac_is_derived_from_the_serial_unicast_and_collision_free():
    al = derive_al("MVXPOD01")
    assert al == derive_al("MVXPOD01") != derive_al("MVXPOD02")
    assert int(al[:2], 16) & 0x03 == 0x02  # locally administered, unicast
    assert derive_al("MVXPOD01", taken={al}) != al


@pytest.mark.unit
def test_one_pod_can_have_its_own_profile_and_uplink_policy(tmp_path):
    from emosa.config import validate

    uplink = {
        "mode": "multi-ap",
        "credentials": "config",
        "ssid": "bh",
        "secret_ref": "bh",
        "bssid": "02:00:00:00:09:00",
    }
    config = fleet_config(tmp_path, multi_bss=True, pods={"POD2": {"uplink": uplink}})
    validate("fleet-config", config)
    entry = {"pod_id": "POD2", "port": 6652, "interface": "em2", "al_mac": derive_al("POD2")}
    own = agent_config(entry, config)
    validate("agent-config", own)
    assert own["uplink"] == uplink and own["multi_bss"] is True  # fleet-wide setting kept
    other = agent_config({**entry, "pod_id": "POD1"}, config)
    assert other["uplink"] == {"mode": "off"}


@pytest.mark.unit
def test_registry_allocates_once_persists_and_reports_full(tmp_path):
    registry = Registry(tmp_path / "fleet.json", range(6651, 6653))
    a = registry.assign({"serial_number": "A", "id": "a"})
    b = registry.assign({"serial_number": "B", "id": "b"})
    assert (a["port"], a["interface"], b["port"], b["interface"]) == (6651, "em1", 6652, "em2")
    assert registry.assign({"serial_number": "C"}) is None
    again = Registry(tmp_path / "fleet.json", range(6651, 6653)).assign({"serial_number": "A"})
    assert (again["port"], again["al_mac"], again["handovers"]) == (6651, a["al_mac"], 2)
    registry.forget("A")
    assert registry.assign({"serial_number": "C"})["port"] == 6651


@pytest.mark.unit
def test_every_pod_gets_its_own_agent_and_is_handed_to_its_port(tmp_path):
    started = []
    fleet = make_fleet(tmp_path, started, multi_bss=True)
    for serial in ("POD1", "POD2", "POD1"):
        pod = Pod(serial)
        fleet._call = pod
        entry = fleet.handle(None, "peer")
        assert pod.updates == [{"manager_addr": f"tcp:10.101.0.1:{entry['port']}"}]
    assert [s[0] for s in started] == ["POD1", "POD2", "POD1"]
    assert all(changed is False for _, changed in started)
    config = json.loads((tmp_path / "etc/POD2.json").read_text())
    assert config == agent_config(fleet.registry.agents["POD2"], fleet.config)
    assert config["ovsdb"] == "ptcp:6652:127.0.0.1" and config["multi_bss"] is True
    assert config["controller_al"] == CONTROLLER and config["interface"] == "em2"
    assert config["al_mac"] == derive_al("POD2")
    # A changed fleet setting reaches the running agent through a restart.
    fleet.config["message_set"] = "r1"
    fleet._call = Pod("POD2")
    fleet.handle(None, "peer")
    assert started[-1] == ("POD2", True)
    (tmp_path / "state" / "POD2" / "journal").mkdir(parents=True)
    released = fleet.forget("POD2")
    assert started[-1] == ("POD2", "stopped") and not (tmp_path / "etc/POD2.json").exists()
    # the old ownership period is archived, not reused by a re-admitted pod
    assert not (tmp_path / "state" / "POD2").exists()
    assert "POD2.released-" in released["archived_state"]
    assert list((tmp_path / "state").glob("POD2.released-*/journal"))


@pytest.mark.unit
@pytest.mark.parametrize("serial,rows", [("bad serial", 1), ("../x", 1), ("OK", 0), ("OK", 2)])
def test_unusable_identity_is_refused_without_changes(tmp_path, serial, rows):
    started = []
    fleet = make_fleet(tmp_path, started)
    pod = Pod(serial, rows=rows)
    fleet._call = pod
    with pytest.raises(ValueError):
        fleet.handle(None, "peer")
    assert not started and not pod.updates and not fleet.registry.agents


@pytest.mark.unit
def test_admission_list_and_capacity_leave_other_pods_unchanged(tmp_path):
    started = []
    fleet = make_fleet(tmp_path, started, admit=["POD1"], ports=[6651, 6651])
    pods = {serial: Pod(serial) for serial in ("POD2", "POD1")}
    for pod in pods.values():
        fleet._call = pod
        fleet.handle(None, "peer")
    assert not pods["POD2"].updates and pods["POD1"].updates
    assert [s[0] for s in started] == ["POD1"] and "POD2" not in fleet.registry.agents
    fleet.admit = None
    fleet._call = pod = Pod("POD3")
    assert fleet.handle(None, "peer") is None and not pod.updates


@pytest.mark.ovsdb
def test_a_connecting_ovsdb_server_is_identified_and_handed_over(tmp_path):
    async def scenario():
        db = await SimDatabase().start()
        started = []
        fleet = make_fleet(tmp_path, started)
        ready = threading.Event()
        pstream = fleet.listen()  # ovs registers its punix signal hooks on the main thread
        thread = threading.Thread(target=fleet.serve, args=(pstream, ready), daemon=True)
        thread.start()
        admin = OvsSession(db.endpoint)
        try:
            await db.seed(serial_number="EMOSA-SIM-001")
            assert ready.wait(5)
            await db.manager_remote("unix:" + str(tmp_path / "fleet.sock"))
            end = time.monotonic() + 10
            while not started and time.monotonic() < end:
                await asyncio.sleep(0.05)
            assert started and started[0][0] == "EMOSA-SIM-001"
            await db.manager_remote("unix:" + str(tmp_path / "fleet.sock"), connect=False)
            select = {
                "op": "select",
                "table": "AWLAN_Node",
                "where": [],
                "columns": ["manager_addr"],
            }
            while True:
                rows = (await admin.transact([select]))[0]["rows"]
                if rows[0]["manager_addr"] or time.monotonic() > end:
                    break
                await asyncio.sleep(0.05)
            assert rows[0]["manager_addr"] == "tcp:10.101.0.1:6651"
            entry = fleet.registry.agents["EMOSA-SIM-001"]
            assert entry["model"] == "EMOSA synthetic extender" and entry["port"] == 6651
        finally:
            fleet.stop.set()
            thread.join(5)
            await admin.close()
            await db.close()

    asyncio.run(scenario())
