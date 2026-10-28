# SPDX-License-Identifier: Apache-2.0
"""The fleet front port: one persisted virtual agent per pod that connects."""

import asyncio
import json
import socket
import threading
import time

import pytest

from emosa.agent.fleet import (
    Fleet,
    Registry,
    agent_config,
    derive_al,
    free_underlay,
    held_underlay,
    recognised,
)
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
def test_free_underlay_avoids_the_gtp_underlay_and_the_used_ones():
    assert free_underlay("169.254.1.0/24", set()) == "169.254.2.0/24"  # the RDK lab's GTP
    assert free_underlay("169.254.2.0/25", set()) == "169.254.1.0/24"  # a router's GTP
    assert free_underlay("169.254.2.0/25", {"169.254.1.0/24"}) == "169.254.3.0/24"
    assert free_underlay("169.254.0.0/16", set()) is None  # every /24 overlaps it
    every = {f"169.254.{n}.0/24" for n in range(2, 255)}
    assert free_underlay("169.254.1.0/24", every) is None


@pytest.mark.unit
def test_registry_allocates_each_pod_an_underlay_only_with_a_gtp_underlay(tmp_path):
    from emosa.config import validate

    path = tmp_path / "fleet.json"
    registry = Registry(path, range(6651, 6654), gtp_underlay="169.254.1.0/24")
    a = registry.assign({"serial_number": "A"})
    b = registry.assign({"serial_number": "B"})
    assert (a["underlay"], b["underlay"]) == ("169.254.2.0/24", "169.254.3.0/24")
    validate("fleet-registry", json.loads(path.read_text()))
    # kept across a pod's reconnects and the fleet's restarts
    again = Registry(path, range(6651, 6654), gtp_underlay="169.254.1.0/24")
    assert again.assign({"serial_number": "B"})["underlay"] == "169.254.3.0/24"
    # a site whose GTP underlay changed onto one: that pod alone gets another
    moved = Registry(path, range(6651, 6654), gtp_underlay="169.254.3.0/25")
    assert moved.assign({"serial_number": "B"})["underlay"] == "169.254.1.0/24"
    assert moved.assign({"serial_number": "A"})["underlay"] == "169.254.2.0/24"
    # without one, none is allocated and none reaches the agent
    plain = Registry(tmp_path / "plain.json", range(6651, 6654))
    assert "underlay" not in plain.assign({"serial_number": "C"})
    with_gtp = fleet_config(tmp_path, gtp_underlay="169.254.1.0/24")
    validate("fleet-config", with_gtp)
    config = agent_config(a, with_gtp)
    validate("agent-config", config)
    assert config["underlay"] == "169.254.2.0/24"
    assert "underlay" not in agent_config(a, fleet_config(tmp_path))


@pytest.mark.unit
def test_a_restarted_fleet_places_the_underlays_before_its_agents_start(tmp_path):
    """A registered pod's agent starts without the pod at the front port, so an underlay the
    GTP underlay moved onto is allocated again at the fleet's start, and one is allocated to
    an entry from before the fleet had a GTP underlay."""
    started = []
    fleet = make_fleet(tmp_path, started)
    fleet.registry.assign({"serial_number": "A"})
    fleet = make_fleet(tmp_path, started, gtp_underlay="169.254.1.0/24")
    fleet.registry.assign({"serial_number": "B"})
    assert fleet.registry.agents["B"]["underlay"] == "169.254.2.0/24"
    del started[:]
    fleet = make_fleet(tmp_path, started, gtp_underlay="169.254.2.0/25")
    fleet.start_registered()
    configs = {p: json.loads((tmp_path / "etc" / f"{p}.json").read_text()) for p in "AB"}
    assert (configs["A"]["underlay"], configs["B"]["underlay"]) == (
        "169.254.1.0/24",
        "169.254.3.0/24",
    )
    saved = json.loads((tmp_path / "state" / "fleet.json").read_text())
    assert [saved[p]["underlay"] for p in "AB"] == ["169.254.1.0/24", "169.254.3.0/24"]


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


LEGACY = {"model": "BCM_LEGACY_POD", "firmware_prefix": "2.4.3", "profile": "bcm-pod-2.4.3-gre-v1"}


