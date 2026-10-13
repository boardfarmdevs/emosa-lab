# SPDX-License-Identifier: Apache-2.0
"""The lab in a box (easymesh-labs alignment plan 8.2): the real agent, C or Python,
against a fake pod and a scripted controller in a private network namespace, with no
lab VM.

    python -m emosa_lab.box boot --agent c --directory DIR

The pod is a disposable ovsdb-server holding the recorded rows of an unchanged
OpenSync 6.6.1 pod (tests/fixtures/opensync/pod-6.6.1-hwsim-tables.json); it dials
the agent as a pod does after its redirect. The agent's 1905 interface is one end of
a veth pair; the controller's side sees every frame it sends.

The box runs in a user and network namespace of its own (unshare), so it needs no
root and touches no host interface.
"""

import argparse
import asyncio
import codecs
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import ovs.jsonrpc
import ovs.stream

from emosa.agent.fleet import agent_config, derive_al
from emosa.opensync.schema import reference_path
from emosa.opensync.session import OvsSession
from emosa.wire.cmdu import Reassembler, Tlv, fragment_message
from emosa.wsc import decode_attributes
from emosa_lab.simulation.database import SimDatabase
from emosa_lab.simulation.manager import APPLY_COLUMNS
from emosa_lab.simulation.wsc_provisioning import registrar_reply

ROOT = Path(__file__).resolve().parents[3]
POD_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-tables.json"
# another pod, on a Multi-AP Wi-Fi backhaul (bhaul-sta-24 on emosa-lab-bh, data plane option 1)
MULTI_AP_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-uplink-multi-ap.json"
# a pod on its GTP bootstrap: bhaul-sta-24 in vconf mode on the bootstrap BSS, cm on the gretap.
# For the operator steer onto a parent and back (spec 8.6 revert); the agent awaits a steer.
BOOTSTRAP_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-bootstrap.json"
BOOTSTRAP_BSS = "02:00:00:00:1b:01"
# a fleet GRE parent AP (spec 8.6): a steer onto it is a 3-address GRE join, not a Multi-AP switch
GRE_PARENT_AP = "72:00:00:00:0a:01"
BACKHAUL_PARENT, BACKHAUL_STATION = "02:00:00:00:19:01", "02:00:00:00:14:00"
UPLINK = {
    "mode": "multi-ap",
    "bssid": BACKHAUL_PARENT,
    "credentials": "config",
    "ssid": "emosa-lab-bh",
    "secret_ref": "backhaul",
    "station": "bhaul-sta-24",
}
CONTROLLER_AL = "02:00:00:00:00:02"
AGENT_PORT = 6651
ETHERTYPE = 0x893A
AGENT_INTERFACE, CONTROLLER_INTERFACE = "em0", "ctl0"
AUTOCONFIG_SEARCH, AUTOCONFIG_RESPONSE, AUTOCONFIG_WSC = 0x0007, 0x0008, 0x0009
REGISTRAR = ROOT / ".cache/wsc-registrar/component-registrar"  # scripts/build-wsc-registrar.py
REGISTRAR_SSID = "EMOSA-WSC-component"  # deploy/wire/component-registrar.c
COLD_VIF = "home-ap-24"  # the profiles' fronthaul VIF


def in_namespace():
    """Re-execute this command in a private user and network namespace, once."""
    if os.environ.get("EMOSA_BOX_NAMESPACE") == "1":
        return
    env = {**os.environ, "EMOSA_BOX_NAMESPACE": "1"}
    command = ["unshare", "--net", "--map-root-user", sys.executable, "-m", "emosa_lab.box"]
    os.execvpe("unshare", command + sys.argv[1:], env)


def network(interface=AGENT_INTERFACE):
    """lo, and the veth pair between the agent's interface and the controller's."""
    for command in (
        ["ip", "link", "set", "lo", "up"],
        ["ip", "link", "add", interface, "type", "veth", "peer", "name", CONTROLLER_INTERFACE],
        ["ip", "link", "set", interface, "up"],
        ["ip", "link", "set", CONTROLLER_INTERFACE, "up"],
    ):
        subprocess.run(command, check=True)


def recorded_pod(rows=POD_ROWS, replace=None, drop=()):
    """The recorded pod's tables as one insert transaction (references by name), each
    text in replace (a MAC, say) changed into its value. drop: interfaces left out, as on
    a pod whose bootstrap made their radio only (their VIF and Inet rows, and the
    references to them)."""
    text = Path(rows).read_text()
    for old, new in (replace or {}).items():
        text = text.replace(old, new)
    tables = json.loads(text)["tables"]
    gone = {
        uuid
        for table in ("Wifi_VIF_Config", "Wifi_VIF_State", "Wifi_Inet_Config")
        for uuid, row in tables.get(table, {}).items()
        if row.get("if_name") in drop
    }
    tables = {t: {u: r for u, r in rows.items() if u not in gone} for t, rows in tables.items()}
    names = {uuid: f"row{i}" for i, uuid in enumerate(u for rows in tables.values() for u in rows)}

    def named(value):
        if isinstance(value, list) and len(value) == 2 and value[0] == "uuid":
            if value[1] in gone:  # a set of one, its only member left out
                return ["set", []]
            return ["named-uuid", names[value[1]]] if value[1] in names else value
        if isinstance(value, list) and len(value) == 2 and value[0] == "set":
            return ["set", [named(v) for v in value[1] if not gone.intersection(uuids(v))]]
        if isinstance(value, list):
            return [named(v) for v in value]
        return value

    operations = []
    for table, rows in tables.items():
        for uuid, row in rows.items():
            operations.append(
                {
                    "op": "insert",
                    "table": table,
                    "uuid-name": names[uuid],
                    "row": {column: named(value) for column, value in row.items()},
                }
            )
    return tables, operations


async def table_rows(db, table):
    """One table of the pod's database, every column, decoded."""
    schema = json.loads(Path(reference_path()).read_text())
    columns = sorted(schema["tables"][table]["columns"])
    session = OvsSession(db.endpoint, read_only=True, monitor_columns={table: columns})
    try:
        snapshot = await session.snapshot()
    finally:
        await session.close()
    return snapshot["tables"].get(table, {})


async def db_transact(db, operations):
    session = OvsSession(db.endpoint)
    try:
        return await session.transact(operations)
    finally:
        await session.close()


FIRST_OCTETS = ("82", "a2", "c2", "e2", "86", "a6", "c6", "e6")  # locally administered


def uuids(value):
    """The UUIDs of a raw OVSDB reference or set of references."""
    if isinstance(value, list) and len(value) == 2 and value[0] == "uuid":
        return [value[1]]
    if isinstance(value, list) and len(value) == 2 and value[0] == "set":
        return [item[1] for item in value[1] if isinstance(item, list) and item[0] == "uuid"]
    return []


async def attach_vifs(db):
    """As OpenSync's wm does for a VIF a manager created: its State gets a BSSID (the radio's MAC
    under another locally administered first octet) and joins its radio's vif_states, where an
    agent's view of the pod finds the radio's BSSes. The number of changes made."""
    radio_configs = await table_rows(db, "Wifi_Radio_Config")
    radio_states = await table_rows(db, "Wifi_Radio_State")
    vif_states = await table_rows(db, "Wifi_VIF_State")
    by_config = {
        uuids(r.get("radio_config"))[0]: (u, r)
        for u, r in radio_states.items()
        if uuids(r.get("radio_config"))
    }
    taken = {v.get("mac") for v in vif_states.values() if isinstance(v.get("mac"), str)}
    changes = []
    for uuid, vif in vif_states.items():
        config = uuids(vif.get("vif_config"))
        if vif.get("mode") != "ap" or not config:
            continue
        radio_config = next(
            (u for u, r in radio_configs.items() if config[0] in uuids(r.get("vif_configs"))), None
        )
        if radio_config not in by_config:
            continue
        state, radio = by_config[radio_config]
        if not isinstance(vif.get("mac"), str) and isinstance(radio.get("mac"), str):
            mac = next(
                (o + radio["mac"][2:] for o in FIRST_OCTETS if o + radio["mac"][2:] not in taken),
                None,
            )
            if mac:
                taken.add(mac)
                where = [["_uuid", "==", ["uuid", uuid]]]
                changes.append(
                    {"op": "update", "table": "Wifi_VIF_State", "where": where, "row": {"mac": mac}}
                )
        if uuid not in uuids(radio.get("vif_states")):
            changes.append(
                {
                    "op": "mutate",
                    "table": "Wifi_Radio_State",
                    "where": [["_uuid", "==", ["uuid", state]]],
                    "mutations": [["vif_states", "insert", ["set", [["uuid", uuid]]]]],
                }
            )
    if changes:
        await db_transact(db, changes)
    return len(changes)


REPLY_CUT_PORT = 6661  # the reply cut's end, which the pod dials in place of the agent


class ReplyCut:
    """Between the pod's database and its agent: every message passed on, but the agent's first
    transaction that `match` picks meets a fault, as when the pod's management link fails at
    that moment:
    - refuse=False: the database commits it, its reply is dropped and both ends closed (spec 5:
      the outcome unknown, INDETERMINATE); the pod dials again through it;
    - refuse=True: it never reaches the database, and the agent gets an error result for it on
      the same connection (the write did not happen, the link stays up).
    After that fault every message is passed on as before."""

    def __init__(self, match, refuse=False):
        self.match, self.refuse, self.done, self.server = match, refuse, False, None

    async def start(self):
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", REPLY_CUT_PORT)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def serve(self, pod_reader, pod_writer):
        try:
            agent_reader, agent_writer = await asyncio.open_connection("127.0.0.1", AGENT_PORT)
        except OSError:
            pod_writer.close()
            return
        awaited = set()  # the ids of the matched transactions, whose replies are cut

        def from_agent(message):  # "pass", "drop" or "cut"
            if self.done or message.get("method") != "transact" or not self.match(message):
                return "pass"
            if not self.refuse:
                awaited.add(json.dumps(message.get("id")))
                return "pass"
            self.done = True
            error = {"error": "box: refused", "details": "the reply cut refused it"}
            reply = {"id": message.get("id"), "result": [error], "error": None}
            agent_writer.write(json.dumps(reply).encode())
            return "drop"

        def from_pod(message):
            if json.dumps(message.get("id")) in awaited and "method" not in message:
                self.done = True
                return "cut"
            return "pass"

        async def pump(reader, writer, verdict):
            decoder, text, buffer = json.JSONDecoder(), codecs.getincrementaldecoder("utf-8")(), ""
            try:
                while data := await reader.read(65536):
                    buffer += text.decode(data)
                    while buffer.strip():
                        try:
                            message, end = decoder.raw_decode(buffer.lstrip())
                        except ValueError:
                            break  # the rest of the message is still to come
                        buffer = buffer.lstrip()[end:]
                        outcome = verdict(message)
                        if outcome == "cut":
                            return
                        if outcome == "pass":
                            writer.write(json.dumps(message).encode())
                    await writer.drain()
                    await agent_writer.drain()  # a refusal written back to the agent
            except (ConnectionError, OSError):
                pass

        pumps = [
            asyncio.ensure_future(pump(agent_reader, pod_writer, from_agent)),
            asyncio.ensure_future(pump(pod_reader, agent_writer, from_pod)),
        ]
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pumps:
            task.cancel()
        for writer in (pod_writer, agent_writer):
            writer.close()


def writes_registrar_ssid(message):
    """The agent's write of the registrar's SSID (an M2's configuration)."""
    return REGISTRAR_SSID in json.dumps(message)


def deletes_steering_client(message):
    """The agent's close of a steering window: a delete of a Band_Steering_Clients row."""
    return any(
        isinstance(op, dict)
        and op.get("op") == "delete"
        and op.get("table") == "Band_Steering_Clients"
        for op in (message.get("params") or [])[1:]
    )


REPLY_CUTS = {  # the faults a box can put between the pod and its agent
    "lost-write": lambda: ReplyCut(writes_registrar_ssid),
    "refused-close": lambda: ReplyCut(deletes_steering_client, refuse=True),
}


async def dial(db, endpoint, *, connect=True):
    """Make the pod's database connect to the agent, as a pod does after its redirect; or,
    with connect=False, drop that connection (the pod's management link cut)."""
    action = "add-remote" if connect else "remove-remote"

    def command():
        error, stream = ovs.stream.Stream.open_block(
            ovs.stream.Stream.open("unix:" + str(db.directory / "control.sock")), 5000
        )
        if error:
            raise RuntimeError("the pod's database has no control socket")
        rpc = ovs.jsonrpc.Connection(stream)
        try:
            error, reply = rpc.transact_block(
                ovs.jsonrpc.Message.create_request("ovsdb-server/" + action, [endpoint])
            )
            if error or reply.error is not None:
                raise RuntimeError("the pod's database did not take the remote")
        finally:
            rpc.close()

    await asyncio.to_thread(command)


