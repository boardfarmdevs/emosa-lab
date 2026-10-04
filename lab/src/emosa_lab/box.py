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
import json
import os
import socket
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
from emosa_lab.simulation.database import SimDatabase
from emosa_lab.simulation.manager import APPLY_COLUMNS
from emosa_lab.simulation.wsc_provisioning import registrar_reply

ROOT = Path(__file__).resolve().parents[3]
POD_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-tables.json"
# another pod, on a Multi-AP Wi-Fi backhaul (bhaul-sta-24 on emosa-lab-bh, data plane option 1)
MULTI_AP_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-uplink-multi-ap.json"
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


def recorded_pod(rows=POD_ROWS):
    """The recorded pod's tables as one insert transaction (references by name)."""
    tables = json.loads(Path(rows).read_text())["tables"]
    names = {uuid: f"row{i}" for i, uuid in enumerate(u for rows in tables.values() for u in rows)}

    def named(value):
        if isinstance(value, list) and len(value) == 2 and value[0] == "uuid":
            return ["named-uuid", names[value[1]]] if value[1] in names else value
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
        multi_bss=False,
        uplink=None,
    ):
        self.agent, self.directory, self.binary = agent, directory, binary
        self.telemetry, self.foreign_broker, self.backhaul = telemetry, foreign_broker, backhaul
        # multi_bss: the controller answers each M1 with an M2 set, a fronthaul and a backhaul
        # BSS (the registrar's backhaul mode); uplink: the agent's uplink setting instead of
        # UPLINK (with backhaul)
        self.multi_bss, self.uplink = multi_bss, uplink
        self.m2_modes = ("configure", "backhaul") if multi_bss else ("configure",)
        self.pod_rows = MULTI_AP_ROWS if backhaul else POD_ROWS
        self.process = self.db = self.controller = self.log = self.broker = None
        self.interface = AGENT_INTERFACE  # the agent's 1905 interface (a fleet names its own)

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        network(self.interface)
        tables, operations = recorded_pod(self.pod_rows)
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
        if self.backhaul:  # option 1, its credentials from the agent's own configuration
            fleet["uplink"] = self.uplink or UPLINK
            secrets = state_root / self.serial / "secrets"
            secrets.mkdir(parents=True, mode=0o700)
            (secrets / "backhaul").write_text("recorded-psk-replaced")
            (secrets / "backhaul").chmod(0o600)
        config = agent_config(entry, fleet)
        # the agent's interface carries its AL MAC, as the labs' macvlan per agent does
        subprocess.run(["ip", "link", "set", self.interface, "address", self.al_mac], check=True)
        state_root.mkdir(mode=0o700, exist_ok=True)  # the fleet's state_root
        self.status_path = state_root / self.serial / "status.json"
        self.controller = Controller(CONTROLLER_INTERFACE, self.al_mac)
        self.env = {
            **os.environ,
            "EMOSA_SCHEMAS": str(ROOT / "schemas"),
            "EMOSA_PROFILES": str(ROOT / "src/emosa/profiles"),
        }
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
        await dial(self.db, f"tcp:127.0.0.1:{AGENT_PORT}")

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
        if self.broker and self.broker.poll() is None:
            self.broker.terminate()
            self.broker.wait(5)

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
        schema = json.loads(Path(reference_path()).read_text())
        columns = sorted(schema["tables"][table]["columns"])
        session = OvsSession(self.db.endpoint, read_only=True, monitor_columns={table: columns})
        try:
            snapshot = await session.snapshot()
        finally:
            await session.close()
        return snapshot["tables"].get(table, {})

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
        session = OvsSession(self.db.endpoint)
        try:
            return await session.transact(operations)
        finally:
            await session.close()

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

    async def new_pod_database(self):
        """The pod's OpenSync started again (its container with its extender, say): a new
        database from the template, with new row UUIDs, dialing the agent again."""
        old = self.db
        await old.close()
        tables, operations = recorded_pod(self.pod_rows)
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
    m2s = [await asyncio.to_thread(registrar_reply, REGISTRAR, wsc, mode) for mode in box.m2_modes]
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


def backhaul_steering_request(station, target=TARGET_PARENT):
    """Backhaul Steering Request TLV (0x9E): the backhaul station, the target BSS and its
    operating class and channel."""
    return Tlv(
        0x9E,
        bytes.fromhex(station.replace(":", ""))
        + bytes.fromhex(target.replace(":", ""))
        + b"\x51\x06",
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


async def backhaul_on(box):
    """Onboarded, and the uplink switch (option 1) applied: the station pinned to its parent."""
    if not (await onboarded(box)).get("applied"):
        return False

    def applied():
        operation = uplink_status(box).get("operation") or {}
        return operation.get("state") == "OBSERVED_APPLIED"

    return bool(await box.until(applied, seconds=60))


async def backhaul_capability(box):
    """On a Multi-AP backhaul the Backhaul STA Capability Report names the pod's backhaul
    station (spec 2.4, 8.3); the uplink switch pins it to the configured parent."""
    on = await backhaul_on(box)
    report = await box.ask(0x8027, (), 0x8028)
    value = tlv_value(report, 0xCB) or b""
    station = value[7:13].hex(":") if len(value) >= 13 and value[6] & 0x80 else None
    return box.result(
        passed=on and station == BACKHAUL_STATION and await pinned(box, BACKHAUL_PARENT),
        uplink_applied=on,
        station=station,
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


# The reference workload's faults, in the box


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


SCENARIOS = {
    "boot": boot,
    "onboard": onboard,
    "refuse": refuse,
    "answers": answers,
    "early-report": early_report,
    "renew": renew,
    "no-m2": no_m2,
    "silent-controller": silent_controller,
    "unserved-pod": unserved_pod,
    "new-source": new_source,
    "clients": clients,
    "reannounce": reannounce,
    "channel-selection": channel_selection,
    "channel-scan": channel_scan,
    "telemetry": telemetry,
    "foreign-broker": foreign_broker,
    "metrics": metrics,
    "unassociated": unassociated,
    "steering": steering,
    "steering-refusals": steering_refusals,
    "backhaul-capability": backhaul_capability,
    "backhaul-steering": backhaul_steering,
    "backhaul-steering-renewal": backhaul_steering_renewal,
    "backhaul-steering-refused": backhaul_steering_refused,
    "backhaul-steering-own-bss": backhaul_steering_own_bss,
    "adapter-restart": adapter_restart,
    "transport-cut": transport_cut,
}
OPTIONS = {  # the box each scenario needs beyond the default
    "telemetry": {"telemetry": True},
    "foreign-broker": {"telemetry": True, "foreign_broker": True},
    "metrics": {"telemetry": True},
    "unassociated": {"telemetry": True},
    "backhaul-capability": {"backhaul": True},
    "backhaul-steering": {"backhaul": True},
    "backhaul-steering-renewal": {"backhaul": True},
    "backhaul-steering-own-bss": {"backhaul": True},
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