@pytest.mark.unit
def test_the_first_model_entry_that_matches_the_pods_model_and_firmware_recognises_it():
    later = {"model": "BCM_LEGACY_POD", "profile": "bcm-pod-2.4.3-gre-5g-v1"}
    assert recognised([LEGACY, later], "BCM_LEGACY_POD", "2.4.3-0-legacy") == (0, LEGACY)
    assert recognised([LEGACY, later], "BCM_LEGACY_POD", "2.6.0") == (1, later)
    assert recognised([LEGACY, later], "BCM_LEGACY_POD", None) == (1, later)
    assert recognised([LEGACY], "BCM_LEGACY_POD", None) == (None, None)
    assert recognised([LEGACY], "bcm_legacy_pod", "2.4.3") == (None, None)  # equal, not alike
    assert recognised([LEGACY, later], None, "2.4.3") == (None, None)
    assert recognised(None, "BCM_LEGACY_POD", "2.4.3") == (None, None)


@pytest.mark.unit
def test_a_pods_model_gives_it_settings_under_its_own_and_over_the_fleets(tmp_path):
    from emosa.config import validate

    config = fleet_config(
        tmp_path,
        multi_bss=True,
        models=[{**LEGACY, "phy_netdev_up": False, "multi_bss": False}],
        pods={"POD2": {"multi_bss": True}},
    )
    validate("fleet-config", config)
    entry = {"pod_id": "POD1", "port": 6651, "interface": "em1", "al_mac": derive_al("POD1")}
    legacy = {**entry, "model": "BCM_LEGACY_POD", "firmware": "2.4.3-0-legacy"}
    own = agent_config(legacy, config)
    validate("agent-config", own)
    assert (own["profile"], own["multi_bss"], own["phy_netdev_up"]) == (
        LEGACY["profile"],
        False,
        False,
    )
    mine = agent_config({**legacy, "pod_id": "POD2"}, config)
    assert (mine["profile"], mine["multi_bss"]) == (LEGACY["profile"], True)
    other = agent_config({**entry, "model": "m", "firmware": "2.4.3"}, config)
    assert other["multi_bss"] is True and other["profile"] == "opensync-lab-hwsim-6.6.1-v1"
    assert "phy_netdev_up" not in other


@pytest.mark.unit
def test_the_admit_switch_admits_a_pod_of_a_known_model_and_no_other(tmp_path, caplog):
    started = []
    fleet = make_fleet(tmp_path, started, admit=["POD1"], models=[LEGACY])
    legacy = Pod("POD2")
    legacy.row.update(model="BCM_LEGACY_POD", firmware_version="2.4.3-0-legacy")
    fleet._call = legacy
    assert fleet.handle(None, "peer") is None and not legacy.updates  # the switch off
    fleet = make_fleet(tmp_path, started, admit=["POD1"], models=[LEGACY], admit_by_model=True)
    fleet._call = legacy
    with caplog.at_level("INFO"):
        assert fleet.handle(None, "peer")["pod_id"] == "POD2" and legacy.updates
    assert "admitted by model, models[0] (BCM_LEGACY_POD)" in caplog.text
    config = json.loads((tmp_path / "etc/POD2.json").read_text())
    assert config["profile"] == LEGACY["profile"]
    newer = Pod("POD3")
    newer.row.update(model="BCM_LEGACY_POD", firmware_version="2.6.0")
    fleet._call = newer
    assert fleet.handle(None, "peer") is None and not newer.updates
    fleet._call = Pod("POD1")
    with caplog.at_level("INFO"):
        fleet.handle(None, "peer")
    assert "admitted by serial" in caplog.text.splitlines()[-1]
    # at the fleet's start, by the model and firmware the registry keeps
    del started[:]
    make_fleet(
        tmp_path, started, admit=["POD1"], models=[LEGACY], admit_by_model=True
    ).start_registered()
    assert [s[0] for s in started] == ["POD1", "POD2"]
    del started[:]
    make_fleet(tmp_path, started, admit=["POD1"], models=[LEGACY]).start_registered()
    assert [s[0] for s in started] == ["POD1"]


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