async def wait_for_port(port, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError(f"nothing listens on port {port}")


def agent_command(agent, config_path, binary=None):
    if agent == "c":
        path = Path(binary) if binary else ROOT / "c/build/emosa-agent-c"
        return [str(path), str(config_path), "--profiles", str(ROOT / "src/emosa/profiles")]
    return [sys.executable, "-m", "emosa.agent.pod", str(config_path)]


class Controller:
    """The controller's end of the veth: the messages the agent sends, and ours to it."""

    def __init__(self, interface, agent_al):
        self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
        self.socket.bind((interface, ETHERTYPE))
        self.socket.setblocking(False)
        self.reassembler = Reassembler()
        self.messages, self.arrived = [], []
        self.last_sent = time.monotonic()
        self.agent = bytes.fromhex(agent_al.replace(":", ""))
        self.al = bytes.fromhex(CONTROLLER_AL.replace(":", ""))
        self.mid = 0x4000

    def poll(self):
        while True:
            try:
                frame = self.socket.recv(65535)
            except BlockingIOError:
                return
            try:
                message = self.reassembler.feed(frame, ingress=CONTROLLER_INTERFACE)
            except Exception:  # a frame the reference refuses is not a message
                continue
            if message is not None:
                self.messages.append(message)
                self.arrived.append(time.monotonic())

    def when(self, message):
        """When the controller's side received this message (monotonic seconds)."""
        return self.arrived[next(i for i, m in enumerate(self.messages) if m is message)]

    def latest(self, message_type):
        found = [m for m in self.messages if m.message_type == message_type]
        return found[-1] if found else None

    def send(self, message_type, tlvs, mid=None):
        if mid is None:
            self.mid = (self.mid + 1) & 0xFFFF
            mid = self.mid
        for frame in fragment_message(self.agent, self.al, message_type, mid, tuple(tlvs)):
            self.socket.send(frame)
        self.last_sent = time.monotonic()
        return mid

    def send_frame(self, frame):
        """Raw bytes on the controller's end of the link, as any host on the LAN may."""
        self.socket.send(frame)

    def reply(self, message_type, mid):
        """The agent's message of this type for our request mid (a response or an ACK)."""
        return next(
            (m for m in self.messages if m.message_type == message_type and m.mid == mid), None
        )

    def close(self):
        self.socket.close()


TELEMETRY = {  # the pod's statistics through a broker in the box (spec 3.6)
    "mode": "mqtt",
    "broker": "127.0.0.1",
    "port": 1883,
    "subscribe": "127.0.0.1:1883",
    "reporting_interval": 10,
    "survey": True,
}
FOREIGN_BROKER = [["broker", "cloud.example.net"], ["port", "443"], ["topics", "operator/stats"]]


class Box:
    """The agent, its pod and its controller, started in the current network namespace.
    With telemetry, a broker (mosquitto) takes the pod's statistics; `foreign_broker`
    starts the pod with another manager's broker in its MQTT settings."""

    def __init__(
        self,
        agent,
        directory,
        *,
        binary=None,
        telemetry=False,
        foreign_broker=False,
        backhaul=False,
        bootstrap=False,
        multi_bss=False,
        uplink=None,
        topology_query_window=None,
        netns=False,
        kept_target=None,
        profile=None,
        m2_modes=None,
        wired=False,
        cold=False,
        reply_cut=False,
        underlay=None,
        gre_child=False,
    ):
        self.agent, self.directory, self.binary = agent, directory, binary
        # underlay (with a gre-parent profile): the pod's 169.254.N.0/24 as a GRE parent (spec
        # 8.6), given to the agent as the fleet registry would; its parent AP takes the .1
        self.underlay = underlay
        self.topology_query_window = topology_query_window
        # netns: the agent's interface in a namespace of its own (EMOSA_NETNS), as on a
        # gateway whose EasyMesh controller is local
        self.netns, self.holder, self.netns_path = netns, None, None
        self.telemetry, self.foreign_broker, self.backhaul = telemetry, foreign_broker, backhaul
        # multi_bss: the controller answers each M1 with an M2 set, a fronthaul and a backhaul
        # BSS (the registrar's backhaul mode); uplink: the agent's uplink setting instead of
        # UPLINK (with backhaul)
        self.multi_bss, self.uplink = multi_bss, uplink
        # kept_target (with backhaul): the upstream a controller's move left on an earlier
        # start, kept in the agent's state for this one
        self.kept_target = kept_target
        # m2_modes: the set the controller sends instead (one registrar mode per BSS);
        # profile: the agent's pod profile instead of the fleet's default
        self.m2_modes = m2_modes or (("configure", "backhaul") if multi_bss else ("configure",))
        # radio_m2_modes: another set for the radio of this RUID ("02:00:00:00:05:00"), as a
        # controller configures each band (spec 9)
        self.radio_m2_modes = {}
        self.profile = profile
        # wired: a wired pod, cm using its Ethernet port eth1 as the uplink; the agent's
        # uplink mode ethernet (spec 8.4)
        self.wired = wired
        # cold: the pod's fronthaul VIF left out, as on a pod whose bootstrap made its radio
        # only (the Pis'): the agent creates it from the M2 (spec 3.2), and the pod's managers
        # give it a BSSID on the radio
        self.cold = cold
        self.drop = (COLD_VIF,) if cold else ()
        # reply_cut: the pod dials the agent through a ReplyCut, one of REPLY_CUTS (lost-write:
        # the reply to the agent's first write of the registrar's SSID lost; refused-close: the
        # agent's first steering close refused on the same connection)
        self.reply_cut = REPLY_CUTS[reply_cut]() if reply_cut else None
        # bootstrap (with backhaul): the pod on its GTP bootstrap, the agent awaiting a steer
        self.bootstrap = bootstrap
        self.pod_rows = BOOTSTRAP_ROWS if bootstrap else (MULTI_AP_ROWS if backhaul else POD_ROWS)
        # gre_child (with backhaul): the recorded pod as it is, its 5 GHz station on its GRE uplink,
        # with the agent's uplink setting: a GRE child of the Pis' shape (spec 8.6, 9)
        if gre_child:
            self.pod_rows = POD_ROWS
        self.process = self.db = self.controller = self.log = self.broker = None
        self.interface = AGENT_INTERFACE  # the agent's 1905 interface (a fleet names its own)

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        network(self.interface)
        tables, operations = recorded_pod(self.pod_rows, drop=self.drop)
        self.serial = next(iter(tables["AWLAN_Node"].values()))["serial_number"]
        if self.foreign_broker:
            node = next(o for o in operations if o["table"] == "AWLAN_Node")
            node["row"]["mqtt_settings"] = ["map", FOREIGN_BROKER]
        if self.telemetry:
            # anonymous, local, and as the namespace's own root: no privileges to drop
            broker_config = self.directory / "broker.conf"
            broker_config.write_text("listener 1883 127.0.0.1\nallow_anonymous true\nuser root\n")
            self.broker = subprocess.Popen(
                ["mosquitto", "-c", str(broker_config)],
                stdout=(self.directory / "broker.log").open("w"),
                stderr=subprocess.STDOUT,
            )
            await wait_for_port(1883)
        self.db = (
            SimDatabase()
        )  # its own short directory: Unix socket paths are limited to 108 bytes
        await self.db.start()
        session = OvsSession(self.db.endpoint)
        try:
            await session.transact(operations)
            if self.wired:
                await session.transact(
                    [
                        {
                            "op": "insert",
                            "table": "Connection_Manager_Uplink",
                            "row": {
                                "if_name": "eth1",
                                "if_type": "eth",
                                "is_used": True,
                                "has_L2": True,
                                "has_L3": True,
                            },
                        }
                    ]
                )
        finally:
            await session.close()
        entry = {
            "pod_id": self.serial,
            "port": AGENT_PORT,
            "interface": self.interface,
            "al_mac": derive_al(self.serial, [CONTROLLER_AL]),
        }
        self.al_mac = entry["al_mac"]
        state_root = self.directory / "state"
        # a run root, as on a gateway (a RAM disk there): the status in the run directory
        self.run_root = self.directory / "run"
        fleet = {
            "controller_al": CONTROLLER_AL,
            "state_root": str(state_root),
            "run_root": str(self.run_root),
        }
        if self.telemetry:
            fleet["telemetry"] = TELEMETRY
        if self.multi_bss:
            fleet["multi_bss"] = True
        if self.profile:
            fleet["profile"] = self.profile
        if self.underlay:
            # the fleet's own GTP underlay and this pod's allocated underlay, as the registry
            # would hand them to the agent (spec 8.6)
            fleet["gtp_underlay"] = "169.254.1.0/24"
            entry["underlay"] = self.underlay
        if self.wired:
            fleet["uplink"] = {"mode": "ethernet"}
        if self.topology_query_window:
            fleet["topology_query_window"] = self.topology_query_window
        if self.backhaul:  # option 1, its credentials from the agent's own configuration
            fleet["uplink"] = self.uplink or UPLINK
            secrets = state_root / self.serial / "secrets"
            secrets.mkdir(parents=True, mode=0o700)
            (secrets / "backhaul").write_text("recorded-psk-replaced")
            (secrets / "backhaul").chmod(0o600)
            if self.kept_target:
                kept = state_root / self.serial / "uplink"
                kept.mkdir(parents=True, mode=0o700)
                configured = (self.uplink or UPLINK)["bssid"]
                (kept / "target.json").write_text(
                    json.dumps({"configured": configured, "target": self.kept_target, "moves": 1})
                )
        config = agent_config(entry, fleet)
        # the agent's interface carries its AL MAC, as the labs' macvlan per agent does
        subprocess.run(["ip", "link", "set", self.interface, "address", self.al_mac], check=True)
        if self.netns:
            self.holder = subprocess.Popen(["unshare", "--net", "sleep", "infinity"])
            own = os.readlink("/proc/self/ns/net")
            self.netns_path = f"/proc/{self.holder.pid}/ns/net"
            for _ in range(50):
                if os.path.exists(self.netns_path) and os.readlink(self.netns_path) != own:
                    break
                await asyncio.sleep(0.1)
            subprocess.run(
                ["ip", "link", "set", self.interface, "netns", str(self.holder.pid)], check=True
            )
            for command in (
                ["ip", "link", "set", "lo", "up"],
                ["ip", "link", "set", self.interface, "up"],
            ):
                subprocess.run(["nsenter", f"--net={self.netns_path}", *command], check=True)
        state_root.mkdir(mode=0o700, exist_ok=True)  # the fleet's state_root
        self.status_path = state_root / self.serial / "status.json"
        self.controller = Controller(CONTROLLER_INTERFACE, self.al_mac)
        self.env = {
            **os.environ,
            "EMOSA_SCHEMAS": str(ROOT / "schemas"),
            "EMOSA_PROFILES": str(ROOT / "src/emosa/profiles"),
            **({"EMOSA_NETNS": self.netns_path} if self.netns else {}),
        }
        if self.reply_cut:
            await self.reply_cut.start()
        await self.start(config, fleet)
        return self

    async def start(self, config, fleet):
        """The pod's agent started with this configuration, and the pod dialing it."""
        config_path = self.directory / "agent.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        self.log = (self.directory / "agent.log").open("w")
        self.command = agent_command(self.agent, config_path, self.binary)
        self.process = subprocess.Popen(
            self.command, stdout=self.log, stderr=self.log, env=self.env
        )
        await asyncio.sleep(1)
        port = REPLY_CUT_PORT if self.reply_cut else AGENT_PORT
        await dial(self.db, f"tcp:127.0.0.1:{port}")

    def restart_agent(self):
        """The agent started again with its configuration and state directory."""
        self.process = subprocess.Popen(
            self.command, stdout=self.log, stderr=self.log, env=self.env
        )

    async def __aexit__(self, *exc):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.log:
            self.log.close()
        if self.controller:
            self.controller.close()
        if self.db:
            await self.db.close()
        if self.reply_cut:
            await self.reply_cut.close()
        if self.broker and self.broker.poll() is None:
            self.broker.terminate()
            self.broker.wait(5)
        if self.holder and self.holder.poll() is None:
            self.holder.terminate()  # its namespace, and the agent's end of the veth, go with it
            self.holder.wait(5)

    def status(self):
        try:
            return json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            return None

    async def until(self, predicate, seconds=60):
        """Poll the controller's side until predicate() holds or the agent exits."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and self.process.poll() is None:
            self.controller.poll()
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.2)
        return None

    async def rows(self, table):
        """The pod's rows of one table, every column, decoded."""
        return await table_rows(self.db, table)

    def counts(self):
        return ((self.status() or {}).get("session") or {}).get("counts") or {}

    def sent(self, message_type):
        """The agent's messages of this type so far, in order."""
        self.controller.poll()
        return [m for m in self.controller.messages if m.message_type == message_type]

    async def ask(self, message_type, tlvs, expected, seconds=15):
        """A request to the agent, and its reply of the expected type with the request's MID."""
        mid = self.controller.send(message_type, tlvs)
        return await self.until(lambda: self.controller.reply(expected, mid), seconds=seconds)

    async def transact(self, operations):
        return await db_transact(self.db, operations)

    async def vif(self, table, if_name):
        """(uuid, row) of the pod's VIF by name in Wifi_VIF_Config or Wifi_VIF_State."""
        for uuid, row in (await self.rows(table)).items():
            if row.get("if_name") == if_name:
                return uuid, row
        return None, None

    async def client_join(self, mac, if_name="home-ap-24"):
        """A station associates with the pod's BSS: owm's client row, on the VIF's State."""
        uuid, _ = await self.vif("Wifi_VIF_State", if_name)
        await self.transact(
            [
                {
                    "op": "insert",
                    "table": "Wifi_Associated_Clients",
                    "uuid-name": "joined",
                    "row": {"mac": mac, "state": "active"},
                },
                {
                    "op": "mutate",
                    "table": "Wifi_VIF_State",
                    "where": [["_uuid", "==", ["uuid", uuid]]],
                    "mutations": [
                        ["associated_clients", "insert", ["set", [["named-uuid", "joined"]]]]
                    ],
                },
            ]
        )

    async def client_leave(self, mac, if_name="home-ap-24"):
        uuid, _ = await self.vif("Wifi_VIF_State", if_name)
        client = next(
            u
            for u, r in (await self.rows("Wifi_Associated_Clients")).items()
            if r.get("mac") == mac
        )
        await self.transact(
            [
                {
                    "op": "mutate",
                    "table": "Wifi_VIF_State",
                    "where": [["_uuid", "==", ["uuid", uuid]]],
                    "mutations": [["associated_clients", "delete", ["set", [["uuid", client]]]]],
                },
                {
                    "op": "delete",
                    "table": "Wifi_Associated_Clients",
                    "where": [["_uuid", "==", ["uuid", client]]],
                },
            ]
        )

    async def new_pod_database(self, replace=None):
        """The pod's OpenSync started again (its container with its extender, say): a new
        database from the template, with new row UUIDs (and replace's changes: other
        radios), dialing the agent again."""
        old = self.db
        await old.close()
        tables, operations = recorded_pod(self.pod_rows, replace, self.drop)
        self.db = SimDatabase()
        await self.db.start()
        await self.transact(operations)
        await dial(self.db, f"tcp:127.0.0.1:{AGENT_PORT}")

    def result(self, **extra):
        return {
            "agent": self.agent,
            "serial": self.serial,
            "al_mac": self.al_mac,
            "exit": self.process.poll(),
            "messages": sorted({f"0x{m.message_type:04x}" for m in self.controller.messages}),
            "session": (self.status() or {}).get("session", {})
            and self.status()["session"].get("state"),
            **extra,
        }


async def apply_configuration(db):
    """The pod's managers: each VIF's State takes its Config's applied columns (a new
    VIF gets its State row), as OpenSync's wm does once the radio has applied them."""
    session = OvsSession(db.endpoint)
    try:
        snapshot = await session.snapshot()
        tables, schema = snapshot["tables"], snapshot["schema"]
        states = {
            schema.decode("Wifi_VIF_State", "if_name", row["if_name"]): uuid
            for uuid, row in tables["Wifi_VIF_State"].items()
        }
        changes = []
        for uuid, row in tables["Wifi_VIF_Config"].items():
            if schema.decode("Wifi_VIF_Config", "mode", row.get("mode", "")) != "ap":
                continue  # a station's State is cm's and its supplicant's, not wm's
            name = schema.decode("Wifi_VIF_Config", "if_name", row["if_name"])
            applied = {c: row[c] for c in [*APPLY_COLUMNS, "multi_ap"] if c in row}
            if name in states:
                where = [["_uuid", "==", ["uuid", states[name]]]]
                changes.append(
                    {"op": "update", "table": "Wifi_VIF_State", "where": where, "row": applied}
                )
            else:
                applied |= {"if_name": row["if_name"], "vif_config": ["uuid", uuid]}
                changes.append({"op": "insert", "table": "Wifi_VIF_State", "row": applied})
        await session.transact(changes)
    finally:
        await session.close()


async def boot(box):
    """The agent takes the pod's connection and searches for its controller. Its status is
    in its run directory, the state directory's status.json a link to it (spec §6)."""
    searched = await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH))
    await box.until(lambda: box.status() is not None)
    target = box.run_root / box.serial / "status.json"
    linked = (
        box.status_path.is_symlink()
        and os.readlink(box.status_path) == str(target)
        and target.is_file()
    )
    return box.result(passed=bool(searched) and linked, status_linked=linked)


async def onboard(box):
    """The controller answers: the Response, then an M2 from hostap's registrar for the
    agent's own M1. The agent writes the credentials to the pod, the pod's manager
    applies them, and the agent sees them applied."""
    state = await onboarded(box)
    if "failed" in state:
        return box.result(passed=False, failed=state["failed"])
    return box.result(passed=state["applied"], wrote=True, operations=state["operations"])


async def answer_m1(box, ruid, wsc):
    """The registrar's answer to one M1: an M2, or with multi_bss an M2 set (one per BSS,
    each from its own registrar session: m2_session distinct)."""
    modes = box.radio_m2_modes.get(ruid.hex(":"), box.m2_modes)
    m2s = [await asyncio.to_thread(registrar_reply, REGISTRAR, wsc, mode) for mode in modes]
    box.controller.send(AUTOCONFIG_WSC, (Tlv(0x82, ruid), *(Tlv(0x11, m2) for m2 in m2s)))


async def onboarded(box):
    """onboard's steps, which other scenarios start from: the agent's radio (its RUID) and
    whether the credentials were applied, or what failed. As a controller does, every
    Search gets a Response and every M1 an M2 until the credentials are applied (a slow
    agent may end a session before its answer arrives and start another)."""
    if not await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH)):
        return {"failed": "no search"}
    deadline, searches, m1s, ruid, wrote = time.monotonic() + 90, 0, 0, None, False
    pending = False  # an M2 sent whose credentials the pod's managers have not applied yet

    def applied():
        operations = (box.status() or {}).get("operations") or []
        return any(
            "APPLIED" in str(op.get("state", "")) for op in operations if isinstance(op, dict)
        )

    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        # an EasyMesh 6.1 Response: registrar, 2.4 GHz, the controller service, Profile 1,
        # and Controller Capability with KiB/MiB counters and the Early AP Capability Report
        for search in found[searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)  # AP Radio Basic Caps
            await answer_m1(box, ruid, wsc)
            pending = True
        m1s = len(found)
        if pending:
            rows = await box.rows("Wifi_VIF_Config")
            if any(r.get("ssid") == REGISTRAR_SSID for r in rows.values()):
                wrote, pending = True, False
                await apply_configuration(box.db)  # the pod's managers apply it, once
                if box.cold:
                    await attach_vifs(box.db)
        if wrote and applied():
            break
        await asyncio.sleep(0.3)
    if ruid is None:
        return {"failed": "no M1"}
    if not wrote:
        return {"failed": "the credentials never reached the pod"}
    return {
        "ruid": ruid,
        "applied": applied(),
        "operations": (box.status() or {}).get("operations"),
    }


def operational_bssid(topology):
    """The first BSSID in a Topology Response's AP Operational BSS TLV."""
    value = next((t.value for t in topology.tlvs if t.kind == 0x83), b"")
    # radios(1), then per radio: RUID(6), BSSes(1), per BSS: BSSID(6), SSID length(1), SSID
    if len(value) >= 14 and value[0] and value[7]:
        return value[8:14]
    return None


async def answers(box):
    """After onboarding, frames not for the agent (to the broadcast address, a runt) are
    ignored, and the controller's requests: each gets its response (or its 1905
    ACK) with the request's message ID, except the two that report the pod's statistics
    (spec 3.8): with no telemetry in the box there are none, and the agent withholds the
    Link Metric and AP Metrics Responses and records why instead of inventing values."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "credentials not applied"))
    ruid, stranger = state["ruid"], bytes.fromhex("02aabbccddee")

    async def ask(message_type, tlvs, expected, seconds=15):
        mid = box.controller.send(message_type, tlvs)
        return await box.until(lambda: box.controller.reply(expected, mid), seconds=seconds)

    # frames not for the agent: a Topology Query to the broadcast address (not the 1905
    # multicast nor the agent), and a runt (shorter than a CMDU header); both ignored
    box.controller.mid = (box.controller.mid + 1) & 0xFFFF
    broadcast_mid = box.controller.mid
    for frame in fragment_message(
        b"\xff" * 6, box.controller.al, 0x0002, broadcast_mid, (Tlv(0xB3, b"\x01"),)
    ):
        box.controller.send_frame(frame)
    runt = box.controller.agent + box.controller.al + b"\x89\x3a\x00\x00\x00\x02\x00"
    box.controller.send_frame(runt)
    await asyncio.sleep(2)
    ignored = box.controller.reply(0x0003, broadcast_mid) is None and box.process.poll() is None
    topology = await ask(0x0002, (Tlv(0xB3, b"\x01"),), 0x0003)
    bssid = operational_bssid(topology) if topology else None
    requests = {
        "topology": None,  # asked above; its BSSID names the BSS for the queries below
        "ap_capability": (0x8001, (), 0x8002),
        "channel_preference": (0x8004, (), 0x8005),
        "client_capability": (0x8009, (Tlv(0x90, (bssid or bytes(6)) + stranger),), 0x800A),
        "backhaul_sta_capability": (0x8027, (), 0x8028),
        "unassociated_sta_metrics": (0x800F, (Tlv(0x97, bytes((81, 1, 6, 1)) + stranger),), 0x8000),
        "policy_config": (0x8003, (Tlv(0x8A, b"\x00\x01" + ruid + bytes(4)),), 0x8000),
    }
    answered = {"topology": "0x0003" if topology else None}
    for name, request in requests.items():
        if request is None:
            continue
        message_type, tlvs, expected = request
        reply = await ask(message_type, tlvs, expected)
        answered[name] = f"0x{expected:04x}" if reply else None
    # withheld without statistics: no response, and the reason in the session's counts
    withheld = {
        "link_metric": (
            (0x0005, (Tlv(0x08, b"\x00\x02"),), 0x0006),
            "neighbor_measurement_unavailable",
        ),
        "ap_metrics": (
            (0x800B, (Tlv(0x93, b"\x01" + (bssid or bytes(6))),), 0x800C),
            "ap_measurements_unavailable",
        ),
    }
    abstained = {}
    for name, ((message_type, tlvs, expected), reason) in withheld.items():
        reply = await ask(message_type, tlvs, expected, seconds=5)
        counts = ((box.status() or {}).get("session") or {}).get("counts") or {}
        abstained[name] = reason if reply is None and counts.get(reason) else None
    session = (box.status() or {}).get("session") or {}
    # where the agent records its replies, the same in both implementations
    recorded = {
        "policy_receipt_ack_sent": (session.get("counts") or {}).get("policy_receipt_ack_sent"),
        "topology_response_sent": ((session.get("reports") or {}).get("counts") or {}).get(
            "topology_response_sent"
        ),
        "topology_response_in_session_counts": "topology_response_sent"
        in (session.get("counts") or {}),
    }
    return box.result(
        passed=ignored and bool(bssid) and all(answered.values()) and all(abstained.values()),
        ignored=ignored,
        bssid=bssid.hex(":") if bssid else None,
        answered=answered,
        abstained=abstained,
        recorded=recorded,
    )


async def refuse(box):
    """An EasyMesh 6.1 controller whose Response lacks the Controller Capability TLV:
    the agent refuses it (incompatible, spec 2.5) and sends no M1."""
    search = await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH))
    if not search:
        return box.result(passed=False, failed="no search")
    response = (Tlv(0x0F, b"\0"), Tlv(0x10, b"\0"), Tlv(0x80, b"\x01\0"), Tlv(0xB3, b"\x01"))
    box.controller.send(AUTOCONFIG_RESPONSE, response, mid=search.mid)

    def refused():
        session = (box.status() or {}).get("session") or {}
        return session.get("state") == "incompatible" and session

    session = await box.until(refused, seconds=20)
    await asyncio.sleep(2)  # an M1 would follow the Response at once
    box.controller.poll()
    m1 = box.controller.latest(AUTOCONFIG_WSC)
    issues = (session or {}).get("admission_issues")
    return box.result(passed=bool(session) and m1 is None, admission_issues=issues)


RESPONSE_61 = (
    Tlv(0x0F, b"\0"),
    Tlv(0x10, b"\0"),
    Tlv(0x80, b"\x01\0"),
    Tlv(0xB3, b"\x01"),
    Tlv(0xDD, b"\xc0"),
)  # as onboarded() sends it
RENEW = 0x000A
TOPOLOGY_QUERY, TOPOLOGY_RESPONSE, TOPOLOGY_NOTIFICATION = 0x0002, 0x0003, 0x0001
TOPOLOGY_DISCOVERY = 0x0000
EARLY_REPORT, ACK = 0x8043, 0x8000
NEW_STATION = "02:00:00:00:77:01"


async def next_message(box, message_type, after, seconds=60):
    """The first message of this type beyond the first `after` ones."""
    return await box.until(
        lambda: (lambda found: found[after] if len(found) > after else None)(
            box.sent(message_type)
        ),
        seconds=seconds,
    )


async def provision(box, searches=0, m1s=0, *, seconds=90):
    """As a controller does until the agent is provisioning again: a Response to every Search
    beyond the first `searches`, a registrar's M2 to every M1 beyond the first `m1s`, and the
    pod's managers applying the credentials once written. A slow agent can end a session
    before its answer arrives and start another; it gets answered too. True once
    provisioning after an M2."""
    deadline = time.monotonic() + seconds
    answered_searches, answered_m1s, m2_sent, pending = searches, m1s, False, False

    async def written():
        rows = await box.rows("Wifi_VIF_Config")
        return any(r.get("ssid") == REGISTRAR_SSID for r in rows.values())

    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        for search in found[answered_searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        answered_searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[answered_m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            await answer_m1(box, ruid, wsc)
            m2_sent = pending = True
        answered_m1s = len(found)
        if pending and await written():
            pending = False
            await apply_configuration(box.db)  # the pod's managers apply it, once
        session = (box.status() or {}).get("session") or {}
        if m2_sent and not pending and session.get("state") == "provisioning":
            return True
        await asyncio.sleep(0.3)
    return False


async def early_report(box):
    """With EasyMesh 6.1 the Early AP Capability Report goes before M1 and, without an Ack,
    three times in all, 250 ms apart, each with a new MID (spec 2.5); acknowledged, once."""
    search = await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH))
    if not search:
        return box.result(passed=False, failed="no search")
    box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
    if not await next_message(box, AUTOCONFIG_WSC, 0):
        return box.result(passed=False, failed="no M1")
    await asyncio.sleep(2.5)  # no M2 and no Ack: the retries run out
    unacknowledged = box.sent(EARLY_REPORT)
    mids = [m.mid for m in unacknowledged]
    # a Renew starts again; this time the controller acknowledges the first transmission
    searches, before = len(box.sent(AUTOCONFIG_SEARCH)), len(unacknowledged)
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    search = await next_message(box, AUTOCONFIG_SEARCH, searches)
    if not search:
        return box.result(passed=False, failed="no search after the Renew")
    box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
    first = await next_message(box, EARLY_REPORT, before, seconds=20)
    if first:
        box.controller.send(ACK, (), mid=first.mid)
    await asyncio.sleep(2.5)
    acknowledged = len(box.sent(EARLY_REPORT)) - before
    return box.result(
        # three at most, each a new MID; a loop busy at start may get fewer into its 1 s
        passed=2 <= len(mids) <= 3 and len(set(mids)) == len(mids) and acknowledged == 1,
        unacknowledged_transmissions=len(mids),
        distinct_mids=len(set(mids)),
        acknowledged_transmissions=acknowledged,
    )


async def renew(box):
    """A Renew from the controller: the agent starts again at once (a Search, then a fresh
    M1), takes the new M2 and is provisioning again (spec 2.4, 2.5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    sent_at = time.monotonic()
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    search = await next_message(box, AUTOCONFIG_SEARCH, searches, seconds=10)
    again = await provision(box, searches, m1s) if search else False
    return box.result(
        passed=bool(search) and again,
        search_after_s=round(box.controller.when(search) - sent_at, 1) if search else None,
        provisioning_again=again,
        operations=[op.get("state") for op in (box.status() or {}).get("operations") or []],
    )


async def no_m2(box):
    """Suite finding: an M1 the controller never answers (it restarted in between). After
    30 s without an M2 the agent starts onboarding again (spec 2.5). The box answers every
    Search, never an M1, and times the Search that follows an M1 by 25 s or more."""
    deadline, answered, waited = time.monotonic() + 120, 0, None
    while time.monotonic() < deadline and box.process.poll() is None and waited is None:
        searches, m1s = box.sent(AUTOCONFIG_SEARCH), box.sent(AUTOCONFIG_WSC)
        for search in searches[answered:]:
            at = box.controller.when(search)
            before = [box.controller.when(m) for m in m1s if box.controller.when(m) < at]
            if before and at - before[-1] >= 25:
                waited = at - before[-1]  # the no-M2 rule, not an earlier session's end
                break
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        answered = len(searches)
        await asyncio.sleep(0.2)
    return box.result(
        passed=waited is not None and 29 <= waited <= 45,
        search_after_m1_s=round(waited, 1) if waited else None,
        m1s=len(box.sent(AUTOCONFIG_WSC)),
    )


async def silent_controller(box):
    """Suite finding: a controller that stops talking, in any state. After 130 s without a
    message from it the agent starts onboarding again (spec 2.5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, quiet_since = len(box.sent(AUTOCONFIG_SEARCH)), box.controller.last_sent
    again = await next_message(box, AUTOCONFIG_SEARCH, searches, seconds=180)
    waited = box.controller.when(again) - quiet_since if again else None
    return box.result(
        passed=bool(again) and 128 <= waited <= 150,
        search_after_silence_s=round(waited, 1) if waited else None,
    )


async def forgotten_agent(box):
    """Suite finding: RDK's controller restarted and forgot the agent, yet kept sending it
    its other queries, so the silence rule never fired. With topology_query_window (20 s
    here), a provisioned agent that gets no Topology Query for that long onboards again
    (spec 2.5). The box queries as RDK's controller does, every 5 s: for 30 s a Topology
    Query with the others (the agent stays), then the others only (it searches)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))

    def others():
        box.controller.send(0x8001, ())  # AP Capability Query
        box.controller.send(0x0005, (Tlv(0x08, b"\x00\x02"),))  # Link Metric Query

    for _ in range(6):  # known: 30 s with a Topology Query every 5 s
        box.controller.send(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),))
        last_query = time.monotonic()
        others()
        await asyncio.sleep(5)
    stayed = len(box.sent(AUTOCONFIG_SEARCH)) == searches
    again, deadline = None, time.monotonic() + 60
    while again is None and time.monotonic() < deadline and box.process.poll() is None:
        others()  # forgotten: the other queries only
        again = await next_message(box, AUTOCONFIG_SEARCH, searches, seconds=5)
    waited = box.controller.when(again) - last_query if again else None
    provisioned = await provision(box, searches, m1s) if again else False
    return box.result(
        passed=stayed and bool(again) and 19 <= waited <= 30 and provisioned,
        stayed_while_queried=stayed,
        search_after_last_topology_query_s=round(waited, 1) if waited else None,
        provisioning_again=provisioned,
    )


