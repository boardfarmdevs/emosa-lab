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
from emosa.wire.cmdu import Reassembler
from emosa_lab.simulation.database import SimDatabase

ROOT = Path(__file__).resolve().parents[3]
POD_ROWS = ROOT / "tests/fixtures/opensync/pod-6.6.1-hwsim-tables.json"
CONTROLLER_AL = "02:00:00:00:00:02"
AGENT_PORT = 6651
ETHERTYPE = 0x893A
AGENT_INTERFACE, CONTROLLER_INTERFACE = "em0", "ctl0"
AUTOCONFIG_SEARCH = 0x0007


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
    """The controller's end of the veth: every 1905 message the agent sends."""

    def __init__(self, interface):
        self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
        self.socket.bind((interface, ETHERTYPE))
        self.socket.setblocking(False)
        self.reassembler = Reassembler()
        self.messages = []

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

    def close(self):
        self.socket.close()


async def boot(agent, directory, *, binary=None, seconds=60):
    """The agent starts, takes the pod's connection and searches for a controller."""
    directory.mkdir(parents=True, exist_ok=True)
    network()
    tables, operations = recorded_pod()
    serial = next(iter(tables["AWLAN_Node"].values()))["serial_number"]
    db = SimDatabase()  # its own short directory: Unix socket paths are limited to 108 bytes
    await db.start()
    session = OvsSession(db.endpoint)
    try:
        await session.transact(operations)
    finally:
        await session.close()
    entry = {
        "pod_id": serial,
        "port": AGENT_PORT,
        "interface": AGENT_INTERFACE,
        "al_mac": derive_al(serial, [CONTROLLER_AL]),
    }
    config = agent_config(
        entry, {"controller_al": CONTROLLER_AL, "state_root": str(directory / "state")}
    )
    # the agent's interface carries its AL MAC, as the labs' macvlan per agent does
    subprocess.run(["ip", "link", "set", AGENT_INTERFACE, "address", entry["al_mac"]], check=True)
    (directory / "state").mkdir(mode=0o700, exist_ok=True)  # the fleet's state_root
    config_path = directory / "agent.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    controller = Controller(CONTROLLER_INTERFACE)
    env = {
        **os.environ,
        "EMOSA_SCHEMAS": str(ROOT / "schemas"),
        "EMOSA_PROFILES": str(ROOT / "src/emosa/profiles"),
    }
    log = (directory / "agent.log").open("w")
    process = subprocess.Popen(
        agent_command(agent, config_path, binary), stdout=log, stderr=log, env=env
    )
    result = {
        "agent": agent,
        "serial": serial,
        "al_mac": entry["al_mac"],
        "searched": False,
        "status": None,
    }
    try:
        await asyncio.sleep(1)
        await dial(db, f"tcp:127.0.0.1:{AGENT_PORT}")
        deadline = time.monotonic() + seconds
        status_path = directory / "state" / serial / "status.json"
        while time.monotonic() < deadline and process.poll() is None:
            controller.poll()
            if any(m.message_type == AUTOCONFIG_SEARCH for m in controller.messages):
                result["searched"] = True
                break
            await asyncio.sleep(0.2)
        if status_path.exists():
            result["status"] = json.loads(status_path.read_text())
        result["exit"] = process.poll()
        result["messages"] = sorted({f"0x{m.message_type:04x}" for m in controller.messages})
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        log.close()
        controller.close()
        await db.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=["boot"])
    parser.add_argument("--agent", choices=["c", "python"], required=True)
    parser.add_argument("--binary", help="the C agent (default c/build/emosa-agent-c)")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    in_namespace()
    result = asyncio.run(boot(args.agent, args.directory.resolve(), binary=args.binary))
    print(json.dumps({k: v for k, v in result.items() if k != "status"}, indent=2))
    return 0 if result["searched"] else 1


if __name__ == "__main__":
    sys.exit(main())