@pytest.mark.parametrize("command", ["serve", "forget"])
def test_the_reference_refuses_supervised_agents(tmp_path, monkeypatch, capsys, command):
    """spec §4: the agents as the fleet's children are the C fleet's; the reference
    says so instead of starting systemd units the configuration did not ask for."""
    from emosa.agent import fleet as fleet_module

    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(fleet_config(tmp_path, agents="supervised")))
    monkeypatch.setattr("sys.argv", ["emosa-fleet", command, str(path), "POD1"])
    with pytest.raises(SystemExit) as stopped:
        fleet_module.main()
    assert stopped.value.code == 2
    assert "emosa-fleet-c" in capsys.readouterr().err
    assert not (tmp_path / "state").exists()


def fleet_secret(directory, ref, value):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / ref
    path.write_text(value)
    path.chmod(0o600)


def test_an_agents_secret_is_provisioned_from_the_fleets_store(tmp_path):
    # spec §4: the GTP passphrase kept once per gateway (the fleet's secrets), copied into the
    # agent's store when its configuration names it, so a forgotten or new pod needs no hand
    fleet_secret(tmp_path / "fleet-secrets", "gtp", "FleetGtpPassphrase1")
    started = []
    bootstrap = {"ssid": "site-gtp", "secret_ref": "gtp"}
    fleet = make_fleet(
        tmp_path,
        started,
        secrets=str(tmp_path / "fleet-secrets"),
        pods={"POD2": {"uplink": {"mode": "off", "bootstrap": bootstrap}}},
    )
    entry = {"pod_id": "POD2", "port": 6652, "interface": "em2", "al_mac": derive_al("POD2")}
    fleet.bind(entry)
    copied = tmp_path / "state" / "POD2" / "secrets" / "gtp"
    assert copied.read_text() == "FleetGtpPassphrase1"
    assert copied.stat().st_mode & 0o777 == 0o600
    assert (copied.parent.stat().st_mode & 0o777) == 0o700
    assert started == [("POD2", False)]
    # a copy: the fleet's store keeps it for the next pod (14 October: gone from a gateway's)
    assert (tmp_path / "fleet-secrets" / "gtp").read_text() == "FleetGtpPassphrase1"


def test_an_agents_own_secret_is_never_replaced_and_a_missing_one_is_only_logged(tmp_path, caplog):
    fleet_secret(tmp_path / "fleet-secrets", "gtp", "FleetGtpPassphrase1")
    fleet_secret(tmp_path / "state" / "POD2" / "secrets", "gtp", "AgentsOwnPassphrase")
    started = []
    fleet = make_fleet(
        tmp_path,
        started,
        secrets=str(tmp_path / "fleet-secrets"),
        pods={
            "POD2": {"uplink": {"mode": "off", "bootstrap": {"ssid": "g", "secret_ref": "gtp"}}},
            "POD3": {"uplink": {"mode": "off", "bootstrap": {"ssid": "g", "secret_ref": "other"}}},
        },
    )
    for pod, port in (("POD2", 6652), ("POD3", 6653)):
        fleet.bind({"pod_id": pod, "port": port, "interface": "em2", "al_mac": derive_al(pod)})
    assert (tmp_path / "state" / "POD2" / "secrets" / "gtp").read_text() == "AgentsOwnPassphrase"
    assert not (tmp_path / "state" / "POD3" / "secrets" / "other").exists()
    assert "other" in caplog.text and "FleetGtpPassphrase1" not in caplog.text
    assert [pod for pod, _ in started] == ["POD2", "POD3"]  # the agent starts all the same


@pytest.mark.unit
def test_registry_adopts_a_dialled_port_only_when_no_entry_holds_it(tmp_path):
    registry = Registry(tmp_path / "fleet.json", range(6651, 6654))
    placed, b = registry.adopt({"serial_number": "B", "id": "b"}, 6653)
    assert placed == "adopted" and (b["port"], b["interface"]) == (6653, "em3")
    assert b["al_mac"] == derive_al("B") and b["handovers"] == 1
    # a pod with an entry keeps it, whichever agent port it dialled
    placed, again = registry.adopt({"serial_number": "B", "id": "b"}, 6651)
    assert placed == "registered" and again["port"] == 6653 and again["handovers"] == 2
    # another pod's port, or one outside the range, is not adopted
    assert registry.adopt({"serial_number": "C", "id": "c"}, 6653) == ("taken", None)
    assert registry.adopt({"serial_number": "C", "id": "c"}, 7000) == ("taken", None)
    assert registry.free_ports() == [6651, 6652]
    assert Registry(tmp_path / "fleet.json", range(6651, 6654)).agents["B"]["port"] == 6653