async def agent_netns(box):
    """EMOSA_NETNS: the agent's interface in a network namespace of its own, as on a gateway
    whose EasyMesh controller runs alongside (RDK's took an agent whose AL MAC was the MAC of
    one of its own interfaces for its co-located agent, 5 October 2026): no interface of the
    agent's own namespace has the AL MAC, and the agent onboards through the other one."""
    links = subprocess.run(["ip", "-o", "link"], capture_output=True, text=True).stdout
    state = await onboarded(box)
    return box.result(
        passed=box.al_mac not in links and bool(state.get("applied")),
        al_mac_in_own_namespace=box.al_mac in links,
        onboarding=state,
    )


async def unserved_pod(box):
    """Suite finding: provisioned, but the pod serves none of the controller's BSSes (its
    configuration was lost, a write lost to an uplink move) and nothing is being written.
    After 60 s the agent asks for the configuration again: onboarding anew (spec 2.5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches = len(box.sent(AUTOCONFIG_SEARCH))
    lost_at = time.monotonic()
    # the pod's managers lost the fronthaul BSS: no BSS of the controller's is served
    uuid, _ = await box.vif("Wifi_VIF_State", "home-ap-24")
    radio = next(
        u for u, r in (await box.rows("Wifi_Radio_State")).items() if r.get("if_name") == "phy1"
    )
    await box.transact(
        [
            {
                "op": "mutate",
                "table": "Wifi_Radio_State",
                "where": [["_uuid", "==", ["uuid", radio]]],
                "mutations": [["vif_states", "delete", ["set", [["uuid", uuid]]]]],
            },
            {"op": "delete", "table": "Wifi_VIF_State", "where": [["_uuid", "==", ["uuid", uuid]]]},
        ]
    )
    again = await next_message(box, AUTOCONFIG_SEARCH, searches, seconds=120)
    waited = box.controller.when(again) - lost_at if again else None
    return box.result(
        passed=bool(again) and 58 <= waited <= 90,
        search_after_loss_s=round(waited, 1) if waited else None,
    )


RECREATED_RADIO = {
    "02:00:00:00:01:00": "02:00:00:00:6a:00",
    "82:00:00:00:01:00": "82:00:00:00:6a:00",
}


async def pod_recreated(box):
    """Finding (a), rdk-1004 7 October: the pod recreated in place (its container cloned and
    started again: a new database from the template, the same serial, and another AP radio
    from the lab's pool). The agent takes the new pod's connection and onboards it, its
    Search and M1 on the new radio."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.new_pod_database(RECREATED_RADIO)
    again = await provision(box, searches, m1s)
    rows = await box.rows("Wifi_VIF_Config")
    written = any(r.get("ssid") == REGISTRAR_SSID for r in rows.values())
    status = box.status() or {}
    radio = json.dumps(status.get("pod") or {})
    new_radio = "02:00:00:00:6a:00" in radio and "02:00:00:00:01:00" not in radio
    return box.result(
        passed=again and written and new_radio,
        provisioning_again=again,
        credentials_on_new_pod=written,
        new_radio_in_status=new_radio,
        ruid=(status.get("pod") or {}).get("ruid"),
    )


async def new_source(box):
    """Suite finding: the pod dropped with its extender and back with a fresh database (a new
    generation). The agent ends the session on the old source and onboards on the new one."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.new_pod_database()
    again = await provision(box, searches, m1s)
    rows = await box.rows("Wifi_VIF_Config")
    written = any(r.get("ssid") == REGISTRAR_SSID for r in rows.values())
    return box.result(
        passed=again and written, provisioning_again=again, credentials_on_new_database=written
    )


def association_events(box, after=0):
    """(station, BSSID, joined) of each Client Association Event the agent notified."""
    events = []
    for message in box.sent(TOPOLOGY_NOTIFICATION)[after:]:
        for tlv in message.tlvs:
            if tlv.kind == 0x92 and len(tlv.value) >= 13:
                events.append(
                    (tlv.value[:6].hex(":"), tlv.value[6:12].hex(":"), bool(tlv.value[12] & 0x80))
                )
    return events


def client_ages(topology):
    """{station: seconds since association} from a Topology Response's Associated Clients
    TLV (0x84): BSSes(1), per BSS: BSSID(6), clients(2), per client: MAC(6) and age(2)."""
    ages = {}
    value = next((t.value for t in topology.tlvs if t.kind == 0x84), b"")
    at, count = 1, value[0] if value else 0
    for _ in range(count):
        clients = int.from_bytes(value[at + 6 : at + 8], "big")
        at += 8
        for _ in range(clients):
            ages[value[at : at + 6].hex(":")] = int.from_bytes(value[at + 6 : at + 8], "big")
            at += 8
    return ages


async def clients(box):
    """A station joins and leaves the pod's BSS: a Topology Notification with a Client
    Association Event each time (spec 2.4); the Topology Response lists the station with its
    age as of that response (suite finding: ages taken as of each Topology Response)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)  # re-announcement done
    await asyncio.sleep(2)
    before = len(box.sent(TOPOLOGY_NOTIFICATION))
    await box.client_join(NEW_STATION)
    joined = await box.until(
        lambda: [e for e in association_events(box, before) if e[0] == NEW_STATION and e[2]],
        seconds=20,
    )
    first = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    await asyncio.sleep(4)
    second = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    ages = [client_ages(r).get(NEW_STATION) if r else None for r in (first, second)]
    before = len(box.sent(TOPOLOGY_NOTIFICATION))
    await box.client_leave(NEW_STATION)
    left = await box.until(
        lambda: [e for e in association_events(box, before) if e[0] == NEW_STATION and not e[2]],
        seconds=20,
    )
    aged = None not in ages and 3 <= ages[1] - ages[0] <= 8
    return box.result(
        passed=bool(joined) and aged and bool(left), joined=bool(joined), ages=ages, left=bool(left)
    )


async def reannounce(box):
    """Suite finding: a controller that restarted never learns the clients that joined before
    it did. Once provisioned, at the controller's next Topology Query the agent announces
    every current client again, once (spec 2.5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await asyncio.sleep(2)
    before = len(box.sent(TOPOLOGY_NOTIFICATION))
    await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    await asyncio.sleep(3)
    announced = sorted(e[0] for e in association_events(box, before) if e[2])
    before = len(box.sent(TOPOLOGY_NOTIFICATION))
    await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    await asyncio.sleep(3)
    again = sorted(e[0] for e in association_events(box, before) if e[2])
    current = ["02:00:00:00:0a:00", "02:00:00:00:10:00"]  # the recorded pod's fronthaul clients
    return box.result(
        passed=announced == current and again == [], announced=announced, announced_again=again
    )


def tlv_value(message, kind):
    return next((t.value for t in message.tlvs if t.kind == kind), None) if message else None


async def channel_selection(box):
    """Channel Selection Requests (spec 2.4, 3.4): one the pod can honour (its channel
    preferred) is accepted, code 0, without moving the radio; one it cannot (RDK's, with its
    channel ruled out) is declined, code 2. Each is followed by the Operating Channel Report."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ruid = state["ruid"]
    # from spec/conformance/control.json: channel-selection-accepted and -rdk-declined
    accepted = (Tlv(0x8B, ruid + bytes.fromhex("01510106e0")),)
    declined = (
        Tlv(
            0x8B,
            ruid
            + bytes.fromhex(
                "05510c01020304050708090a0b0c0d00510106105308010203040507"
                "080910530106e0540905060708090a0b0c0d10"
            ),
        ),
        Tlv(0x8D, ruid + b"\x00"),
    )
    codes, reports = {}, {}
    for name, tlvs in (("accepted", accepted), ("declined", declined)):
        reported = len(box.sent(0x8008))
        response = await box.ask(0x8006, tlvs, 0x8007)
        value = tlv_value(response, 0x8E)
        codes[name] = value[6] if value and len(value) >= 7 else None
        report = await next_message(box, 0x8008, reported, seconds=10)
        reports[name] = (tlv_value(report, 0x8F) or b"").hex() or None
    radio = next(
        r for r in (await box.rows("Wifi_Radio_State")).values() if r.get("if_name") == "phy1"
    )
    return box.result(
        passed=codes == {"accepted": 0, "declined": 2}
        and all(reports.values())
        and radio.get("channel") == 6,
        codes=codes,
        operating_channel_reports=reports,
        channel=radio.get("channel"),
    )


async def channel_scan(box):
    """A Channel Scan Request (spec 2.4, 3.4): acknowledged, then a Channel Scan Report with a
    timestamp and one result per requested channel, status 1 (scan not supported)."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed="not onboarded")
    request = Tlv(0xA6, b"\x00\x01" + state["ruid"] + bytes.fromhex("0151020106"))
    reported = len(box.sent(0x801C))
    ack = await box.ask(0x801B, (request,), ACK)
    report = await next_message(box, 0x801C, reported, seconds=10)
    results = [t.value for t in report.tlvs if t.kind == 0xA7] if report else []
    statuses = sorted((r[7], r[8]) for r in results if len(r) >= 9)  # (channel, status)
    return box.result(
        passed=bool(ack) and statuses == [(1, 1), (6, 1)] and tlv_value(report, 0xA8) is not None,
        results=statuses,
    )


# The pod's statistics (spec 3.6, 3.8, 3.9): a broker in the box; the box publishes as the
# pod's qm would, built from the pinned schema (emosa_lab.conformance's report builders)

STATIONS = ("02:00:00:00:0a:00", "02:00:00:00:10:00")  # the recorded fronthaul's clients


def ovsdb_map(value):
    """An OVSDB map column as a dict, raw (["map", pairs]) or decoded."""
    if isinstance(value, list) and len(value) == 2 and value[0] == "map":
        return dict(value[1])
    return dict(value or {})


def telemetry_status(box):
    return (box.status() or {}).get("telemetry") or {}


def now_ms():
    return int(time.time() * 1000)


async def publish(box, payload_hex, *, retain=False):
    import paho.mqtt.publish as mqtt_publish

    topic = telemetry_status(box).get("topic") or f"emosa/stats/{box.serial}"
    await asyncio.to_thread(
        mqtt_publish.single,
        topic,
        bytes.fromhex(payload_hex),
        hostname="127.0.0.1",
        port=1883,
        retain=retain,
    )


def pod_statistics(at_ms):
    """A survey of the pod's channel and a client report of its two stations."""
    from emosa_lab.conformance import COUNTERS_FULL, client_report, survey_report

    return (
        survey_report(at_ms - 1500),
        client_report(
            at_ms - 1000,
            [(STATIONS[0], 40, 144.4, 72.2, COUNTERS_FULL), (STATIONS[1], 30, 65.0, 58.5, None)],
        ),
    )


async def telemetry_applied(box):
    def applied():
        operation = telemetry_status(box).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED" and operation

    return await box.until(applied, seconds=60)


async def telemetry(box):
    """The agent has its pod publish its statistics: the broker, the pod's topic and the
    reports in one guarded write, applied once the pod's database has it; the reports then
    reach the agent through the broker, and a repeated one is dropped (spec 3.6)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    applied = await telemetry_applied(box)
    node = next(iter((await box.rows("AWLAN_Node")).values()))
    settings = ovsdb_map(node.get("mqtt_settings"))
    reports = sorted(r.get("stats_type") for r in (await box.rows("Wifi_Stats_Config")).values())
    survey, clients = pod_statistics(now_ms())
    await publish(box, clients)
    accepted = await box.until(lambda: telemetry_status(box).get("reports_accepted"), seconds=20)
    await publish(box, clients)  # the same report again
    await asyncio.sleep(2)
    status = telemetry_status(box)
    stations = sorted(status.get("stations") or {})
    return box.result(
        passed=bool(applied)
        and settings.get("broker") == "127.0.0.1"
        and "client" in reports
        and accepted == 1
        and status.get("reports_accepted") == 1
        and (status.get("reports_rejected") or 0) >= 1
        and stations == sorted(STATIONS),
        mqtt_settings=settings,
        stats_types=reports,
        reports_accepted=status.get("reports_accepted"),
        reports_rejected=status.get("reports_rejected"),
        stations=stations,
    )


async def foreign_broker(box):
    """The pod already publishes to another manager's broker (the operator's cloud): the
    agent does not take it over; its write is refused and the pod's settings stay (spec 3.6)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")

    def settled():
        operation = telemetry_status(box).get("operation") or {}
        return operation.get("state") in {"REJECTED", "FAILED", "OWNERSHIP_CONFLICT"} and operation

    operation = await box.until(settled, seconds=60)
    node = next(iter((await box.rows("AWLAN_Node")).values()))
    settings = sorted(map(list, ovsdb_map(node.get("mqtt_settings")).items()))
    return box.result(
        passed=bool(operation) and settings == sorted(FOREIGN_BROKER),
        operation=(operation or {}).get("state"),
        reason=(operation or {}).get("reason"),
        mqtt_settings=settings,
    )


METRIC_POLICY = bytes.fromhex("000001020000000100023c78")  # Steering Policy, from metrics.json


async def metrics(box):
    """With the pod's statistics, AP metrics as a native agent reports them (spec 3.8): the
    Metric Reporting Policy kept; an AP Metrics Query answered with the AP Metrics TLV
    (channel utilization from the survey, the BSS's stations), the Associated STA Link
    Metrics and Traffic Stats of its stations; and unsolicited at the policy's interval."""
    state = await onboarded(box)
    if not state.get("applied") or not await telemetry_applied(box):
        return box.result(passed=False, failed="not onboarded or no telemetry")
    ruid = state["ruid"]
    bssid = bytes.fromhex("820000000100")

    async def keep_publishing():
        while True:
            for payload in pod_statistics(now_ms()):
                await publish(box, payload)
            await asyncio.sleep(5)

    publisher = asyncio.create_task(keep_publishing())
    try:
        await box.until(lambda: len(telemetry_status(box).get("stations") or {}) == 2, seconds=20)
        # every 5 s, link metrics and traffic stats for the radio (from metrics.json)
        policy = (
            Tlv(0x89, METRIC_POLICY[:3] + ruid + METRIC_POLICY[9:]),
            Tlv(0x8A, b"\x05\x01" + ruid + bytes.fromhex("78053cc0")),
        )
        acked = await box.ask(0x8003, policy, ACK)
        response = await box.ask(0x800B, (Tlv(0x93, b"\x01" + bssid),), 0x800C)
        ap = tlv_value(response, 0x94) or b""
        link = [t.value[:6].hex(":") for t in (response.tlvs if response else []) if t.kind == 0x96]
        traffic = [
            t.value[:6].hex(":") for t in (response.tlvs if response else []) if t.kind == 0xA2
        ]
        queried = response.mid if response else None
        before = len(box.sent(0x800C))
        await asyncio.sleep(12)
        unsolicited = [m for m in box.sent(0x800C)[before:] if m.mid != queried]
    finally:
        publisher.cancel()
    utilization = ap[6] if len(ap) >= 9 else None
    stations = int.from_bytes(ap[7:9], "big") if len(ap) >= 9 else None
    return box.result(
        passed=bool(acked)
        and ap[:6] == bssid
        and utilization == round(41 * 255 / 100)
        # traffic statistics only for the station whose counters the pod measures (spec 3.6)
        and stations == 2
        and sorted(link) == sorted(STATIONS)
        and traffic == [STATIONS[0]]
        and len(unsolicited) >= 1,
        utilization=utilization,
        stations=stations,
        link_metrics=sorted(link),
        traffic_stats=sorted(traffic),
        unsolicited=len(unsolicited),
    )


async def unassociated(box):
    """Unassociated STA Link Metrics (spec 3.9): the agent watches the stations asked about
    (a monitor-only steering row on the pod); a station the pod heard is reported with its
    RCPI; an associated one is refused with reason 1, one not heard with reason 2."""
    if not (await onboarded(box)).get("applied") or not await telemetry_applied(box):
        return box.result(passed=False, failed="not onboarded or no telemetry")
    heard, unheard, associated = "02:00:00:00:99:99", "02:00:00:00:98:98", STATIONS[0]
    query = Tlv(
        0x97,
        bytes([81, 1, 6, 3])
        + b"".join(bytes.fromhex(m.replace(":", "")) for m in (associated, heard, unheard)),
    )

    def refused(ack):
        # Error Code TLV: reason(1), station(6)
        return (
            sorted((t.value[1:7].hex(":"), t.value[0]) for t in ack.tlvs if t.kind == 0xA3)
            if ack
            else None
        )

    first = await box.ask(0x800F, (query,), ACK)

    async def watched():
        rows = (await box.rows("Band_Steering_Clients")).values()
        return sorted(r.get("mac") for r in rows if "emosa" in str(r.get("cs_params")))

    deadline, watch = time.monotonic() + 30, []
    while time.monotonic() < deadline and heard not in watch:
        watch = await watched()
        await asyncio.sleep(0.5)
    from emosa_lab.conformance import bs_report

    await publish(box, bs_report(now_ms() - 500, [(heard, ((0, "home-ap-24", ((0, 1500, 34),)),))]))
    await asyncio.sleep(2)
    reported = len(box.sent(0x8010))
    second = await box.ask(0x800F, (query,), ACK)
    response = await next_message(box, 0x8010, reported, seconds=10)
    measured = [t.value for t in (response.tlvs if response else []) if t.kind == 0x98]
    stations = []
    for value in measured:  # class(1), count(1), per station: MAC(6) channel(1) age(4) RCPI(1)
        at = 2
        for _ in range(value[1] if len(value) > 1 else 0):
            stations.append((value[at : at + 6].hex(":"), value[at + 6], value[at + 11]))
            at += 12
    return box.result(
        passed=refused(first) == sorted([(associated, 1), (heard, 2), (unheard, 2)])
        and heard in watch
        and refused(second) == sorted([(associated, 1), (unheard, 2)])
        and [s[:2] for s in stations] == [(heard, 6)]
        and stations[0][2] > 0,
        first_refusals=refused(first),
        watched=watch,
        second_refusals=refused(second),
        measured=stations,
    )


