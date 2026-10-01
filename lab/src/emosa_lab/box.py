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
from emosa.opensync.session import OvsSession
from emosa.wire.cmdu import Reassembler, Tlv, fragment_message
from emosa_lab.simulation.database import SimDatabase
from emosa_lab.simulation.manager import APPLY_COLUMNS
from emosa_lab.simulation.wsc_provisioning import registrar_reply

ROOT = Path(__file__).resolve().parents[3]
POD_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-tables.json"
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


def network():
    """lo, and the veth pair between the agent's interface and the controller's."""
    for command in (
        ["ip", "link", "set", "lo", "up"],
        [
            "ip",
            "link",
            "add",
            AGENT_INTERFACE,
            "type",
            "veth",
            "peer",
            "name",
            CONTROLLER_INTERFACE,
        ],
        ["ip", "link", "set", AGENT_INTERFACE, "up"],
        ["ip", "link", "set", CONTROLLER_INTERFACE, "up"],
    ):
        subprocess.run(command, check=True)


def recorded_pod():
    """The recorded pod's tables as one insert transaction (references by name)."""
    tables = json.loads(POD_ROWS.read_text())["tables"]
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


async def dial(db, endpoint):
    """Make the pod's database connect to the agent, as a pod does after its redirect."""

    def command():
        error, stream = ovs.stream.Stream.open_block(
            ovs.stream.Stream.open("unix:" + str(db.directory / "control.sock")), 5000
        )
        if error:
            raise RuntimeError("the pod's database has no control socket")
        rpc = ovs.jsonrpc.Connection(stream)
        try:
            error, reply = rpc.transact_block(
                ovs.jsonrpc.Message.create_request("ovsdb-server/add-remote", [endpoint])
            )
            if error or reply.error is not None:
                raise RuntimeError("the pod's database did not take the remote")
        finally:
            rpc.close()

    await asyncio.to_thread(command)


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
        self.messages = []
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

    def latest(self, message_type):
        found = [m for m in self.messages if m.message_type == message_type]
        return found[-1] if found else None

    def send(self, message_type, tlvs, mid=None):
        if mid is None:
            self.mid = (self.mid + 1) & 0xFFFF
            mid = self.mid
        for frame in fragment_message(self.agent, self.al, message_type, mid, tuple(tlvs)):
            self.socket.send(frame)

    def close(self):
        self.socket.close()


class Box:
    """The agent, its pod and its controller, started in the current network namespace."""

    def __init__(self, agent, directory, *, binary=None):
        self.agent, self.directory, self.binary = agent, directory, binary
        self.process = self.db = self.controller = self.log = None

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        network()
        tables, operations = recorded_pod()
        self.serial = next(iter(tables["AWLAN_Node"].values()))["serial_number"]
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
            "interface": AGENT_INTERFACE,
            "al_mac": derive_al(self.serial, [CONTROLLER_AL]),
        }
        self.al_mac = entry["al_mac"]
        state_root = self.directory / "state"
        config = agent_config(
            entry, {"controller_al": CONTROLLER_AL, "state_root": str(state_root)}
        )
        # the agent's interface carries its AL MAC, as the labs' macvlan per agent does
        subprocess.run(["ip", "link", "set", AGENT_INTERFACE, "address", self.al_mac], check=True)
        state_root.mkdir(mode=0o700, exist_ok=True)  # the fleet's state_root
        self.status_path = state_root / self.serial / "status.json"
        config_path = self.directory / "agent.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        self.controller = Controller(CONTROLLER_INTERFACE, self.al_mac)
        env = {
            **os.environ,
            "EMOSA_SCHEMAS": str(ROOT / "schemas"),
            "EMOSA_PROFILES": str(ROOT / "src/emosa/profiles"),
        }
        self.log = (self.directory / "agent.log").open("w")
        self.process = subprocess.Popen(
            agent_command(self.agent, config_path, self.binary),
            stdout=self.log,
            stderr=self.log,
            env=env,
        )
        await asyncio.sleep(1)
        await dial(self.db, f"tcp:127.0.0.1:{AGENT_PORT}")
        return self

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
        session = OvsSession(self.db.endpoint, read_only=True)
        try:
            snapshot = await session.snapshot()
        finally:
            await session.close()
        return snapshot["tables"].get(table, {})

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
    """The agent takes the pod's connection and searches for its controller."""
    searched = await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH))
    return box.result(passed=bool(searched))


async def onboard(box):
    """The controller answers: the Response, then an M2 from hostap's registrar for the
    agent's own M1. The agent writes the credentials to the pod, the pod's manager
    applies them, and the agent sees them applied."""
    search = await box.until(lambda: box.controller.latest(AUTOCONFIG_SEARCH))
    if not search:
        return box.result(passed=False, failed="no search")
    # an EasyMesh 6.1 Response: registrar, 2.4 GHz, the controller service, Profile 1, and
    # Controller Capability with KiB/MiB counters and the Early AP Capability Report
    response = (
        Tlv(0x0F, b"\0"),
        Tlv(0x10, b"\0"),
        Tlv(0x80, b"\x01\0"),
        Tlv(0xB3, b"\x01"),
        Tlv(0xDD, b"\xc0"),
    )
    box.controller.send(AUTOCONFIG_RESPONSE, response, mid=search.mid)
    m1 = await box.until(lambda: box.controller.latest(AUTOCONFIG_WSC))
    if not m1:
        return box.result(passed=False, failed="no M1")
    wsc = next(t.value for t in m1.tlvs if t.kind == 0x11)
    ruid = next(t.value[:6] for t in m1.tlvs if t.kind == 0x85)  # AP Radio Basic Capabilities
    m2 = await asyncio.to_thread(registrar_reply, REGISTRAR, wsc)
    box.controller.send(AUTOCONFIG_WSC, (Tlv(0x82, ruid), Tlv(0x11, m2)))

    async def written():
        rows = await box.rows("Wifi_VIF_Config")
        return any(r.get("ssid") == REGISTRAR_SSID for r in rows.values())

    deadline = time.monotonic() + 60
    wrote = False
    while time.monotonic() < deadline and box.process.poll() is None and not wrote:
        box.controller.poll()
        wrote = await written()
        await asyncio.sleep(0.3)
    if not wrote:
        return box.result(passed=False, failed="the credentials never reached the pod")
    await apply_configuration(box.db)  # the pod's managers apply the new configuration

    def applied():
        status = box.status() or {}
        operations = status.get("operations") or []
        return any(
            "APPLIED" in str(op.get("state", "")) for op in operations if isinstance(op, dict)
        )

    done = await box.until(applied, seconds=60)
    return box.result(
        passed=bool(done), wrote=wrote, operations=(box.status() or {}).get("operations")
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


SCENARIOS = {"boot": boot, "onboard": onboard, "refuse": refuse}


async def run(scenario, agent, directory, *, binary=None):
    async with Box(agent, directory, binary=binary) as box:
        return await SCENARIOS[scenario](box)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
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