@pytest.mark.unit
def test_after_a_lost_registry_each_pod_is_readmitted_on_the_port_it_dials(tmp_path):
    """A factory reset: the configurations back, the registry gone. Each pod still dials its own
    agent port; whatever order they come back in, each gets its port, interface and AL MAC back,
    where the front port would hand them out by arrival order."""
    started = []
    fleet = make_fleet(tmp_path, started)
    before = {}
    for serial in ("POD1", "POD2", "POD3"):
        fleet._call = Pod(serial)
        before[serial] = fleet.handle(None, "peer")
    (tmp_path / "state" / "fleet.json").unlink()
    for serial in ("POD3", "POD1", "POD2"):
        fleet._call = pod = Pod(serial)
        entry = fleet.handle(None, "peer", before[serial]["port"])
        assert {k: entry[k] for k in ("port", "interface", "al_mac")} == {
            k: before[serial][k] for k in ("port", "interface", "al_mac")
        }
        assert pod.updates == [{"manager_addr": f"tcp:10.101.0.1:{entry['port']}"}]
    assert started[-3:] == [("POD3", False), ("POD1", False), ("POD2", False)]
    # the contrast: by arrival order at the front port, POD3 would have taken POD1's port
    (tmp_path / "state" / "fleet.json").unlink()
    fleet._call = Pod("POD3")
    assert fleet.handle(None, "peer")["port"] == before["POD1"]["port"]


@pytest.mark.unit
def test_a_pod_on_another_pods_agent_port_or_not_admitted_is_left_unchanged(tmp_path):
    started = []
    fleet = make_fleet(tmp_path, started, admit=["POD1", "POD2"])
    fleet._call = Pod("POD1")
    assert fleet.handle(None, "peer", 6652)["port"] == 6652
    fleet._call = pod = Pod("POD2")
    assert fleet.handle(None, "peer", 6652) is None and not pod.updates
    fleet._call = pod = Pod("POD9")  # not admitted
    assert fleet.handle(None, "peer", 6653) is None and not pod.updates
    assert [s[0] for s in started] == ["POD1"] and set(fleet.registry.agents) == {"POD1"}
    # a pod with an entry that dials another free port is handed over to its own
    fleet._call = pod = Pod("POD1")
    assert fleet.handle(None, "peer", 6653)["port"] == 6652
    assert pod.updates == [{"manager_addr": "tcp:10.101.0.1:6652"}]


def free_tcp_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def dial_as_pod(port, serial, updates):
    """A pod's ovsdb-server dialling agent port ``port`` as its cm does: it answers the fleet's
    select of AWLAN_Node and its update, until the fleet ends the session."""
    decoder, buffer = json.JSONDecoder(), ""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        while data := sock.recv(65536):
            buffer += data.decode()
            while buffer.strip():
                try:
                    message, end = decoder.raw_decode(buffer.lstrip())
                except ValueError:
                    break
                buffer = buffer.lstrip()[end:]
                op = message["params"][1]
                if op["op"] == "select":
                    row = {"id": "node-" + serial, "serial_number": serial, "model": "m"}
                    result = [{"rows": [row]}]
                else:
                    updates.append(op["row"])
                    result = [{"count": 1}]
                reply = {"id": message["id"], "result": result, "error": None}
                sock.sendall(json.dumps(reply).encode())