# Client steering (spec 3.7): the box plays the pod's owm on the steering rows

FRONTHAUL_BSSID = bytes.fromhex("820000000100")
TARGET = "02:00:00:12:75:2c"  # another AP's BSS, from spec/conformance/control.json


def steering_request(station, *, mandate=True, target=TARGET):
    """A Client Steering Request TLV (0x9B) from the pod's BSS: the mode (mandate, BTM
    disassociation imminent, abridged), a 5 s window and disassociation timer, one station
    and one target (operating class 81, channel 6)."""
    flags = 0xE0 if mandate else 0x60
    mac = bytes.fromhex(station.replace(":", ""))
    return Tlv(
        0x9B,
        FRONTHAUL_BSSID
        + bytes([flags])
        + b"\x00\x05\x00\x05\x01"
        + mac
        + b"\x01"
        + bytes.fromhex(target.replace(":", ""))
        + b"\x51\x06",
    )


async def steering_rows(box):
    clients = [
        r for r in (await box.rows("Band_Steering_Clients")).values() if r.get("mac") == STATIONS[0]
    ]
    neighbors = [
        r for r in (await box.rows("Wifi_VIF_Neighbors")).values() if r.get("bssid") == TARGET
    ]
    groups = list((await box.rows("Band_Steering_Config")).values())
    return clients, neighbors, groups


async def wait_rows(box, predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and box.process.poll() is None:
        box.controller.poll()
        rows = await steering_rows(box)
        if predicate(*rows):
            return rows
        await asyncio.sleep(0.3)
    return None


async def steering_leftover(box):
    """Box finding 20, rdk-emosa-1005 8 October: a steering window whose close failed (the pod's
    management link cut while it was open) left its rows in the pod, and every later mandate for
    the station was refused as another manager's steering, for good. Once the pod is back, the
    window's rows are closed by their UUIDs, and a second mandate for the station is carried
    out."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ack = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    opened = await wait_rows(box, lambda c, n, g: c and n and g)
    if not opened:
        return box.result(passed=False, failed="no steering window", acked=bool(ack))
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}", connect=False)  # owm never takes it

    def counts():
        return ((box.status() or {}).get("steering") or {}).get("counts") or {}

    close_failed = await box.until(lambda: counts().get("close_failed"), seconds=60)
    left, _, _ = await steering_rows(box)  # the pod's own database, the link still cut
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}")
    again = await provision(box, searches, m1s)
    closed = await wait_rows(box, lambda c, n, g: not c and not n and not g, seconds=30)
    second = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    reopened = await wait_rows(box, lambda c, n, g: c and n and g)
    refused = counts().get("refused_ownership_conflict", 0)
    return box.result(
        passed=bool(close_failed)
        and bool(left)
        and again
        and bool(closed)
        and bool(second)
        and bool(reopened)
        and not refused,
        close_failed_seen=bool(close_failed),
        rows_left_while_away=len(left),
        provisioning_again=again,
        leftover_closed=bool(closed),
        second_window=bool(reopened),
        refused_ownership_conflict=refused,
        counts=counts(),
    )


async def steering_close_refused(box):
    """Box finding 23: a window's close refused by the pod's database with the link up (the
    box's reply cut answers the delete with an error), so its rows stay in the pod on the same
    source. The sweep tries again after its back-off (30 s) and closes them by their UUIDs, and
    a second mandate for the station is carried out, not refused as another manager's (before:
    nothing tried again until the pod's next source, and the window's record could leave the
    journal first, leaving the rows for good)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ack = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    opened = await wait_rows(box, lambda c, n, g: c and n and g)
    if not opened:
        return box.result(passed=False, failed="no steering window", acked=bool(ack))

    def counts():
        return ((box.status() or {}).get("steering") or {}).get("counts") or {}

    # owm never takes the window: it ends unapplied, and its close is refused
    close_failed = await box.until(lambda: counts().get("close_failed"), seconds=60)
    left, _, _ = await steering_rows(box)
    closed = await wait_rows(box, lambda c, n, g: not c and not n and not g, seconds=60)
    second = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    reopened = await wait_rows(box, lambda c, n, g: c and n and g)
    refused = counts().get("refused_ownership_conflict", 0)
    return box.result(
        passed=bool(close_failed)
        and box.reply_cut.done
        and bool(left)
        and bool(closed)
        and bool(second)
        and bool(reopened)
        and not refused,
        close_refused=box.reply_cut.done,
        close_failed_seen=bool(close_failed),
        rows_left=len(left),
        leftover_closed=bool(closed),
        second_window=bool(reopened),
        refused_ownership_conflict=refused,
        counts=counts(),
    )


async def steering(box):
    """A steering mandate for one station and one target: acknowledged; a steering window
    opened as one write (the station's client row, a group, the target as a neighbor);
    applied when owm reports it steering, then the directed kick; when owm stops steering
    the window closes, deleting exactly the rows it inserted (spec 3.7)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    ack = await box.ask(0x8014, (steering_request(STATIONS[0]),), ACK)
    refusals = [t for t in (ack.tlvs if ack else []) if t.kind == 0xA3]
    opened = await wait_rows(box, lambda c, n, g: c and n and g)
    if not opened:
        return box.result(passed=False, failed="no steering window", acked=bool(ack))
    client = opened[0][0]
    btm = ovsdb_map(client.get("sc_btm_params"))
    uuid = next(
        u
        for u, r in (await box.rows("Band_Steering_Clients")).items()
        if r.get("mac") == STATIONS[0]
    )
    where = [["_uuid", "==", ["uuid", uuid]]]
    await box.transact(
        [
            {
                "op": "update",
                "table": "Band_Steering_Clients",
                "where": where,
                "row": {"cs_state": "steering"},
            }
        ]
    )  # owm takes the window
    kicked = await wait_rows(box, lambda c, n, g: c and c[0].get("force_kick") == "directed")
    await box.transact(
        [
            {
                "op": "update",
                "table": "Band_Steering_Clients",
                "where": where,
                "row": {"cs_state": "none"},
            }
        ]
    )  # the station went: owm stops steering
    await box.client_leave(STATIONS[0])
    closed = await wait_rows(box, lambda c, n, g: not c and not n and not g, seconds=30)
    return box.result(
        passed=bool(ack)
        and not refusals
        and client.get("cs_mode") == "away"
        and btm.get("bssid") == TARGET
        and bool(kicked)
        and bool(closed),
        acked=bool(ack),
        cs_mode=client.get("cs_mode"),
        kick_type=client.get("sc_kick_type"),
        btm_target=btm.get("bssid"),
        kicked=bool(kicked),
        closed=bool(closed),
    )


async def steering_refusals(box):
    """What the agent does not carry out (spec 3.7): a station not on the source BSS (an
    Error Code, reason 2); a steering opportunity (Steering Completed at once: EMOSA makes no
    choice of its own); a target the agent would have to choose. None opens a window."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    stranger = "02:00:00:00:99:99"
    ack = await box.ask(0x8014, (steering_request(stranger),), ACK)
    refused = sorted(
        (t.value[1:7].hex(":"), t.value[0]) for t in (ack.tlvs if ack else []) if t.kind == 0xA3
    )
    completed = len(box.sent(0x8017))
    opportunity = await box.ask(0x8014, (steering_request(STATIONS[0], mandate=False),), ACK)
    done = await next_message(box, 0x8017, completed, seconds=5)
    wildcard = await box.ask(
        0x8014, (steering_request(STATIONS[0], target="ff:ff:ff:ff:ff:ff"),), ACK
    )
    await asyncio.sleep(3)
    clients, neighbors, groups = await steering_rows(box)
    return box.result(
        passed=refused == [(stranger, 2)]
        and bool(opportunity)
        and bool(done)
        and bool(wildcard)
        and not clients
        and not neighbors,
        refused=refused,
        steering_completed=bool(done),
        agent_selected_acked=bool(wildcard),
        windows_opened=len(clients),
    )


# The pod's Wi-Fi backhaul (spec 8.3): the Multi-AP pod with option 1; the box plays cm and
# the supplicant when the station moves

TARGET_PARENT = "02:00:00:00:19:02"  # another backhaul BSS of the controller's


def uplink_status(box):
    return (box.status() or {}).get("uplink") or {}


def backhaul_steering_request(station, target=TARGET_PARENT, operating_class=81, channel=6):
    """Backhaul Steering Request TLV (0x9E): the backhaul station, the target BSS and its
    operating class and channel."""
    return Tlv(
        0x9E,
        bytes.fromhex(station.replace(":", ""))
        + bytes.fromhex(target.replace(":", ""))
        + bytes((operating_class, channel)),
    )


def steering_answer(response):
    """(station, target, status) of a Backhaul Steering Response's TLV (0x9F)."""
    value = tlv_value(response, 0x9F) or b""
    return (value[:6].hex(":"), value[6:12].hex(":"), value[12]) if len(value) >= 13 else None


async def pinned(box, bssid):
    """The station's multi_ap credential is pinned to this BSSID on the pod."""
    rows = (await box.rows("Wifi_Credential_Config")).values()
    return any(str(r.get("bssid") or "").lower() == bssid for r in rows)


async def move_station(box, bssid):
    """cm and the supplicant: the backhaul station now on this parent."""
    uuid, _ = await box.vif("Wifi_VIF_State", "bhaul-sta-24")
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"parent": bssid},
            }
        ]
    )


async def four_address(box, parent):
    """cm and owm complete the Multi-AP (4-address) join: the station on `parent`, cm using it
    (for a pod that started on a GTP bootstrap, so move_station's parent change alone isn't the
    4-address State the uplink switch waits for)."""
    uuid, _ = await box.vif("Wifi_VIF_State", "bhaul-sta-24")
    cm = next(iter(await box.rows("Connection_Manager_Uplink")))
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {
                    "parent": parent,
                    "ssid": "emosa-lab-bh",
                    "multi_ap": "backhaul_sta",
                    "wds": True,
                },
            },
            {
                "op": "update",
                "table": "Connection_Manager_Uplink",
                "where": [["_uuid", "==", ["uuid", cm]]],
                "row": {"if_name": "bhaul-sta-24", "if_type": "vif", "is_used": True},
            },
        ]
    )


async def on_gtp_bootstrap(box):
    """cm takes the station back to its GTP bootstrap: vconf on the bootstrap BSS, the gretap uplink
    (the state the pod returns to once EMOSA's credential is gone)."""
    uuid, _ = await box.vif("Wifi_VIF_State", "bhaul-sta-24")
    cm = next(iter(await box.rows("Connection_Manager_Uplink")))
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {
                    "parent": BOOTSTRAP_BSS,
                    "ssid": "opensync-lab-bhaul",
                    "multi_ap": "none",
                    "wds": False,
                },
            },
            {
                "op": "update",
                "table": "Connection_Manager_Uplink",
                "where": [["_uuid", "==", ["uuid", cm]]],
                "row": {"if_name": "g-bhaul-sta-24", "if_type": "gre", "is_used": True},
            },
        ]
    )


async def gre_join(box, parent):
    """cm and owm complete a 3-address GRE join onto `parent`: the station associated (not
    Multi-AP), cm using the gretap it built (spec 8.6)."""
    uuid, _ = await box.vif("Wifi_VIF_State", "bhaul-sta-24")
    cm = next(iter(await box.rows("Connection_Manager_Uplink")))
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {
                    "parent": parent,
                    "ssid": "emosa-lab-bh",
                    "multi_ap": "none",
                    "wds": False,
                },
            },
            {
                "op": "update",
                "table": "Connection_Manager_Uplink",
                "where": [["_uuid", "==", ["uuid", cm]]],
                "row": {"if_name": "g-bhaul-sta-24", "if_type": "gre", "is_used": True},
            },
        ]
    )


async def fallback_station(box):
    """The pod's fallback station (bhaul-sta-50) as pod-bh-fallback leaves it: enabled, its own
    credential and parent. Returns the columns EMOSA must never touch, to compare after a move."""
    uuid, row = await box.vif("Wifi_VIF_Config", "bhaul-sta-50")
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_Config",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"enabled": True, "parent": "02:00:00:00:1c:01"},
            }
        ]
    )
    _, now = await box.vif("Wifi_VIF_Config", "bhaul-sta-50")
    return {k: now.get(k) for k in ("enabled", "ssid", "credential_configs", "parent")}


async def backhaul_on(box):
    """Onboarded, and the uplink switch (option 1) applied: the station pinned to its parent."""
    if not (await onboarded(box)).get("applied"):
        return False

    def applied():
        operation = uplink_status(box).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED"

    return bool(await box.until(applied, seconds=60))


async def backhaul_capability(box):
    """The Backhaul STA Capability Report names each of the pod's backhaul STA radios, the one
    in use first: its 2.4 GHz station on the Multi-AP backhaul, then its disabled 5 GHz one
    (EasyMesh 6.1, 9.3; spec 2.4, 8.3); the uplink switch pins the first to the configured
    parent."""
    on = await backhaul_on(box)
    report = await box.ask(0x8027, (), 0x8028)
    radios = [
        (
            t.value[:6].hex(":"),
            t.value[7:13].hex(":") if len(t.value) >= 13 and t.value[6] & 0x80 else None,
        )
        for t in (report.tlvs if report else [])
        if t.kind == 0xCB
    ]
    expected = [
        ("02:00:00:00:14:00", BACKHAUL_STATION),  # in use, on the 2.4 GHz radio
        ("02:00:00:00:15:00", OTHER_STATION),  # disabled, on the 5 GHz radio
    ]
    return box.result(
        passed=on and radios == expected and await pinned(box, BACKHAUL_PARENT),
        uplink_applied=on,
        radios=radios,
    )


async def backhaul_steering(box, *, renew_midway=False):
    """A Backhaul Steering Request for the pod's backhaul station: acknowledged, the station
    re-pinned to the target; once the pod's State shows it there, the Backhaul Steering
    Response with the request's MID, success (spec 2.4, 8.3). With `renew_midway` a Renew
    and a new onboarding come between: the answer is kept (suite finding)."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    answered = len(box.sent(0x801A))
    mid = box.controller.send(0x8019, (backhaul_steering_request(BACKHAUL_STATION),))
    ack = await box.until(lambda: box.controller.reply(ACK, mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    repinned = await pinned(box, TARGET_PARENT)
    again = None
    if renew_midway:
        searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
        box.controller.send(
            RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0"))
        )
        again = await provision(box, searches, m1s)
    await move_station(box, TARGET_PARENT)
    response = await next_message(box, 0x801A, answered, seconds=60)
    answer = steering_answer(response)
    return box.result(
        passed=bool(ack)
        and repinned
        and response is not None
        and response.mid == mid
        and answer == (BACKHAUL_STATION, TARGET_PARENT, 0)
        and (again is not False),
        acked=bool(ack),
        repinned=repinned,
        renewed_and_provisioned=again,
        response_mid_matches=bool(response and response.mid == mid),
        answer=answer,
    )


async def backhaul_steering_renewal(box):
    return await backhaul_steering(box, renew_midway=True)


async def backhaul_steering_refused(box):
    """Without option 1 (the pod on OpenSync's GRE) a Backhaul Steering Request is answered
    at once: failure, with an Error Code for the station (spec 2.4, 8.3)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    station = "02:00:00:00:05:00"  # the recorded pod's backhaul station, on GRE
    answered = len(box.sent(0x801A))
    mid = box.controller.send(0x8019, (backhaul_steering_request(station),))
    response = await next_message(box, 0x801A, answered, seconds=10)
    errors = [
        (t.value[0], t.value[1:7].hex(":"))
        for t in (response.tlvs if response else [])
        if t.kind == 0xA3
    ]
    answer = steering_answer(response)
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and errors == [(6, station)],
        answer=answer,
        errors=errors,
    )


async def revert_by_record(box):
    """Spec 8.6: a pod on its GTP bootstrap, steered onto a parent and then back to its recorded
    bootstrap BSS. The agent records its bootstrap upstream before any move; a steer onto
    TARGET_PARENT pins the station there (a Multi-AP move here: the box has no GRE parent peer);
    a steer back to the bootstrap BSS is a revert - the agent restores the station's vconf snapshot,
    deletes the credential it inserted, and the station lands back on the bootstrap. Both answered
    success."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    recorded = await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    if not recorded or recorded.get("bssid") != BOOTSTRAP_BSS:
        return box.result(passed=False, failed="bootstrap not recorded", bootstrap=recorded)
    # onto the parent
    answered = len(box.sent(0x801A))
    onto_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, TARGET_PARENT),)
    )
    await box.until(lambda: box.controller.reply(ACK, onto_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    onto_pinned = await pinned(box, TARGET_PARENT)
    await four_address(box, TARGET_PARENT)
    onto = await next_message(box, 0x801A, answered, seconds=60)
    onto_ok = onto is not None and steering_answer(onto) == (BACKHAUL_STATION, TARGET_PARENT, 0)
    # back to the bootstrap: a revert
    answered2 = len(box.sent(0x801A))
    back_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, BOOTSTRAP_BSS),)
    )
    await box.until(lambda: box.controller.reply(ACK, back_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    deleted = not await pinned(box, TARGET_PARENT)  # the agent removed its own credential
    await on_gtp_bootstrap(box)
    back = await next_message(box, 0x801A, answered2, seconds=60)
    back_ok = back is not None and steering_answer(back) == (BACKHAUL_STATION, BOOTSTRAP_BSS, 0)
    return box.result(
        passed=onto_pinned and onto_ok and deleted and back_ok,
        bootstrap=recorded,
        onto_pinned=onto_pinned,
        onto=steering_answer(onto),
        credential_deleted=deleted,
        back=steering_answer(back),
    )


async def restart_during_move(box):
    """Spec 8.6: a pod OpenSync restart during a move drops it (the DB is rebuilt and the pod
    re-pins to its bootstrap by itself). EMOSA re-applies the active move from its persisted target
    once the pod is back, with no new operator steer."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    answered = len(box.sent(0x801A))
    onto_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, TARGET_PARENT),)
    )
    await box.until(lambda: box.controller.reply(ACK, onto_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    await four_address(box, TARGET_PARENT)
    onto = await next_message(box, 0x801A, answered, seconds=60)
    onto_ok = onto is not None and steering_answer(onto) == (BACKHAUL_STATION, TARGET_PARENT, 0)
    # the pod's OpenSync restarts: a fresh database, back on its bootstrap, the move dropped
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.new_pod_database()
    again = await provision(box, searches, m1s)
    # EMOSA re-applies the move from its persisted target, with no new operator steer
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, TARGET_PARENT):
        await asyncio.sleep(0.3)
    reapplied = await pinned(box, TARGET_PARENT)
    return box.result(
        passed=onto_ok and again and reapplied,
        onto=steering_answer(onto),
        provisioned_again=again,
        reapplied_without_a_steer=reapplied,
    )


async def fallback_during_move(box):
    """Spec 8.6 (EMOSA's half): a GRE move and its revert leave the pod's fallback station exactly
    as pod-bh-fallback set it - enabled, its own credential, its own parent. EMOSA writes only the
    moved station's columns, never the fallback's (a GRE move disables no other station). The other
    half (the fallback not taking EMOSA's credential) is the pod side: yocto-53's load_creds and the
    Pi soak, not the box."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    # a fleet GRE parent peer (so a steer onto it is a GRE join), and the fallback station up
    peer_status(box, "RPIPOD0FALLBACKPARENT", al=PARENT_AL, gre_parent_ap=GRE_PARENT_AP)
    fallback = await fallback_station(box)
    await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    await asyncio.sleep(2.5)  # the agent's peer directory (1 s cache) picks up the GRE parent
    # onto the GRE parent: a 3-address GRE move
    answered = len(box.sent(0x801A))
    onto_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, GRE_PARENT_AP),)
    )
    await box.until(lambda: box.controller.reply(ACK, onto_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not await pinned(box, GRE_PARENT_AP):
        await asyncio.sleep(0.3)
    creds = [
        r
        for r in (await box.rows("Wifi_Credential_Config")).values()
        if str(r.get("bssid") or "").lower() == GRE_PARENT_AP
    ]
    is_gre = bool(creds) and creds[0].get("onboard_type") == "gre"
    await gre_join(box, GRE_PARENT_AP)
    onto = await next_message(box, 0x801A, answered, seconds=60)
    onto_ok = onto is not None and steering_answer(onto) == (BACKHAUL_STATION, GRE_PARENT_AP, 0)
    _, onto_fb = await box.vif("Wifi_VIF_Config", "bhaul-sta-50")
    kept_onto = {k: onto_fb.get(k) for k in fallback} == fallback
    # back to the bootstrap: the revert
    answered2 = len(box.sent(0x801A))
    back_mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, BOOTSTRAP_BSS),)
    )
    await box.until(lambda: box.controller.reply(ACK, back_mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and await pinned(box, GRE_PARENT_AP):
        await asyncio.sleep(0.3)
    await on_gtp_bootstrap(box)
    back = await next_message(box, 0x801A, answered2, seconds=60)
    back_ok = back is not None and steering_answer(back) == (BACKHAUL_STATION, BOOTSTRAP_BSS, 0)
    _, back_fb = await box.vif("Wifi_VIF_Config", "bhaul-sta-50")
    kept_back = {k: back_fb.get(k) for k in fallback} == fallback
    return box.result(
        passed=is_gre and onto_ok and kept_onto and back_ok and kept_back,
        gre_move=is_gre,
        onto=steering_answer(onto),
        fallback_kept_through_move=kept_onto,
        back=steering_answer(back),
        fallback_kept_through_revert=kept_back,
    )


SWITCH_DEADLINE = 95  # past an uplink switch's 90 s to be confirmed (spec 8.3)


async def gre_pinned(box):
    """The station carries a gre credential pinned to the fleet parent's AP (a 3-address join)."""
    rows = (await box.rows("Wifi_Credential_Config")).values()
    return any(
        str(r.get("bssid") or "").lower() == GRE_PARENT_AP and r.get("onboard_type") == "gre"
        for r in rows
    )


def kept_target(box):
    """The upstream a controller's move left for the pod's later starts (spec 8.3), or None."""
    try:
        path = box.directory / "state" / box.serial / "uplink" / "target.json"
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


async def gre_child_moved(box):
    """A GRE child onboarded on its GTP bootstrap and moved onto a fleet GRE parent by the
    controller's Backhaul Steering Request, cm using the gretap on the parent: the answer, or None
    when the move did not complete."""
    if not (await onboarded(box)).get("applied"):
        return None
    peer_status(box, "RPIPOD0KEPTPARENT", al=PARENT_AL, gre_parent_ap=GRE_PARENT_AP)
    await box.until(lambda: uplink_status(box).get("bootstrap"), seconds=30)
    await asyncio.sleep(2.5)  # the agent's peer directory (1 s cache) picks up the GRE parent
    answered = len(box.sent(0x801A))
    mid = box.controller.send(0x8019, (backhaul_steering_request(BACKHAUL_STATION, GRE_PARENT_AP),))
    await box.until(lambda: box.controller.reply(ACK, mid), seconds=5)
    await until_async(lambda: gre_pinned(box), seconds=30)
    await gre_join(box, GRE_PARENT_AP)
    onto = await next_message(box, 0x801A, answered, seconds=60)
    answer = steering_answer(onto)
    return answer if answer == (BACKHAUL_STATION, GRE_PARENT_AP, 0) else None


async def until_async(predicate, seconds=60, step=0.3):
    """predicate() (a coroutine) until it holds; its last value."""
    deadline = time.monotonic() + seconds
    value = await predicate()
    while not value and time.monotonic() < deadline:
        await asyncio.sleep(step)
        value = await predicate()
    return value


async def gre_child_kept(box):
    """Spec 8.3 "Moved" for a GRE child (8.6), V54's fault row "pi2's OpenSync restarts": the
    controller's move onto a fleet GRE parent is kept for the pod's later starts. The pod's
    OpenSync starts again (a new database: back on its bootstrap, the move gone). On the new start
    the agent re-applies the move without a new steer, the same 3-address GRE join pinned to the
    parent AP, and it is applied once cm uses the gretap there: no hold, no second answer."""
    moved = await gre_child_moved(box)
    if moved is None:
        return box.result(passed=False, failed="the move onto the GRE parent did not complete")
    target = kept_target(box) or {}
    answers = len(box.sent(0x801A))
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.new_pod_database()  # back on its bootstrap
    on_bootstrap = not await gre_pinned(box)
    again = await provision(box, searches, m1s)
    reapplied = await until_async(lambda: gre_pinned(box), seconds=60)
    await gre_join(box, GRE_PARENT_AP)

    def applied():
        status = uplink_status(box)
        operation = status.get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED" and (
            str(status.get("parent") or "").lower() == GRE_PARENT_AP
        )

    done = await box.until(applied, seconds=60)
    status = uplink_status(box)
    return box.result(
        passed=on_bootstrap
        and bool(again)
        and bool(reapplied)
        and bool(done)
        and str(target.get("target") or "").lower() == GRE_PARENT_AP
        and target.get("mode") == "gre"
        and not status.get("held")
        and len(box.sent(0x801A)) == answers,
        kept_target=target,
        bootstrap_after_restart=on_bootstrap,
        provisioned_again=again,
        reapplied_without_a_steer=bool(reapplied),
        applied=bool(done),
        held=status.get("held"),
        answers_after_restart=len(box.sent(0x801A)) - answers,
    )


async def gre_child_kept_gone(box):
    """Spec 8.3 "Moved" for a GRE child (8.6), V54's fault row "pi1 reboots": the kept GRE
    parent is gone when the child's pod starts again. The agent re-applies the move; the station
    cannot join (no cm gretap on the parent), so the pod's OpenSync starts again on its bootstrap,
    as its router checks fail. The switch is not applied within its 90 s: the agent drops the
    target without a hold and, a GRE child having no configured upstream, writes nothing more:
    the pod stays on its bootstrap until the controller steers it again."""
    moved = await gre_child_moved(box)
    if moved is None:
        return box.result(passed=False, failed="the move onto the GRE parent did not complete")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.new_pod_database()  # the child's restart, the parent gone meanwhile
    await provision(box, searches, m1s)
    # its configuration observed on this start before the next (restart-during-write is the race)
    await box.until(lambda: operation_states(box)[-1:] == [("OBSERVED_APPLIED", None)], seconds=30)
    tried = await until_async(lambda: gre_pinned(box), seconds=60)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.new_pod_database()  # cm's router checks fail: OpenSync again, on its bootstrap
    again = await provision(box, searches, m1s)

    def timed_out():
        return (uplink_status(box).get("operation") or {}).get("state") == "TIMED_OUT"

    failed = await box.until(timed_out, seconds=SWITCH_DEADLINE + 60)
    await asyncio.sleep(10)
    status = uplink_status(box)
    rewritten = await gre_pinned(box)
    return box.result(
        passed=bool(tried)
        and bool(again)
        and bool(failed)
        and not status.get("held")
        and kept_target(box) is None
        and not rewritten,
        tried_kept_target=bool(tried),
        provisioned_again=again,
        kept_switch_timed_out=bool(failed),
        held=status.get("held"),
        target_dropped=kept_target(box) is None,
        written_again=rewritten,
        waiting=status.get("waiting"),
    )


async def backhaul_steering_own_bss(box):
    """Suite finding (a pod's station on its own backhaul BSS loops br-home): a Backhaul
    Steering Request whose target is one of the pod's own BSSes is refused at once, failure
    with an Error Code, and the station stays pinned where it is (spec 8.3)."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    own = next(
        r.get("mac")
        for r in (await box.rows("Wifi_VIF_State")).values()
        if r.get("if_name") == "home-ap-24"
    )
    answered = len(box.sent(0x801A))
    mid = box.controller.send(0x8019, (backhaul_steering_request(BACKHAUL_STATION, target=own),))
    response = await next_message(box, 0x801A, answered, seconds=10)
    answer = steering_answer(response)
    errors = [
        (t.value[0], t.value[1:7].hex(":"))
        for t in (response.tlvs if response else [])
        if t.kind == 0xA3
    ]
    await asyncio.sleep(2)
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and errors == [(6, BACKHAUL_STATION)]
        and not await pinned(box, own)
        and await pinned(box, BACKHAUL_PARENT),
        own_bssid=own,
        answer=answer,
        errors=errors,
    )


# A move to a BSS on another band (spec 8.3): the recorded pod's 5 GHz backhaul station,
# disabled while its 2.4 GHz one is the backhaul, takes the uplink

OTHER_STATION_IF, OTHER_STATION = "bhaul-sta-50", "02:00:00:00:15:00"
TARGET_5G = "02:00:00:00:19:05"  # a 5 GHz backhaul BSS of the controller's


async def station_config(box, if_name):
    """(enabled, BSSID its sole credential is pinned to) of the station's Config row."""
    _, row = await box.vif("Wifi_VIF_Config", if_name)
    if row is None:
        return None, None
    links = row.get("credential_configs") or []
    creds = await box.rows("Wifi_Credential_Config")
    pins = [str(creds[u].get("bssid") or "").lower() for u in links if u in creds]
    return row.get("enabled") is True, pins[0] if len(pins) == 1 else None


async def associate(box, station, parent, channel, previous):
    """cm and the supplicant: ``station`` associated with ``parent`` (4-address, Multi-AP),
    its radio on ``channel``; ``previous`` off and no longer cm's uplink."""
    creds = await box.rows("Wifi_Credential_Config")
    _, config = await box.vif("Wifi_VIF_Config", station)
    ssid = next(
        (creds[u].get("ssid") for u in config.get("credential_configs") or [] if u in creds), ""
    )
    state, _ = await box.vif("Wifi_VIF_State", station)
    old, _ = await box.vif("Wifi_VIF_State", previous)
    radio = next(
        u
        for u, r in (await box.rows("Wifi_Radio_State")).items()
        if state in (r.get("vif_states") or [])
    )
    uplinks = await box.rows("Connection_Manager_Uplink")
    ops = [
        {
            "op": "update",
            "table": "Wifi_VIF_State",
            "where": [["_uuid", "==", ["uuid", state]]],
            "row": {
                "enabled": True,
                "ssid": ssid,
                "multi_ap": "backhaul_sta",
                "wds": True,
                "parent": parent,
            },
        },
        {
            "op": "update",
            "table": "Wifi_VIF_State",
            "where": [["_uuid", "==", ["uuid", old]]],
            "row": {"enabled": False, "parent": ["set", []], "wds": ["set", []]},
        },
        {
            "op": "update",
            "table": "Wifi_Radio_State",
            "where": [["_uuid", "==", ["uuid", radio]]],
            "row": {"channel": channel},
        },
        {
            "op": "update",
            "table": "Connection_Manager_Uplink",
            "where": [["if_name", "==", previous]],
            "row": {"is_used": False},
        },
    ]
    if not any(r.get("if_name") == station for r in uplinks.values()):
        ops.append(
            {
                "op": "insert",
                "table": "Connection_Manager_Uplink",
                "row": {"if_name": station, "if_type": "vif", "has_L2": True, "has_L3": True},
            }
        )
    ops.append(
        {
            "op": "update",
            "table": "Connection_Manager_Uplink",
            "where": [["if_name", "==", station]],
            "row": {"is_used": True},
        }
    )
    await box.transact(ops)


def steering_errors(response):
    return [
        (t.value[0], t.value[1:7].hex(":"))
        for t in (response.tlvs if response else [])
        if t.kind == 0xA3
    ]


async def backhaul_steering_across_bands(box):
    """spec 8.3: a Backhaul Steering Request to a BSS on another band moves the uplink to the
    pod's backhaul station on that band: that station enabled and pinned to the target, the
    station in use disabled, in one transaction. Once the pod's State shows the new station on
    the target, the response, success, names it: the backhaul STA associated after the move
    (EasyMesh 6.1, 17.2.33)."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    answered = len(box.sent(0x801A))
    mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, TARGET_5G, 115, 36),)
    )
    ack = await box.until(lambda: box.controller.reply(ACK, mid), seconds=5)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        moved = await station_config(box, OTHER_STATION_IF) == (True, TARGET_5G)
        off = (await station_config(box, "bhaul-sta-24"))[0] is False
        if moved and off:
            break
        await asyncio.sleep(0.3)
    await associate(box, OTHER_STATION_IF, TARGET_5G, 36, "bhaul-sta-24")
    response = await next_message(box, 0x801A, answered, seconds=60)
    answer = steering_answer(response)
    return box.result(
        passed=bool(ack)
        and moved
        and off
        and response is not None
        and response.mid == mid
        and answer == (OTHER_STATION, TARGET_5G, 0),
        acked=bool(ack),
        five_ghz_station_pinned=moved,
        two_ghz_station_disabled=off,
        answer=answer,
    )


async def backhaul_steering_across_bands_fails(box):
    """spec 8.3: a move to another band's station that is not applied within the switch's
    deadline returns to the previous upstream with the previous station (enabled and pinned
    again, the other disabled), no hold, and is answered with a failure."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    answered = len(box.sent(0x801A))
    mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, TARGET_5G, 115, 36),)
    )
    response = await next_message(box, 0x801A, answered, seconds=150)
    answer = steering_answer(response)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        back = await station_config(box, "bhaul-sta-24") == (True, BACKHAUL_PARENT)
        other_off = (await station_config(box, OTHER_STATION_IF))[0] is False
        if back and other_off:
            break
        await asyncio.sleep(0.5)
    held = (uplink_status(box).get("held")) is not None
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and steering_errors(response) == [(6, BACKHAUL_STATION)]
        and back
        and other_off
        and not held,
        answer=answer,
        errors=steering_errors(response),
        back_on_the_previous_station=back,
        other_station_disabled=other_off,
        held=held,
    )


async def refused_with_channel_code(box, operating_class, channel, target):
    """A Backhaul Steering Request refused at once: failure, Error Code 0x04 (the backhaul STA
    cannot operate on the channel), nothing written: the station stays pinned where it is."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    answered = len(box.sent(0x801A))
    mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, target, operating_class, channel),)
    )
    response = await next_message(box, 0x801A, answered, seconds=10)
    answer = steering_answer(response)
    await asyncio.sleep(2)
    stays = await station_config(box, "bhaul-sta-24") == (True, BACKHAUL_PARENT)
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and steering_errors(response) == [(4, BACKHAUL_STATION)]
        and stays,
        answer=answer,
        errors=steering_errors(response),
        station_stays=stays,
    )


async def backhaul_steering_no_station_on_band(box):
    """spec 8.3: a target on a band the pod has no backhaul station on (6 GHz)."""
    return await refused_with_channel_code(box, 131, 37, "02:00:00:00:19:06")


async def backhaul_steering_channel_not_operable(box):
    """spec 8.3: a 2.4 GHz target on another channel than the radio that carries the pod's BSSes
    (channel 6): its station cannot leave that channel."""
    return await refused_with_channel_code(box, 81, 11, "02:00:00:00:19:0b")


# Pods as parents of other pods (spec 8.5). The other pod's agent is in the box as the peer
# directory reads it: its status beside the agent's, written as that agent writes it.

PARENT_AL, CHILD_AL = "02:72:00:00:00:0a", "02:72:00:00:00:0b"
CHILD_STATION = "02:00:00:00:0c:01"  # the child pod's backhaul station
MEASURED = {  # a station's measurement as an agent's status gives it (spec 3.6)
    "tx_rate_mbps": 72.2,
    "rx_rate_mbps": 65.0,
    "snr_db": 40,
    "tx_frames": 1200,
    "rx_frames": 900,
    "tx_errors": 3,
    "rx_errors": 1,
}


def peer_status(
    box,
    pod_id,
    *,
    al,
    backhaul_bsses=(),
    station=None,
    parent=None,
    stations=None,
    gre_parent_ap=None,
):
    """Another pod's agent in the fleet, as its status gives it (alive: the box's own process).
    gre_parent_ap: the BSSID of this peer's GRE parent AP (spec 8.6), so the agent reads it as a
    fleet parent and a steer onto it is a 3-address GRE join, not a Multi-AP switch."""
    directory = box.run_root / pod_id
    directory.mkdir(parents=True, exist_ok=True)
    status = {
        "pod_id": pod_id,
        "agent_al": al,
        "worker_pid": os.getpid(),
        "updated": time.time(),
        "pod": {
            "bsses": [
                {"role": "backhaul", "bssid": b, "ssid": "mesh_backhaul"} for b in backhaul_bsses
            ],
            "backhaul": None
            if station is None
            else {"station": "bhaul-sta-24", "mac": station, "parent": parent, "band": "2.4G"},
        },
        "telemetry": {"stations": stations or {}},
    }
    if gre_parent_ap is not None:
        status["gre_parent"] = {"ap_bssid": gre_parent_ap}
    (directory / "status.json").write_text(json.dumps(status))


def neighbor_tlvs(topology):
    """(local interface, [neighbor ALs]) of each 1905 Neighbor Device TLV of a Topology Response."""
    out = []
    for t in topology.tlvs if topology else ():
        if t.kind == 0x07 and len(t.value) >= 6:
            entries = t.value[6:]
            out.append(
                (
                    t.value[:6].hex(":"),
                    [entries[i : i + 6].hex(":") for i in range(0, len(entries), 7)],
                )
            )
    return out


def link_metrics(response):
    """The TLVs of a Link Metric Response: (kind, neighbor AL, local interface, neighbor interface,
    values), TX values (errors, packets, throughput, availability, PHY rate), RX (errors, packets,
    RSSI)."""
    out = []
    for t in response.tlvs if response else ():
        v = t.value
        if t.kind == 0x09 and len(v) >= 41:
            values = struct.unpack("!IIHHH", v[27:41])
        elif t.kind == 0x0A and len(v) >= 35:
            values = struct.unpack("!IIB", v[26:35])
        else:
            continue
        out.append((t.kind, v[6:12].hex(":"), v[12:18].hex(":"), v[18:24].hex(":"), list(values)))
    return out


def backhaul_neighbors(box):
    return ((box.status() or {}).get("pod") or {}).get("backhaul_neighbors") or []


async def pod_child(box):
    """spec 8.5: the pod on its EasyMesh backhaul, its upstream another pod's backhaul BSS: the
    Topology Response names that pod's agent as its 1905 neighbor on the backhaul station (the
    controller on Ethernet as before), and a Link Metric Query is answered from the parent's
    measurement of the station, the directions swapped."""
    measured = {**MEASURED, "measured_at": time.time()}
    peer_status(
        box,
        "PARENTPOD",
        al=PARENT_AL,
        backhaul_bsses=(BACKHAUL_PARENT,),
        stations={BACKHAUL_STATION: measured},
    )
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    linked = await box.until(lambda: backhaul_neighbors(box), seconds=15)
    topology = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    neighbors = neighbor_tlvs(topology)
    response = await box.ask(0x0005, (Tlv(0x08, b"\x00\x02"),), 0x0006)
    metrics = link_metrics(response)
    expected = [
        (0x09, PARENT_AL, BACKHAUL_STATION, BACKHAUL_PARENT, [1, 900, 65, 100, 65535]),
        (0x0A, PARENT_AL, BACKHAUL_STATION, BACKHAUL_PARENT, [3, 1200, 40]),
    ]
    return box.result(
        passed=bool(linked)
        and neighbors[0][1] == [box.controller.al.hex(":")]
        and (BACKHAUL_STATION, [PARENT_AL]) in neighbors
        and metrics == expected,
        status_neighbors=backhaul_neighbors(box),
        neighbors=neighbors,
        link_metrics=metrics,
    )


async def pod_parent(box):
    """spec 8.5: another pod's station on this pod's backhaul BSS, that pod's backhaul parent: the
    Topology Response names its agent as a 1905 neighbor on the BSS, and a Link Metric Query is
    answered from this pod's own measurement of the station (its client report, spec 3.6)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await attach_vifs(box.db)  # wm gives the backhaul BSS the agent created its BSSID
    _, bss = await box.vif("Wifi_VIF_State", "b-ap-24")
    bssid = (bss or {}).get("mac")
    if not bssid:
        return box.result(passed=False, failed="no backhaul BSS")
    peer_status(box, "CHILDPOD", al=CHILD_AL, station=CHILD_STATION, parent=bssid)
    await box.client_join(CHILD_STATION, "b-ap-24")
    if not await telemetry_applied(box):
        return box.result(passed=False, failed="statistics publishing not applied")
    from emosa_lab.conformance import COUNTERS_FULL, client_report

    now = int(time.time() * 1000)
    await publish(box, client_report(now - 1000, [(CHILD_STATION, 40, 72.2, 65.0, COUNTERS_FULL)]))

    def measured():
        return CHILD_STATION in (telemetry_status(box).get("stations") or {})

    reported = await box.until(measured, seconds=30)
    linked = await box.until(lambda: backhaul_neighbors(box), seconds=15)
    topology = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    neighbors = neighbor_tlvs(topology)
    response = await box.ask(
        0x0005, (Tlv(0x08, b"\x01" + bytes.fromhex(CHILD_AL.replace(":", "")) + b"\x02"),), 0x0006
    )
    metrics = link_metrics(response)
    seen = (telemetry_status(box).get("stations") or {}).get(CHILD_STATION) or {}
    expected = [
        (
            0x09,
            CHILD_AL,
            bssid,
            CHILD_STATION,
            [
                seen.get("tx_errors"),
                seen.get("tx_frames"),
                int(seen.get("tx_rate_mbps") or 0),
                100,
                65535,
            ],
        ),
        (
            0x0A,
            CHILD_AL,
            bssid,
            CHILD_STATION,
            [seen.get("rx_errors"), seen.get("rx_frames"), seen.get("snr_db")],
        ),
    ]
    return box.result(
        passed=bool(reported)
        and bool(linked)
        and (bssid, [CHILD_AL]) in neighbors
        and metrics == expected,
        backhaul_bss=bssid,
        status_neighbors=backhaul_neighbors(box),
        neighbors=neighbors,
        link_metrics=metrics,
        measured=seen,
    )