def refused(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except ConnectionRefusedError:
        return True
    return False


@pytest.mark.unit
def test_a_serving_fleet_listens_on_the_free_agent_ports_and_readmits_a_pod_there(tmp_path):
    port = free_tcp_port()
    started = []
    fleet = make_fleet(tmp_path, started, ports=[port, port])
    ready = threading.Event()
    pstream = fleet.listen()
    thread = threading.Thread(target=fleet.serve, args=(pstream, ready), daemon=True)
    thread.start()
    try:
        assert ready.wait(5)
        updates = []
        dial_as_pod(port, "EMOSA-SIM-002", updates)
        assert updates == [{"manager_addr": f"tcp:10.101.0.1:{port}"}]
        assert started == [("EMOSA-SIM-002", False)]
        assert fleet.registry.agents["EMOSA-SIM-002"]["port"] == port
        # the port is its agent's now: the fleet no longer listens on it
        assert refused(port)
        # released, the port is the fleet's again, its next look at the registry
        fleet.forget("EMOSA-SIM-002")
        end = time.monotonic() + 5
        while refused(port) and time.monotonic() < end:
            time.sleep(0.1)
        assert not refused(port)
    finally:
        fleet.stop.set()
        thread.join(5)


def inet_row(name, address, netmask="255.255.255.0", scheme="static"):
    return {"if_name": name, "inet_addr": address, "netmask": netmask, "ip_assign_scheme": scheme}


@pytest.mark.unit
def test_a_held_underlay_is_one_static_dot_one_on_a_link_local_slash_24():
    assert held_underlay([inet_row("wl1.1", "169.254.5.1")]) == "169.254.5.0/24"
    # the same on two of its parent APs is still one
    two_aps = [inet_row("wl1.1", "169.254.5.1"), inet_row("wl0.1", "169.254.5.1")]
    assert held_underlay(two_aps) == "169.254.5.0/24"
    two_underlays = [inet_row("wl1.1", "169.254.5.1"), inet_row("wl0.1", "169.254.6.1")]
    assert held_underlay(two_underlays) is None
    assert held_underlay([inet_row("wl1.1", "169.254.5.2")]) is None
    assert held_underlay([inet_row("wl1.1", "169.254.5.1", netmask="255.255.255.128")]) is None
    assert held_underlay([inet_row("wl1.1", "169.254.5.1", scheme="dhcp")]) is None
    assert held_underlay([inet_row("br-home", "192.168.1.1")]) is None
    assert held_underlay([inet_row("wl1.1", "169.254.0.1")]) is None  # N is 1 to 254
    unset = {"if_name": "wl1.1", "inet_addr": ["set", []], "netmask": ["set", []]}
    assert held_underlay([{**unset, "ip_assign_scheme": "static"}]) is None
    assert held_underlay(None) is None


@pytest.mark.unit
def test_on_first_sight_a_gre_parent_keeps_its_underlay_when_free_and_clear_of_the_gtp(tmp_path):
    registry = Registry(tmp_path / "fleet.json", range(6651, 6655), gtp_underlay="169.254.1.0/24")
    assert registry.assign({"serial_number": "A"}, "169.254.7.0/24")["underlay"] == "169.254.7.0/24"
    # A's already, or over the GTP underlay: allocated as before, the lowest free
    assert registry.assign({"serial_number": "B"}, "169.254.7.0/24")["underlay"] == "169.254.2.0/24"
    assert registry.assign({"serial_number": "C"}, "169.254.1.0/24")["underlay"] == "169.254.3.0/24"
    # a pod with an underlay keeps its own, whatever its rows say
    assert registry.assign({"serial_number": "A"}, "169.254.9.0/24")["underlay"] == "169.254.7.0/24"
    placed, d = registry.adopt({"serial_number": "D"}, 6654, "169.254.9.0/24")
    assert placed == "adopted" and d["underlay"] == "169.254.9.0/24"


@pytest.mark.unit
def test_a_fleet_with_gre_parents_reads_the_underlay_a_pod_has_with_its_identity(tmp_path):
    started, asked = [], []
    fleet = make_fleet(tmp_path, started, gtp_underlay="169.254.1.0/24")

    def call(conn, method, params, deadline):
        if params[1]["op"] == "select":
            asked.append([op["table"] for op in params[1:]])
            pod = {"id": "node", "serial_number": "POD1", "model": "m"}
            return [{"rows": [pod]}, {"rows": [inet_row("wl1.1", "169.254.12.1")]}]
        return [{"count": 1}]

    fleet._call = call
    entry = fleet.handle(None, "peer", 6652)
    assert asked == [["AWLAN_Node", "Wifi_Inet_Config"]]
    assert entry["port"] == 6652 and entry["underlay"] == "169.254.12.0/24"
    assert json.loads((tmp_path / "etc/POD1.json").read_text())["underlay"] == "169.254.12.0/24"
    # without a GTP underlay the fleet asks for the identity only, as before
    (tmp_path / "plain").mkdir()
    plain = make_fleet(tmp_path / "plain", started)
    asked.clear()
    plain._call = call
    plain.handle(None, "peer")
    assert asked == [["AWLAN_Node"]]