async def pod_parent_loop(box):
    """spec 8.5: a Backhaul Steering Request whose target is a BSS of a pod that hangs off this
    one would put a loop into br-home: refused at once (failure, an Error Code), the station
    left where it is."""
    if not await backhaul_on(box):
        return box.result(passed=False, failed="uplink switch not applied")
    own = next(
        r.get("mac")
        for r in (await box.rows("Wifi_VIF_State")).values()
        if r.get("if_name") == "home-ap-24"
    )
    downstream = "72:00:00:00:0d:00"  # the child pod's backhaul BSS; the child is on this pod
    peer_status(
        box,
        "CHILDPOD",
        al=CHILD_AL,
        backhaul_bsses=(downstream,),
        station=CHILD_STATION,
        parent=own,
    )
    await asyncio.sleep(1.5)  # the agent's directory is read at most once a second
    answered = len(box.sent(0x801A))
    mid = box.controller.send(
        0x8019, (backhaul_steering_request(BACKHAUL_STATION, target=downstream),)
    )
    response = await next_message(box, 0x801A, answered, seconds=10)
    answer = steering_answer(response)
    await asyncio.sleep(2)
    return box.result(
        passed=response is not None
        and response.mid == mid
        and answer is not None
        and answer[2] == 1
        and not await pinned(box, downstream)
        and await pinned(box, BACKHAUL_PARENT),
        answer=answer,
    )


# spec 8.6 (draft): a pod as the GRE parent of other pods. The box's pod runs a gre-parent
# profile with an underlay; the controller's backhaul BSS becomes the parent AP b-ap-24, and
# a child is a lease in the underlay whose MAC is associated to it.
GRE_UNDERLAY = "169.254.2.0/24"
GRE_AP = "b-ap-24"
GRE_CHILD_MAC = "02:00:00:00:2a:01"
GRE_CHILD_IP = "169.254.2.10"  # its tunnel is pgd2_10


async def gre_parent_up(box):
    """Onboard a gre-parent pod: the M2's backhaul BSS written as the parent AP. (parent AP
    Config row, None) on success, else (None, a failed result)."""
    state = await onboarded(box)
    if not state.get("applied"):
        return None, box.result(passed=False, failed=state.get("failed", "not onboarded"))
    _, ap = await box.vif("Wifi_VIF_Config", GRE_AP)
    if not ap:
        return None, box.result(passed=False, failed="no parent AP b-ap-24")
    return ap, None


async def gre_inet(box, if_name):
    """The pod's Wifi_Inet_Config row for an interface, decoded, or None."""
    for row in (await box.rows("Wifi_Inet_Config")).values():
        if row.get("if_name") == if_name:
            return row
    return None


async def gre_parent_create(box):
    """spec 8.6: a backhaul BSS of the controller's M2 set is written as the pod's GRE parent AP
    (b-ap-24): not Multi-AP, no bridge, no bridging between its stations, hidden; its Inet row the
    pod's underlay at .1 with DHCP .10 to .250. The agent recognizes it as its parent AP."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    inet = await gre_inet(box, GRE_AP)
    dhcpd = ovsdb_map((inet or {}).get("dhcpd"))
    status = (box.status() or {}).get("gre_parent") or {}
    ap_ok = (
        ap.get("multi_ap") == "none"
        and ap.get("bridge") == ""
        and ap.get("ap_bridge") is False
        and ap.get("ssid_broadcast") == "disabled"
    )
    inet_ok = (
        inet is not None
        and inet.get("inet_addr") == "169.254.2.1"
        and inet.get("netmask") == "255.255.255.0"
        and inet.get("mtu") == 1600
        and dhcpd.get("start") == "169.254.2.10"
        and dhcpd.get("stop") == "169.254.2.250"
    )
    return box.result(
        passed=ap_ok
        and inet_ok
        and status.get("ap") == GRE_AP
        and status.get("underlay") == GRE_UNDERLAY,
        parent_ap={
            k: ap.get(k) for k in ("ssid", "multi_ap", "bridge", "ap_bridge", "ssid_broadcast")
        },
        underlay_inet=inet,
        gre_status=status,
    )


GRE_PROFILE = "opensync-lab-hwsim-6.6.1-gre-v1"
PLAIN_PROFILE = "opensync-lab-hwsim-6.6.1-v1"


async def with_profile(box, profile, done):
    """The pod's profile changed (a promotion or demotion, spec 8.6): its agent killed, its
    configuration given the profile, the agent started again on its journal, and every Search and
    M1 answered until done(operations before) holds. False at the deadline."""
    box.process.kill()
    box.process.wait(10)
    config_path = box.directory / "agent.json"
    config = json.loads(config_path.read_text())
    config["profile"] = profile
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    operations = len((box.status() or {}).get("operations") or [])
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.restart_agent()
    found = await serve_radios(box, lambda: done(operations), searches=searches, m1s=m1s)
    return found is not None


async def pod_own_rows(box):
    """The pod's own rows as a GRE parent's changes must leave them: its Inet rows and VIFs other
    than EMOSA's (the fronthaul, the slots, the tunnels), and br-home's ports other than the
    tunnels'."""
    emosa = {COLD_VIF, "svc-d-ap-24", "svc-e-ap-24", "fh-24", GRE_AP}

    def own(rows):
        return {
            u: r
            for u, r in rows.items()
            if r.get("if_name") not in emosa and not str(r.get("if_name", "")).startswith("pgd")
        }

    ports = await box.rows("Port")
    tunnels = {u for u, p in ports.items() if str(p.get("name", "")).startswith("pgd")}
    bridge = next(
        (b for b in (await box.rows("Bridge")).values() if b.get("name") == "br-home"), {}
    )
    return {
        "inet": own(await box.rows("Wifi_Inet_Config")),
        "vifs": own(await box.rows("Wifi_VIF_Config")),
        "br-home": sorted(set(uuids(bridge.get("ports"))) - tunnels),
    }


async def gre_parent_demoted(box):
    """spec 8.6, a parent demoted (the mirror of finding A): a GRE parent with a child's tunnel,
    its profile flipped back to the plain one and its agent restarted on its journal. The
    unchanged M2 set makes the same b-ap-24 row a Multi-AP backhaul BSS again and its Inet row the
    profile's slot row: no underlay address and no DHCP server left on the backhaul. The tunnel
    left on it goes with its br-home port, the pod's own rows are untouched, and the agent no
    longer advertises a parent AP."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    if not await gre_await_tunnel(box, "pgd2_10", present=True):
        return box.result(passed=False, failed="the child's tunnel was not made")
    await gre_nm_apply(box)
    await apply_configuration(box.db)
    uuid, _ = await box.vif("Wifi_VIF_Config", GRE_AP)
    own_before = await pod_own_rows(box)

    async def demoted(operations):
        ops = (box.status() or {}).get("operations") or []
        _, vif = await box.vif("Wifi_VIF_Config", GRE_AP)
        inet = await gre_inet(box, GRE_AP) or {}
        return (
            len(ops) > operations
            and all_applied(ops)
            and (vif or {}).get("multi_ap") == "backhaul_bss"
            and not ovsdb_map(inet.get("dhcpd"))
            and await gre_inet(box, "pgd2_10") is None
        )

    if not await with_profile(box, PLAIN_PROFILE, demoted):
        return box.result(passed=False, failed="b-ap-24 was not made a Multi-AP backhaul again")
    after_uuid, vif = await box.vif("Wifi_VIF_Config", GRE_AP)
    inet = await gre_inet(box, GRE_AP) or {}
    status = (box.status() or {}).get("gre_parent") or {}
    own_after = await pod_own_rows(box)
    # an optional column unset is the empty set
    plain = {
        k: None if inet.get(k) == ["set", []] else inet.get(k)
        for k in ("ip_assign_scheme", "inet_addr", "netmask", "mtu")
    }
    return box.result(
        passed=after_uuid == uuid
        and (vif or {}).get("multi_ap") == "backhaul_bss"
        and (vif or {}).get("bridge") == "br-home"
        and plain == {"ip_assign_scheme": "none", "inet_addr": None, "netmask": None, "mtu": 1500}
        and not ovsdb_map(inet.get("dhcpd"))
        and not await gre_port_in_home(box, "pgd2_10")
        and own_after == own_before
        and status.get("withdrawn") is True
        and status.get("ap_bssid") is None,
        same_row=after_uuid == uuid,
        multi_ap=(vif or {}).get("multi_ap"),
        inet=plain,
        dhcpd=ovsdb_map(inet.get("dhcpd")),
        own_rows_unchanged=own_after == own_before,
        gre_status=status,
    )


async def gre_parent_promoted(box):
    """Finding A (spec 8.6): a pod onboarded with a Multi-AP backhaul BSS (b-ap-24, multi_ap
    backhaul_bss), then promoted to GRE parent while onboarded: its profile flipped to the
    gre-parent one and its agent restarted on its journal. The controller's unchanged M2 set is
    not taken as applied while b-ap-24 is still a Multi-AP BSS: the agent converts that same row
    into the parent AP (multi_ap none, no bridge, hidden) and writes its underlay Inet row, in one
    transaction. A Renew afterwards finds it applied and writes nothing."""
    state = await onboarded(box)
    if not state.get("applied"):
        return box.result(passed=False, failed=state.get("failed", "not onboarded"))
    uuid, before = await box.vif("Wifi_VIF_Config", GRE_AP)
    if not before or before.get("multi_ap") != "backhaul_bss":
        return box.result(passed=False, failed="no Multi-AP backhaul BSS b-ap-24 to promote")
    await apply_configuration(box.db)

    async def converted(operations):
        ops = (box.status() or {}).get("operations") or []
        _, ap = await box.vif("Wifi_VIF_Config", GRE_AP)
        return len(ops) > operations and all_applied(ops) and (ap or {}).get("multi_ap") == "none"

    if not await with_profile(box, GRE_PROFILE, converted):
        return box.result(passed=False, failed="b-ap-24 was not converted into the parent AP")
    after_uuid, ap = await box.vif("Wifi_VIF_Config", GRE_AP)
    inet = await gre_inet(box, GRE_AP)
    ops = (box.status() or {}).get("operations") or []
    wrote = not (ops[-1].get("application_evidence") or {}).get("observed_noop")
    # a Renew: the same M2 set, now applied
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    count = len(ops)

    async def taken_again():
        again = (box.status() or {}).get("operations") or []
        return len(again) > count and all_applied(again)

    renewed = await serve_radios(box, taken_again, searches=searches, m1s=m1s) is not None
    again = (box.status() or {}).get("operations") or []
    noop = bool((again[-1].get("application_evidence") or {}).get("observed_noop"))
    status = (box.status() or {}).get("gre_parent") or {}
    rendered = {k: (ap or {}).get(k) for k in ("multi_ap", "bridge", "ap_bridge", "ssid_broadcast")}
    return box.result(
        passed=after_uuid == uuid
        and rendered
        == {"multi_ap": "none", "bridge": "", "ap_bridge": False, "ssid_broadcast": "disabled"}
        and (inet or {}).get("inet_addr") == "169.254.2.1"
        and wrote
        and renewed
        and noop
        and status.get("ap") == GRE_AP,
        same_row=after_uuid == uuid,
        parent_ap=rendered,
        underlay_inet=(inet or {}).get("inet_addr"),
        conversion_written=wrote,
        renew_noop=noop,
    )


async def gre_parent_ready(box):
    """nm applies the agent's underlay to the AP's Wifi_Inet_State, and the pod's bootstrap has
    br-home: what a tunnel needs besides the parent AP. br-home hangs from the Open_vSwitch root
    (Open_vSwitch -> Bridge -> Port -> Interface), so it is not garbage-collected."""
    await box.transact(
        [
            {
                "op": "insert",
                "table": "Wifi_Inet_State",
                "row": {"if_name": GRE_AP, "if_type": "vif", "inet_addr": "169.254.2.1"},
            },
            {
                "op": "insert",
                "table": "Interface",
                "uuid-name": "brhomeif",
                "row": {"name": "br-home"},
            },
            {
                "op": "insert",
                "table": "Port",
                "uuid-name": "brhomeport",
                "row": {"name": "br-home", "interfaces": ["set", [["named-uuid", "brhomeif"]]]},
            },
            {
                "op": "insert",
                "table": "Bridge",
                "uuid-name": "brhomebr",
                "row": {"name": "br-home", "ports": ["set", [["named-uuid", "brhomeport"]]]},
            },
            {
                "op": "insert",
                "table": "Open_vSwitch",
                "row": {"bridges": ["set", [["named-uuid", "brhomebr"]]]},
            },
        ]
    )


async def gre_port_in_home(box, name):
    """The Port `name` is a member of br-home (the tunnel joined the bridge)."""
    ports = await box.rows("Port")
    bridge = next(
        (r for r in (await box.rows("Bridge")).values() if r.get("name") == "br-home"), None
    )
    members = {ports[u]["name"] for u in uuids((bridge or {}).get("ports")) if u in ports}
    return name in members


async def gre_wm_remove_ap(box, if_name):
    """OpenSync's wm tears down a VIF whose Config the agent deleted: its Wifi_VIF_State and the
    clients on it go, and the radio's State drops the reference. Nothing when it is already gone."""
    uuid, _ = await box.vif("Wifi_VIF_State", if_name)
    if uuid is None:
        return
    vif = (await box.rows("Wifi_VIF_State")).get(uuid, {})
    ops = []
    for radio_uuid, radio in (await box.rows("Wifi_Radio_State")).items():
        if uuid in uuids(radio.get("vif_states")):
            ops.append(
                {
                    "op": "mutate",
                    "table": "Wifi_Radio_State",
                    "where": [["_uuid", "==", ["uuid", radio_uuid]]],
                    "mutations": [["vif_states", "delete", ["set", [["uuid", uuid]]]]],
                }
            )
    for client in uuids(vif.get("associated_clients")):
        ops.append(
            {
                "op": "delete",
                "table": "Wifi_Associated_Clients",
                "where": [["_uuid", "==", ["uuid", client]]],
            }
        )
    ops.append(
        {"op": "delete", "table": "Wifi_VIF_State", "where": [["_uuid", "==", ["uuid", uuid]]]}
    )
    for state_uuid, state in (await box.rows("Wifi_Inet_State")).items():
        if state.get("if_name") == if_name:  # nm drops the interface's address with the netdev
            ops.append(
                {
                    "op": "delete",
                    "table": "Wifi_Inet_State",
                    "where": [["_uuid", "==", ["uuid", state_uuid]]],
                }
            )
    await box.transact(ops)


async def gre_nm_apply(box):
    """nm brings up each gretap the agent created: it gives the gre interface a Wifi_Inet_State,
    which is how the tunnel reconcile sees the tunnel applied (OBSERVED_APPLIED)."""
    states = {r.get("if_name") for r in (await box.rows("Wifi_Inet_State")).values()}
    ops = [
        {
            "op": "insert",
            "table": "Wifi_Inet_State",
            "row": {"if_name": r.get("if_name"), "if_type": "gre"},
        }
        for r in (await box.rows("Wifi_Inet_Config")).values()
        if r.get("if_type") == "gre" and r.get("if_name") not in states
    ]
    if ops:
        await box.transact(ops)


async def gre_await_applied(box, seconds=30):
    """Poll until the tunnel reconcile's latest operation is OBSERVED_APPLIED."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and box.process.poll() is None:
        operation = ((box.status() or {}).get("gre_parent") or {}).get("operation") or {}
        if operation.get("state") == "OBSERVED_APPLIED":
            return True
        await asyncio.sleep(0.3)
    return False


async def gre_await_config_gone(box, if_name, seconds=30):
    """Poll until the agent has deleted a VIF's Wifi_VIF_Config row."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and box.process.poll() is None:
        if (await box.vif("Wifi_VIF_Config", if_name))[1] is None:
            return True
        await asyncio.sleep(0.3)
    return False


async def gre_await_tunnel(box, name, present, seconds=60):
    """Poll the pod's own rows (not the status, which lags a tick) until the tunnel `name` is
    there (present) or gone: its gre Wifi_Inet_Config row and its br-home port both exist, or
    both do not."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and box.process.poll() is None:
        there = await gre_inet(box, name) is not None and await gre_port_in_home(box, name)
        if there == present:
            return True
        await asyncio.sleep(0.3)
    return False


async def gre_parent_child(box):
    """spec 8.6: a child that associated to the parent AP and took a lease in the underlay gets a
    gre Wifi_Inet_Config row pgd<b3>_<b4> from the AP's address to the lease, and a port of
    br-home."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    made = await gre_await_tunnel(box, "pgd2_10", present=True)
    row = await gre_inet(box, "pgd2_10")
    bridged = await gre_port_in_home(box, "pgd2_10")
    await gre_nm_apply(box)  # nm brings the gretap up
    applied = await gre_await_applied(box)
    return box.result(
        passed=bool(made)
        and row is not None
        and row.get("if_type") == "gre"
        and row.get("gre_ifname") == GRE_AP
        and row.get("gre_remote_inet_addr") == GRE_CHILD_IP
        and row.get("gre_local_inet_addr") == "169.254.2.1"
        and row.get("mtu") == 1562
        and bridged
        and bool(applied),
        tunnel=row,
        in_br_home=bridged,
        gre_status=(box.status() or {}).get("gre_parent"),
    )


async def gre_parent_child_gone(box):
    """spec 8.6: when a child's lease or association ends, its tunnel row and its br-home port are
    removed."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    if not await gre_await_tunnel(box, "pgd2_10", present=True):
        return box.result(passed=False, failed="the tunnel was never made")
    await gre_nm_apply(box)  # the gretap up, so the create op settles before the child leaves
    await gre_await_applied(box)
    await box.client_leave(GRE_CHILD_MAC, GRE_AP)
    lease = next(
        u
        for u, r in (await box.rows("DHCP_leased_IP")).items()
        if r.get("inet_addr") == GRE_CHILD_IP
    )
    await box.transact(
        [{"op": "delete", "table": "DHCP_leased_IP", "where": [["_uuid", "==", ["uuid", lease]]]}]
    )
    removed = await gre_await_tunnel(box, "pgd2_10", present=False)
    row = await gre_inet(box, "pgd2_10")
    bridged = await gre_port_in_home(box, "pgd2_10")
    return box.result(
        passed=bool(removed) and row is None and not bridged,
        tunnel=row,
        in_br_home=bridged,
        gre_status=(box.status() or {}).get("gre_parent"),
    )


async def gre_drift_remove_tunnel(box, name):
    """Remove a tunnel's rows as if nm or a hand delete took it (drift): its gre Wifi_Inet_Config
    and Wifi_Inet_State, and its Port out of br-home (the Port and its Interface with it). The
    child's lease and association are left in place."""

    def delete(table, u):
        return {"op": "delete", "table": table, "where": [["_uuid", "==", ["uuid", u]]]}

    ops = [
        delete(t, u)
        for t in ("Wifi_Inet_Config", "Wifi_Inet_State")
        for u, r in (await box.rows(t)).items()
        if r.get("if_name") == name
    ]
    ports = await box.rows("Port")
    port_uuid = next((u for u, r in ports.items() if r.get("name") == name), None)
    if port_uuid is not None:
        bridge = next(
            (u for u, r in (await box.rows("Bridge")).items() if r.get("name") == "br-home"), None
        )
        if bridge is not None:
            ops.append(
                {
                    "op": "mutate",
                    "table": "Bridge",
                    "where": [["_uuid", "==", ["uuid", bridge]]],
                    "mutations": [["ports", "delete", ["set", [["uuid", port_uuid]]]]],
                }
            )
        ops += [delete("Interface", iu) for iu in uuids(ports[port_uuid].get("interfaces"))]
        ops.append(delete("Port", port_uuid))
    if ops:
        await box.transact(ops)


async def gre_parent_child_redrift(box):
    """spec 8.6 / 7f 11 Oct: a child's tunnel that drifts away while the child is still an
    associated lease (a re-appearance with the same lease after a backhaul bounce, or a hand
    delete) is re-created. The applied-state alone never stands in for the tunnels present."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    if not await gre_await_tunnel(box, "pgd2_10", present=True):
        return box.result(passed=False, failed="the tunnel was never made")
    await gre_nm_apply(box)
    if not await gre_await_applied(box):
        return box.result(passed=False, failed="the first tunnel never settled")
    first = (((box.status() or {}).get("gre_parent") or {}).get("operation") or {}).get(
        "operation_id"
    )
    # the tunnel drifts away while the child stays an associated lease (the gre row and its port
    # are deleted). The scope re-creates it so promptly that the gone state is not reliably
    # observable; the proof is a fresh write (a new operation), not a glimpse of the gap.
    await gre_drift_remove_tunnel(box, "pgd2_10")
    # the scope re-creates it in a fresh write; nm brings the new gretap up so that write settles.
    # Poll both together (a sequential remade->nm->applied races the prompt re-create under load).
    remade = False
    op_id = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and box.process.poll() is None:
        await gre_nm_apply(box)
        present = await gre_inet(box, "pgd2_10") is not None and await gre_port_in_home(
            box, "pgd2_10"
        )
        operation = ((box.status() or {}).get("gre_parent") or {}).get("operation") or {}
        op_id = operation.get("operation_id")
        if present and op_id not in (None, first) and operation.get("state") == "OBSERVED_APPLIED":
            remade = True
            break
        await asyncio.sleep(0.3)
    row = await gre_inet(box, "pgd2_10")
    bridged = await gre_port_in_home(box, "pgd2_10")
    return box.result(
        passed=remade  # a fresh write (op != first) re-made the tunnel and settled
        and row is not None
        and row.get("gre_remote_inet_addr") == GRE_CHILD_IP
        and row.get("gre_ifname") == GRE_AP
        and bridged,
        tunnel=row,
        in_br_home=bridged,
        first_op=first,
        re_created_by=op_id,
        gre_status=(box.status() or {}).get("gre_parent"),
    )


async def gre_parent_withdrawn(box):
    """spec 8.6: the controller drops the backhaul BSS from the M2 set (a renew with only the
    fronthaul). EMOSA's AP scope removes the parent AP b-ap-24 and its underlay Inet row; the
    tunnel scope, seeing the AP and its children gone, then removes every child's gretap and
    br-home port. The window between the two transactions (the gretaps briefly in br-home with
    their local address gone) is bounded and leaves br-home's own port untouched, the agent up and
    its tunnel reconcile settled. NM/osw behaviour in that window is the pod's (yocto-53's twin
    scenario 3, real OpenSync); the box has no netdevs."""
    ap, failure = await gre_parent_up(box)
    if failure:
        return failure
    await gre_parent_ready(box)
    await box.transact(
        [
            {
                "op": "insert",
                "table": "DHCP_leased_IP",
                "row": {"hwaddr": GRE_CHILD_MAC, "inet_addr": GRE_CHILD_IP},
            }
        ]
    )
    await box.client_join(GRE_CHILD_MAC, GRE_AP)
    if not await gre_await_tunnel(box, "pgd2_10", present=True):
        return box.result(passed=False, failed="the tunnel was never made")
    await gre_nm_apply(box)  # the gretap up and the create op settled before the withdrawal
    await gre_await_applied(box)
    # the controller withdraws the backhaul BSS: the next M2 set has only the fronthaul
    box.m2_modes = ("configure",)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    again = await provision(box, searches, m1s)
    ap_gone = await gre_await_config_gone(box, GRE_AP)
    ap_inet_gone = await gre_inet(box, GRE_AP) is None
    # the AP scope's transaction is done; wm now tears down the netdev, and the window to the
    # tunnel scope's transaction begins
    window_start = time.monotonic()
    await gre_wm_remove_ap(box, GRE_AP)
    removed = await gre_await_tunnel(box, "pgd2_10", present=False)
    window = time.monotonic() - window_start
    settled = await gre_await_applied(box)  # the removal reconciled (settle lags the DB by a tick)
    row = await gre_inet(box, "pgd2_10")
    bridged = await gre_port_in_home(box, "pgd2_10")
    home_port = await gre_port_in_home(box, "br-home")  # br-home's own port untouched
    status = (box.status() or {}).get("gre_parent") or {}
    return box.result(
        passed=again
        and ap_gone
        and ap_inet_gone
        and bool(removed)
        and row is None
        and not bridged
        and home_port
        and window < 10
        and box.process.poll() is None
        and bool(settled),
        window_s=round(window, 1),
        br_home_own_port=home_port,
        gre_status=status,
    )


# The reference workload's faults, in the box


RUID_24, RUID_50 = "02:00:00:00:01:00", "02:00:00:00:05:00"  # the recorded pod's phy1, phy5
PARENT_AP_50 = "b-ap-50"


def radio_operations(box, band):
    """The operations of the agent's scope for its radio of this band (spec 9)."""
    return ((box.status() or {}).get("radio_operations") or {}).get(band) or []


async def radio_config(box, band):
    """(uuid, row) of the pod's Wifi_Radio_Config of a band."""
    for uuid, row in (await box.rows("Wifi_Radio_Config")).items():
        if row.get("freq_band") == band:
            return uuid, row
    return None, None


async def serve_radios(box, done, *, searches=0, m1s=0, seconds=90):
    """A controller configuring each radio of the pod (spec 9): every Search gets a Response and
    every M1 an M2 set for the radio its AP Radio Basic Capabilities name; the pod's managers
    apply what the agent writes, until done() holds. The RUIDs of the M1s answered, in order,
    or None at the deadline."""
    deadline, last, ruids = time.monotonic() + seconds, None, []
    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        for search in found[searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            ruids.append(ruid.hex(":"))
            await answer_m1(box, ruid, wsc)
        m1s = len(found)
        configs = await box.rows("Wifi_VIF_Config")
        if configs != last:
            last = configs
            await apply_configuration(box.db)
            await attach_vifs(box.db)
        if await done():
            return ruids
        await asyncio.sleep(0.3)
    return None


def all_applied(operations):
    return bool(operations) and all(op.get("state") == "OBSERVED_APPLIED" for op in operations)


async def both_radios_applied(box):
    status = box.status() or {}
    return all_applied(status.get("operations") or []) and all_applied(radio_operations(box, "5G"))


def operational_radios(topology):
    """{RUID: [BSSID, ...]} of a Topology Response's AP Operational BSS TLV."""
    value = next((t.value for t in topology.tlvs if t.kind == 0x83), b"")
    radios, at = {}, 1
    for _ in range(value[0] if value else 0):
        ruid, count, at = value[at : at + 6].hex(":"), value[at + 6], at + 7
        radios[ruid] = []
        for _ in range(count):
            radios[ruid].append(value[at : at + 6].hex(":"))
            at += 7 + value[at + 6]
    return radios


async def second_radio_parent(box):
    """Spec 9: a GRE parent whose parent AP is on its 5 GHz radio (the Pis' adapter B). The agent
    sends an M1 for each radio, the 2.4 GHz one first; the 2.4 GHz radio's M2 set configures the
    fronthaul (its backhaul BSS has no slot there and is left out), the 5 GHz radio's set makes
    b-ap-50 the parent AP on the 5 GHz radio, through that radio's own scope. The pod's station
    on that radio and the radio's channel are left alone; the Topology Response reports both
    radios, b-ap-50 under the 5 GHz one."""
    before_radio = (await radio_config(box, "5G"))[1]
    before_station = (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1]
    ruids = await serve_radios(box, lambda: both_radios_applied(box))
    if ruids is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    radio_uuid, radio = await radio_config(box, "5G")
    ap_uuid, ap = await box.vif("Wifi_VIF_Config", PARENT_AP_50)
    _, ap_state = await box.vif("Wifi_VIF_State", PARENT_AP_50)
    _, fronthaul = await box.vif("Wifi_VIF_Config", COLD_VIF)
    on_24 = uuids((await radio_config(box, "2.4G"))[1].get("vif_configs"))
    inet = await gre_inet(box, PARENT_AP_50)
    station = (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1]
    status = box.status() or {}
    topology = await box.ask(TOPOLOGY_QUERY, (Tlv(0xB3, b"\x01"),), TOPOLOGY_RESPONSE)
    reported = operational_radios(topology) if topology else {}
    # each M1's WSC RF Bands: 1 for the 2.4 GHz radio's, 2 for the 5 GHz radio's
    rf_bands = [
        next(
            a.value[0]
            for a in decode_attributes(next(t.value for t in m1.tlvs if t.kind == 0x11))
            if a.kind == 0x103C
        )
        for m1 in box.sent(AUTOCONFIG_WSC)
    ]
    bssid = (ap_state or {}).get("mac")
    unchanged = {k: v for k, v in before_radio.items() if k != "vif_configs"} == {
        k: v for k, v in radio.items() if k != "vif_configs"
    }
    passed = (
        ruids[:2] == [RUID_24, RUID_50]
        and rf_bands[:2] == [1, 2]
        and ap is not None
        and ap_uuid in uuids(radio.get("vif_configs"))
        and ap_uuid not in on_24
        and (ap.get("ssid"), ap.get("multi_ap"), ap.get("bridge"))
        == ("EMOSA-WSC-backhaul", "none", "")
        and (inet or {}).get("inet_addr") == "169.254.2.1"
        and fronthaul.get("ssid") == REGISTRAR_SSID
        and unchanged
        and station == before_station
        and [op.get("bsses") for op in radio_operations(box, "5G")] == [["EMOSA-WSC-backhaul"]]
        and (status.get("gre_parent") or {}).get("ap") == PARENT_AP_50
        and reported.get(RUID_50) == [bssid]
        and RUID_24 in reported
    )
    return box.result(
        passed=passed,
        m1_ruids=ruids,
        m1_rf_bands=rf_bands,
        parent_ap={k: (ap or {}).get(k) for k in ("ssid", "multi_ap", "bridge")},
        radio_operations=status.get("radio_operations"),
        reported=reported,
        radio_unchanged=unchanged,
        station_unchanged=station == before_station,
    )


async def second_radio_no_power(box):
    """Spec 9, found on rdk-1010 (12 October): OpenSync leaves a radio's Wifi_Radio_State.tx_power
    unset when the radio's VIFs disagree (its station at 20 dBm, b-ap-50 at 30). The agent still
    answers the controller's channel procedures: a Channel Preference Query at once, from the
    capabilities, and a Channel Selection Request naming that radio, whose Operating Channel
    Report gives the radio's advertised maximum EIRP as a bound. Refused, RDK re-sent its query
    every second and renewed the agent every minute."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    uuid = next(
        u for u, r in (await box.rows("Wifi_Radio_State")).items() if r.get("freq_band") == "5G"
    )
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_Radio_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"tx_power": ["set", []]},
            }
        ]
    )
    await asyncio.sleep(2)  # the agent's refresh takes the pod's rows
    refused = (
        ((box.status() or {}).get("session") or {}).get("counts", {}).get("rejected_NOT_READY", 0)
    )
    queried = len(box.sent(0x8005))
    preference = await box.ask(0x8004, (), 0x8005, seconds=5)
    reported = len(box.sent(0x8008))
    radio_b = bytes.fromhex(RUID_50.replace(":", ""))
    selection = await box.ask(0x8006, (Tlv(0x8B, radio_b + b"\x00"),), 0x8007, seconds=5)
    report = await next_message(box, 0x8008, reported, seconds=10)
    responses = [t.value for t in selection.tlvs if t.kind == 0x8E] if selection else []
    operating = [t.value for t in report.tlvs if t.kind == 0x8F] if report else []
    radio_b_report = next((v for v in operating if v[:6] == radio_b), None)

    def bound_count():
        """operating_power_bound_reported: the reference counts its channel procedures apart,
        the C agent with the session's; the status file follows within a second or two."""
        session = (box.status() or {}).get("session") or {}
        channels = (session.get("channels") or {}).get("counts") or {}
        key = "operating_power_bound_reported"
        return (session.get("counts") or {}).get(key) or channels.get(key)

    bound = await box.until(bound_count, seconds=10)
    status = box.status() or {}
    counts = (status.get("session") or {}).get("counts") or {}
    return box.result(
        passed=preference is not None
        and bool(bound)
        and len(box.sent(0x8005)) - queried == 1
        and [(v[:6], v[6]) for v in responses] == [(radio_b, 0)]
        and radio_b_report is not None
        and (radio_b_report[7], radio_b_report[8], radio_b_report[9]) == (115, 44, 23)
        and counts.get("rejected_NOT_READY", 0) == refused,
        preference_answered=preference is not None,
        selection_codes=[v[6] for v in responses],
        radio_b_operating=radio_b_report.hex() if radio_b_report else None,
        not_ready_refusals=counts.get("rejected_NOT_READY", 0) - refused,
        bound_counted=bound,
    )


async def second_radio_child_credentials(box):
    """Spec 8.6 and 9, found by the rdk-1010 proof (12 October): a GRE child on the Pis' shape
    (its station bhaul-sta-50 on its 5 GHz radio, its uplink credentials from the controller's M2
    sets). Its 2.4 GHz radio maps no backhaul BSS, so the backhaul credentials are only in the
    5 GHz radio's own set. Once both radios' sets are applied the uplink has them and waits only
    for an upstream (a Backhaul Steering Request), not for credentials: before the fix the agent
    looked in the fronthaul's set alone and a steer onto a parent's b-ap-50 stalled there."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    radio_set = [op.get("bsses") for op in radio_operations(box, "5G")]
    fronthaul_set = [op.get("ssid") for op in (box.status() or {}).get("operations") or []]
    waiting = await box.until(
        lambda: (uplink_status(box).get("waiting") or "").startswith("no upstream") or None,
        seconds=15,
    )
    status = uplink_status(box)
    return box.result(
        passed=bool(waiting) and radio_set and radio_set[-1] == ["EMOSA-WSC-backhaul"],
        radio_set=radio_set,
        fronthaul_set=fronthaul_set,
        waiting=status.get("waiting"),
        station=status.get("station"),
    )


async def second_radio_withdrawn(box):
    """Spec 9 / 8.6 withdrawal by band: after a Renew the controller's 5 GHz set has no backhaul
    BSS (the operator dropped 5 GHz from the backhaul's bands). The 5 GHz radio's scope removes
    b-ap-50 and its underlay Inet row; the 2.4 GHz radio's configuration is not touched (its
    unchanged M2 set is a no-op), nor the pod's station on the 5 GHz radio."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    _, fronthaul = await box.vif("Wifi_VIF_Config", COLD_VIF)
    station = (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1]
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.radio_m2_modes[RUID_50] = ("configure",)
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))

    async def withdrawn():
        operations = radio_operations(box, "5G")
        gone = (await box.vif("Wifi_VIF_Config", PARENT_AP_50))[1] is None
        return gone and len(operations) == 2 and all_applied(operations)

    ruids = await serve_radios(box, withdrawn, searches=searches, m1s=m1s)
    if ruids is None:
        return box.result(passed=False, failed="b-ap-50 was not withdrawn")
    _, after = await box.vif("Wifi_VIF_Config", COLD_VIF)
    operations = radio_operations(box, "5G")
    return box.result(
        passed=ruids[:2] == [RUID_24, RUID_50]
        and await gre_inet(box, PARENT_AP_50) is None
        and after == fronthaul
        and (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1] == station
        and operations[-1].get("bsses") == []
        and all_applied((box.status() or {}).get("operations") or []),
        m1_ruids=ruids,
        radio_operations=operations,
        fronthaul_unchanged=after == fronthaul,
    )


def advertised_classes(m1):
    """{operating class: its non-operable channels} of the radio an M1's AP Radio Basic
    Capabilities TLV names (RUID, max BSS, class count, then class, max EIRP, count, channels)."""
    value = next(t.value for t in m1.tlvs if t.kind == 0x85)
    classes, at = {}, 8
    for _ in range(value[7]):
        count = value[at + 2]
        classes[value[at]] = list(value[at + 3 : at + 3 + count])
        at += 3 + count
    return classes


def recovery_starts(box):
    """How many sessions the agent has started (its recovery loop's attempts)."""
    session = (box.status() or {}).get("session") or {}
    return (session.get("recovery") or {}).get("attempts_started", 0)


async def second_radio_moves_channel(box):
    """Spec 9's channel rule in V54's shape (radio B on UNII-3 channel 149). With both radios
    configured, the pod's 5 GHz station roams to a parent on 149 and the radio, b-ap-50 on it,
    moves from 44 with it: the pod's doing, its State channel-less for a moment, then 149. The
    agent takes the radio's new capabilities in exactly one fresh session (its recovery starts one,
    with an M1 per radio, the 5 GHz one advertising 115, 124 and 125 with 149 operable in 124 and
    125; a Search answered late may be sent again within it); both
    radios' unchanged M2 sets are observed applied without writing, so b-ap-50, the fronthaul
    and the radio's Config are left as they were, and no further session follows."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    ap = (await box.vif("Wifi_VIF_Config", PARENT_AP_50))[1]
    fronthaul = (await box.vif("Wifi_VIF_Config", COLD_VIF))[1]
    radio = (await radio_config(box, "5G"))[1]
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    before = len((box.status() or {}).get("operations") or []), len(radio_operations(box, "5G"))
    starts = recovery_starts(box)
    state = next(
        u for u, r in (await box.rows("Wifi_Radio_State")).items() if r.get("freq_band") == "5G"
    )
    for channel in (["set", []], 149):  # between two channels, then on the new parent's
        await box.transact(
            [
                {
                    "op": "update",
                    "table": "Wifi_Radio_State",
                    "where": [["_uuid", "==", ["uuid", state]]],
                    "row": {"channel": channel},
                }
            ]
        )
        await asyncio.sleep(2)

    async def taken_again():
        ops, five = (box.status() or {}).get("operations") or [], radio_operations(box, "5G")
        return (
            len(ops) > before[0]
            and len(five) > before[1]
            and all_applied(ops)
            and all_applied(five)
        )

    ruids = await serve_radios(box, taken_again, searches=searches, m1s=m1s)
    if ruids is None:
        return box.result(passed=False, failed="the radio's new capabilities were not taken")
    await asyncio.sleep(10)  # no further session follows
    status = box.status() or {}
    radio_b_m1 = next(
        (
            m1
            for m1 in box.sent(AUTOCONFIG_WSC)[m1s:]
            if next(t.value[:6] for t in m1.tlvs if t.kind == 0x85).hex(":") == RUID_50
        ),
        None,
    )
    classes = advertised_classes(radio_b_m1) if radio_b_m1 else {}
    noop = [
        bool((ops[-1].get("application_evidence") or {}).get("observed_noop"))
        for ops in (status.get("operations") or [{}], radio_operations(box, "5G") or [{}])
    ]
    unchanged = {
        "b-ap-50": (await box.vif("Wifi_VIF_Config", PARENT_AP_50))[1] == ap,
        "fronthaul": (await box.vif("Wifi_VIF_Config", COLD_VIF))[1] == fronthaul,
        "radio": (await radio_config(box, "5G"))[1] == radio,
    }
    sessions = recovery_starts(box) - starts
    radios = (status.get("pod") or {}).get("radios") or []
    reported = {r.get("band"): r.get("channel") for r in radios}
    # one session: the agent's recovery started exactly one, its radios' M1s in order. Within it a
    # Search or an M1 may go again when its answer is late (the box under load answered a Search
    # after more than the agent's 1 s, 12 Oct): a retransmission, not another session.
    return box.result(
        passed=sessions == 1
        and list(dict.fromkeys(ruids)) == [RUID_24, RUID_50]
        and classes
        == {
            115: [36, 40, 44, 48],
            124: [153, 157, 161],
            125: [153, 157, 161, 165, 169, 173, 177],
        }
        and all(noop)
        and all(unchanged.values())
        and reported.get("5G") == 149,
        searches=len(box.sent(AUTOCONFIG_SEARCH)) - searches,
        sessions=sessions,
        m1_ruids=ruids,
        radio_b_classes={str(k): v for k, v in classes.items()},
        observed_noop=noop,
        unchanged=unchanged,
        reported_channels=reported,
    )


async def renew_on_bootstrap(box):
    """V54 regression guard (the MV3's controller restart, 12 October): a GRE child of the Pis'
    shape on its bootstrap, both radios configured. The controller restarts: a Renew, and the
    pod's station dropped by the router for a moment, so the station has no parent and its 5 GHz
    radio, whose channel follows the station, shows none, then the same one again. The agent
    onboards once, for the Renew (the held channel covers the moment, spec 9): one session, both
    radios' unchanged M2 sets observed applied without writing, and none of the pod's station,
    credential or Inet rows touched."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    station = (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1]
    credentials = await box.rows("Wifi_Credential_Config")
    inet = await box.rows("Wifi_Inet_Config")
    starts = recovery_starts(box)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    before = len((box.status() or {}).get("operations") or []), len(radio_operations(box, "5G"))
    box.controller.send(RENEW, (Tlv(0x01, box.controller.al), Tlv(0x0F, b"\0"), Tlv(0x10, b"\0")))
    radios = (await box.rows("Wifi_Radio_State")).items()
    radio, radio_row = next((u, r) for u, r in radios if r.get("freq_band") == "5G")
    vif, vif_row = await box.vif("Wifi_VIF_State", "bhaul-sta-50")
    flap = [  # dropped by the router, then back on the same parent and channel
        ({"channel": ["set", []]}, {"parent": ["set", []]}),
        ({"channel": radio_row.get("channel")}, {"parent": vif_row.get("parent") or ["set", []]}),
    ]
    for radio_change, vif_change in flap:
        await box.transact(
            [
                {
                    "op": "update",
                    "table": "Wifi_Radio_State",
                    "where": [["_uuid", "==", ["uuid", radio]]],
                    "row": radio_change,
                },
                {
                    "op": "update",
                    "table": "Wifi_VIF_State",
                    "where": [["_uuid", "==", ["uuid", vif]]],
                    "row": vif_change,
                },
            ]
        )
        await asyncio.sleep(0.5)

    async def taken_again():
        ops, five = (box.status() or {}).get("operations") or [], radio_operations(box, "5G")
        more = len(ops) > before[0] and len(five) > before[1]
        return more and all_applied(ops) and all_applied(five)

    ruids = await serve_radios(box, taken_again, searches=searches, m1s=m1s)
    if ruids is None:
        return box.result(passed=False, failed="the Renew's session did not configure both radios")
    await asyncio.sleep(10)  # no further session follows
    status = box.status() or {}
    noop = [
        bool((ops[-1].get("application_evidence") or {}).get("observed_noop"))
        for ops in (status.get("operations") or [{}], radio_operations(box, "5G") or [{}])
    ]
    unchanged = {
        "station": (await box.vif("Wifi_VIF_Config", "bhaul-sta-50"))[1] == station,
        "credentials": await box.rows("Wifi_Credential_Config") == credentials,
        "inet": await box.rows("Wifi_Inet_Config") == inet,
    }
    sessions = recovery_starts(box) - starts
    return box.result(
        passed=sessions == 1
        and list(dict.fromkeys(ruids)) == [RUID_24, RUID_50]
        and all(noop)
        and all(unchanged.values()),
        sessions=sessions,
        m1_ruids=ruids,
        observed_noop=noop,
        unchanged=unchanged,
        uplink_waiting=uplink_status(box).get("waiting"),
    )


def advertised_max_eirp(m1):
    """(RUID, [max EIRP of each class]) of the radio an M1's AP Radio Basic Capabilities name."""
    value = next(t.value for t in m1.tlvs if t.kind == 0x85)
    eirps, at = [], 8
    for _ in range(value[7]):
        eirps.append(value[at + 1])
        at += 3 + value[at + 2]
    return value[:6].hex(":"), eirps


CONFIG_TABLES = (
    "Wifi_VIF_Config",
    "Wifi_Inet_Config",
    "Wifi_Credential_Config",
    "Wifi_Radio_Config",
)


async def set_radio_power(box, power):
    """Every radio's Wifi_Radio_State.tx_power in one transaction, as the pod's managers fill it."""
    await box.transact(
        [
            {
                "op": "update",
                "table": "Wifi_Radio_State",
                "where": [["_uuid", "==", ["uuid", uuid]]],
                "row": {"tx_power": power},
            }
            for uuid in await box.rows("Wifi_Radio_State")
        ]
    )


async def power_rises(box):
    """Spec 2.4, F-201 (the Pis on their new mt76 modules, 12 October): a pod whose source began
    before it reported its radios' power (the agent advertised its own or 20 dBm) reports more,
    30 dBm, after onboarding. Each radio's capabilities are taken again with the higher maximum
    EIRP: exactly one fresh session, its M1s advertising 30 dBm, its unchanged M2 sets observed
    applied without writing, no Config row of the pod changed and its uplink untouched. The
    channel procedures are then answered (before, refused while the power exceeded the advertised
    maximum: RDK renewed the agent every 40 s). A fall to 23 dBm changes nothing."""
    if await serve_radios(box, lambda: both_radios_applied(box)) is None:
        return box.result(passed=False, failed="the two radios were not both configured")
    before_m1s = {ruid: eirps for ruid, eirps in map(advertised_max_eirp, box.sent(AUTOCONFIG_WSC))}
    config = {t: await box.rows(t) for t in CONFIG_TABLES}
    uplink = {k: uplink_status(box).get(k) for k in ("waiting", "uplink", "in_use", "parent")}
    starts = recovery_starts(box)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    before = len((box.status() or {}).get("operations") or []), len(radio_operations(box, "5G"))
    await set_radio_power(box, 30)

    async def taken_again():
        ops, five = (box.status() or {}).get("operations") or [], radio_operations(box, "5G")
        more = len(ops) > before[0] and len(five) > before[1]
        return more and all_applied(ops) and all_applied(five)

    ruids = await serve_radios(box, taken_again, searches=searches, m1s=m1s)
    if ruids is None:
        return box.result(
            passed=False, failed="no fresh session for the risen power", advertised=before_m1s
        )
    after_m1s = {
        ruid: eirps for ruid, eirps in map(advertised_max_eirp, box.sent(AUTOCONFIG_WSC)[m1s:])
    }
    radio_a = bytes.fromhex(RUID_24.replace(":", ""))
    reported = len(box.sent(0x8008))
    selection = await box.ask(0x8006, (Tlv(0x8B, radio_a + b"\x00"),), 0x8007, seconds=5)
    report = await next_message(box, 0x8008, reported, seconds=10)
    responses = [t.value for t in selection.tlvs if t.kind == 0x8E] if selection else []
    operating = [t.value for t in report.tlvs if t.kind == 0x8F] if report else []
    radio_a_report = next((v for v in operating if v[:6] == radio_a), None)
    sessions = recovery_starts(box) - starts
    await set_radio_power(box, 23)  # a fall: nothing
    await asyncio.sleep(10)
    status = box.status() or {}
    noop = [
        bool((ops[-1].get("application_evidence") or {}).get("observed_noop"))
        for ops in (status.get("operations") or [{}], radio_operations(box, "5G") or [{}])
    ]
    unchanged = {t: await box.rows(t) == rows for t, rows in config.items()}
    uplink_after = {k: uplink_status(box).get(k) for k in uplink}
    return box.result(
        passed=sessions == 1
        and recovery_starts(box) - starts == 1
        and list(dict.fromkeys(ruids)) == [RUID_24, RUID_50]
        and all(set(e) == {30} for e in after_m1s.values())
        and set(after_m1s) == {RUID_24, RUID_50}
        and [(v[:6], v[6]) for v in responses] == [(radio_a, 0)]
        and radio_a_report is not None
        and radio_a_report[9] == 30
        and all(noop)
        and all(unchanged.values())
        and uplink_after == uplink,
        advertised_before=before_m1s,
        advertised_after=after_m1s,
        sessions_on_the_rise=sessions,
        sessions_after_the_fall=recovery_starts(box) - starts - sessions,
        selection_codes=[v[6] for v in responses],
        radio_a_operating=radio_a_report.hex() if radio_a_report else None,
        observed_noop=noop,
        config_unchanged=unchanged,
        uplink_unchanged=uplink_after == uplink,
    )


async def adapter_restart(box):
    """The agent process killed and started again (its journal kept): it takes the pod back,
    onboards again and finds the configuration already running, without writing (spec 5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    box.process.kill()
    box.process.wait(10)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.restart_agent()
    again = await provision(box, searches, m1s)
    operations = (box.status() or {}).get("operations") or []
    last = operations[-1] if operations else {}
    noop = bool((last.get("application_evidence") or {}).get("observed_noop"))
    return box.result(
        passed=again and last.get("state") == "OBSERVED_APPLIED" and noop,
        provisioning_again=again,
        operations=[op.get("state") for op in operations],
        observed_without_writing=noop,
    )


async def transport_cut(box):
    """The pod's management connection to the agent cut for 20 s (the pod's database drops
    the remote): the agent loses its source, and when the pod dials again it onboards anew
    and is provisioning, the configuration found running (spec 2.5)."""
    if not (await onboarded(box)).get("applied"):
        return box.result(passed=False, failed="not onboarded")
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}", connect=False)
    states = set()

    def lost_source():
        state = ((box.status() or {}).get("session") or {}).get("state")
        states.add(state)
        return state not in {"provisioning", None}

    lost = await box.until(lost_source, seconds=30)
    await asyncio.sleep(20)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await dial(box.db, f"tcp:127.0.0.1:{AGENT_PORT}")
    again = await provision(box, searches, m1s)
    return box.result(
        passed=bool(lost) and again,
        source_lost_seen=bool(lost),
        states_during_cut=sorted(s for s in states if s),
        provisioning_again=again,
    )


async def write_lost_unapplied(box):
    """spec 5 (finding 22): the pod's link lost while the agent's write commits (the box's
    ReplyCut): the write is in the pod's Config, its reply lost (INDETERMINATE), and the pod's
    State never shows it (its managers do not apply it). Past its deadline (120 s) the
    operation ends TIMED_OUT and no longer blocks the pod: the agent onboards again, the next
    M2 is written, and once the pod's managers apply that one it is observed applied."""
    deadline = time.monotonic() + 330
    searches = m1s = 0
    first, seen, applying = None, {}, False
    while time.monotonic() < deadline and box.process.poll() is None:
        found = box.sent(AUTOCONFIG_SEARCH)
        for search in found[searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[m1s:]:
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            await answer_m1(box, ruid, wsc)
        m1s = len(found)
        for op in (box.status() or {}).get("operations") or []:
            if isinstance(op, dict) and op.get("operation_id"):
                seen[op["operation_id"]] = (op.get("state"), op.get("reason"))
        if first is None:
            first = next((i for i, (s, _) in seen.items() if s == "INDETERMINATE"), None)
        later = {i: s for i, (s, _) in seen.items() if first and i != first}
        if not applying and any(
            s in ("CONFIG_COMMITTED", "OBSERVED_APPLIED") for s in later.values()
        ):
            applying = True
            await apply_configuration(box.db)  # the pod's managers apply the later write
        if "OBSERVED_APPLIED" in later.values():
            break
        await asyncio.sleep(0.5)
    first_state = seen.get(first, (None, None))
    busy = sum(1 for s, r in seen.values() if s == "REJECTED" and r == "BUSY")
    return box.result(
        passed=bool(first)
        and first_state[0] == "TIMED_OUT"
        and "OBSERVED_APPLIED" in [s for i, (s, _) in seen.items() if i != first],
        reply_cut=box.reply_cut.done,
        first_operation=first_state,
        rejected_busy=busy,
        later_applied=applying and "OBSERVED_APPLIED" in later.values(),
    )


def operation_states(box):
    """[(state, reason)] of the agent's operations, in order."""
    return [
        (op.get("state"), op.get("reason"))
        for op in (box.status() or {}).get("operations") or []
        if isinstance(op, dict)
    ]


async def restart_during_write(box):
    """spec 5 (found by the box, 12 October): the pod's OpenSync starts again between the agent's
    write and its observation. The controller's first M2 is written (CONFIG_COMMITTED) but the
    pod's managers have not applied it when the pod's database comes back from its template.
    That write went with its start, so it fails at once (NOT_READY) instead of holding the pod
    until its deadline: the new start's M2 is written, not refused BUSY, and observed applied."""
    before, searches, m1s = 0, 0, 0
    deadline = time.monotonic() + 60
    committed = False
    while time.monotonic() < deadline and box.process.poll() is None and not committed:
        found = box.sent(AUTOCONFIG_SEARCH)
        for search in found[searches:]:
            box.controller.send(AUTOCONFIG_RESPONSE, RESPONSE_61, mid=search.mid)
        searches = len(found)
        found = box.sent(AUTOCONFIG_WSC)
        for m1 in found[m1s:]:  # answered, and the write left unapplied by the pod's managers
            wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
            ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)
            await answer_m1(box, ruid, wsc)
        m1s = len(found)
        states = operation_states(box)
        committed = len(states) > before and states[-1][0] == "CONFIG_COMMITTED"
        await asyncio.sleep(0.2)
    if not committed:
        return box.result(passed=False, failed="the first write was not committed")
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.new_pod_database()  # its OpenSync again, before the write was observed
    again = await provision(box, searches, m1s)
    await box.until(lambda: operation_states(box)[-1:] == [("OBSERVED_APPLIED", None)], seconds=30)
    states = operation_states(box)
    interrupted = states[before] if len(states) > before else None
    return box.result(
        passed=bool(again)
        and interrupted == ("FAILED", "NOT_READY")
        and states[-1] == ("OBSERVED_APPLIED", None)
        and ("REJECTED", "BUSY") not in states[before:],
        provisioned_again=again,
        interrupted_write=interrupted,
        operations=states[before:],
    )


SCENARIOS = {
    "boot": boot,
    "onboard": onboard,
    "refuse": refuse,
    "answers": answers,
    "early-report": early_report,
    "renew": renew,
    "no-m2": no_m2,
    "silent-controller": silent_controller,
    "forgotten-agent": forgotten_agent,
    "agent-netns": agent_netns,
    "unserved-pod": unserved_pod,
    "new-source": new_source,
    "pod-recreated": pod_recreated,
    "clients": clients,
    "reannounce": reannounce,
    "channel-selection": channel_selection,
    "channel-scan": channel_scan,
    "telemetry": telemetry,
    "foreign-broker": foreign_broker,
    "metrics": metrics,
    "unassociated": unassociated,
    "steering": steering,
    "steering-leftover": steering_leftover,
    "steering-close-refused": steering_close_refused,
    "steering-refusals": steering_refusals,
    "backhaul-capability": backhaul_capability,
    "backhaul-steering": backhaul_steering,
    "revert-by-record": revert_by_record,
    "restart-during-move": restart_during_move,
    "fallback-during-move": fallback_during_move,
    "gre-child-kept": gre_child_kept,
    "gre-child-kept-gone": gre_child_kept_gone,
    "backhaul-steering-renewal": backhaul_steering_renewal,
    "backhaul-steering-refused": backhaul_steering_refused,
    "backhaul-steering-own-bss": backhaul_steering_own_bss,
    "backhaul-steering-across-bands": backhaul_steering_across_bands,
    "backhaul-steering-across-bands-fails": backhaul_steering_across_bands_fails,
    "backhaul-steering-no-station-on-band": backhaul_steering_no_station_on_band,
    "backhaul-steering-channel-not-operable": backhaul_steering_channel_not_operable,
    "pod-child": pod_child,
    "pod-parent": pod_parent,
    "pod-parent-loop": pod_parent_loop,
    "gre-parent-create": gre_parent_create,
    "gre-parent-child": gre_parent_child,
    "gre-parent-child-gone": gre_parent_child_gone,
    "gre-parent-child-redrift": gre_parent_child_redrift,
    "gre-parent-withdrawn": gre_parent_withdrawn,
    "second-radio-parent": second_radio_parent,
    "second-radio-withdrawn": second_radio_withdrawn,
    "second-radio-child-credentials": second_radio_child_credentials,
    "second-radio-no-power": second_radio_no_power,
    "second-radio-moves-channel": second_radio_moves_channel,
    "renew-on-bootstrap": renew_on_bootstrap,
    "power-rises": power_rises,
    "gre-parent-promoted": gre_parent_promoted,
    "gre-parent-demoted": gre_parent_demoted,
    "adapter-restart": adapter_restart,
    "transport-cut": transport_cut,
    "write-lost-unapplied": write_lost_unapplied,
    "restart-during-write": restart_during_write,
}
OPTIONS = {  # the box each scenario needs beyond the default
    "write-lost-unapplied": {"reply_cut": "lost-write"},
    "steering-close-refused": {"reply_cut": "refused-close"},
    "forgotten-agent": {"topology_query_window": 20},
    "agent-netns": {"netns": True},
    "telemetry": {"telemetry": True},
    "foreign-broker": {"telemetry": True, "foreign_broker": True},
    "metrics": {"telemetry": True},
    "unassociated": {"telemetry": True},
    "backhaul-capability": {"backhaul": True},
    "backhaul-steering": {"backhaul": True},
    "revert-by-record": {
        "backhaul": True,
        "bootstrap": True,
        "uplink": {
            "mode": "gre",
            "station": "bhaul-sta-24",
            "credentials": "config",
            "ssid": "emosa-lab-bh",
            "secret_ref": "backhaul",
        },
    },
    "restart-during-move": {
        "backhaul": True,
        "bootstrap": True,
        "uplink": {
            "mode": "gre",
            "station": "bhaul-sta-24",
            "credentials": "config",
            "ssid": "emosa-lab-bh",
            "secret_ref": "backhaul",
        },
    },
    "fallback-during-move": {
        "backhaul": True,
        "bootstrap": True,
        "uplink": {
            "mode": "gre",
            "station": "bhaul-sta-24",
            "credentials": "config",
            "ssid": "emosa-lab-bh",
            "secret_ref": "backhaul",
        },
    },
    **{
        name: {
            "backhaul": True,
            "bootstrap": True,
            "uplink": {
                "mode": "gre",
                "station": "bhaul-sta-24",
                "credentials": "config",
                "ssid": "emosa-lab-bh",
                "secret_ref": "backhaul",
            },
        }
        for name in ("gre-child-kept", "gre-child-kept-gone")
    },
    "backhaul-steering-renewal": {"backhaul": True},
    "backhaul-steering-own-bss": {"backhaul": True},
    "backhaul-steering-across-bands": {"backhaul": True},
    "backhaul-steering-across-bands-fails": {"backhaul": True},
    "backhaul-steering-no-station-on-band": {"backhaul": True},
    "backhaul-steering-channel-not-operable": {"backhaul": True},
    "pod-child": {"backhaul": True},
    "pod-parent": {"multi_bss": True, "telemetry": True},
    "pod-parent-loop": {"backhaul": True},
    "gre-parent-create": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "gre-parent-child": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "gre-parent-child-gone": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "gre-parent-child-redrift": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "gre-parent-withdrawn": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    "second-radio-parent": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
    },
    "second-radio-withdrawn": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
    },
    # a GRE child of the Pis' shape, its uplink credentials from the M2 sets (spec 8.6, 9)
    "second-radio-child-credentials": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
        "backhaul": True,
        "gre_child": True,
        "uplink": {"mode": "gre", "station": "bhaul-sta-50"},
    },
    "second-radio-no-power": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
    },
    "second-radio-moves-channel": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
        "underlay": GRE_UNDERLAY,
    },
    **{
        name: {
            "multi_bss": True,
            "profile": "opensync-lab-hwsim-6.6.1-gre-5g-v1",
            "underlay": GRE_UNDERLAY,
            "backhaul": True,
            "gre_child": True,
            "uplink": {"mode": "gre", "station": "bhaul-sta-50"},
        }
        for name in ("renew-on-bootstrap", "power-rises")
    },
    # a GRE parent with a child, then demoted to the plain profile
    "gre-parent-demoted": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-gre-v1",
        "underlay": GRE_UNDERLAY,
    },
    # onboarded on the plain profile, then promoted to GRE parent (finding A)
    "gre-parent-promoted": {
        "multi_bss": True,
        "profile": "opensync-lab-hwsim-6.6.1-v1",
        "underlay": GRE_UNDERLAY,
    },
}


async def run(scenario, agent, directory, *, binary=None):
    from emosa_lab import box_features

    if scenario in box_features.SCENARIOS:  # the features' faults and refusals (plan 8.4)
        options = box_features.OPTIONS.get(scenario, {})
        async with Box(agent, directory, binary=binary, **options) as box:
            return await box_features.SCENARIOS[scenario](box)
    if scenario not in SCENARIOS:  # the fleet's and the GTP's (box_adapter)
        from emosa_lab import box_adapter

        return await box_adapter.run(scenario, agent, directory, binary=binary)
    async with Box(agent, directory, binary=binary, **OPTIONS.get(scenario, {})) as box:
        return await SCENARIOS[scenario](box)


def main():
    from emosa_lab import box_adapter, box_features

    names = {*SCENARIOS, *box_adapter.SCENARIOS, *box_adapter.STANDALONE, *box_features.SCENARIOS}
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=sorted(names))
    parser.add_argument("--agent", choices=["c", "python"], required=True)
    parser.add_argument("--binary", help="the C agent (default c/build/emosa-agent-c)")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    in_namespace()
    result = asyncio.run(
        run(args.scenario, args.agent, args.directory.resolve(), binary=args.binary)
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
