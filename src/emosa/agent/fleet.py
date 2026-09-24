"""EMOSA fleet: one virtual EasyMesh agent for every OpenSync pod that appears.

The fleet is a manager front port in the style of the OpenSync redirector. A
pod's ``cm`` is handed to it (by the operator cloud's redirector, or the lab
NOC), and its ovsdb-server connects here, with EMOSA as the OVSDB client, like
any cloud manager. For each connection the fleet:

1. reads the pod's ``AWLAN_Node`` identity (serial, node ID, model, firmware);
2. finds or allocates the pod's virtual agent and persists it in the registry:
   - an AL MAC derived from the serial (stable even if the registry is lost);
   - a per-pod OVSDB port and a 1905 interface;
3. writes the agent's configuration and starts the agent
   (``emosa-agent@<pod>``, see :mod:`emosa.agent.pod`);
4. writes ``AWLAN_Node.manager_addr`` to the agent's port and ends the session.
   ``cm`` acts on a new manager address only once disconnected, as with any
   cloud handover.

A pod that reconnects later, for example after a reboot, gets the same agent
back. Per-pod agents stay separate processes: one pod's fault or restart never
touches another pod's agent.

Run: ``python -m emosa.agent.fleet serve /etc/emosa-fleet.json``
(also ``list`` and ``forget SERIAL``).
"""

import argparse
import errno
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

import ovs.jsonrpc
import ovs.poller
import ovs.stream

from emosa.config import validate
from emosa.opensync.profiles import DEFAULT as DEFAULT_PROFILE

log = logging.getLogger("emosa.fleet")

SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
IDENTITY = ["id", "serial_number", "model", "firmware_version"]
TIMEOUT = 5  # seconds for one pod's whole identify-and-hand-over exchange
AGENT_DEFAULTS = {
    "message_set": "easymesh-6.1",
    "multi_bss": False,
    "m2_session": "distinct",
    "profile": DEFAULT_PROFILE,
    "uplink": {"mode": "off"},
}


def derive_al(serial, taken=()):
    """A locally administered unicast AL MAC from the serial, avoiding ``taken``."""
    for n in range(256):
        digest = hashlib.sha256(f"emosa-agent-al:{serial}:{n}".encode()).digest()
        al = bytes([0x02]) + digest[:5]
        text = al.hex(":")
        if text not in taken:
            return text
    raise RuntimeError("no free AL MAC")


class Registry:
    """serial -> virtual agent; a JSON file rewritten atomically on every change."""

    def __init__(self, path, ports, *, reserved_als=()):
        self.path, self.ports = Path(path), tuple(ports)
        self.reserved = set(reserved_als)
        self.lock = threading.Lock()
        try:
            self.agents = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.agents = {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.agents, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)

    def assign(self, identity):
        """The pod's agent entry, allocated on first sight; None when full."""
        serial = identity["serial_number"]
        with self.lock:
            entry = self.agents.get(serial)
            if entry is None:
                used = {a["port"] for a in self.agents.values()}
                port = next((p for p in self.ports if p not in used), None)
                if port is None:
                    return None
                index = self.ports.index(port) + 1
                taken = self.reserved | {a["al_mac"] for a in self.agents.values()}
                entry = {
                    "pod_id": serial,
                    "al_mac": derive_al(serial, taken),
                    "port": port,
                    "interface": f"em{index}",
                    "first_seen": time.time(),
                    "handovers": 0,
                }
                self.agents[serial] = entry
            entry.update(
                node_id=identity.get("id"),
                model=identity.get("model"),
                firmware=identity.get("firmware_version"),
                last_seen=time.time(),
                handovers=entry["handovers"] + 1,
            )
            self._save()
            return dict(entry)

    def forget(self, serial):
        with self.lock:
            entry = self.agents.pop(serial, None)
            self._save()
            return entry


def agent_config(entry, fleet):
    """The per-pod agent configuration (the format ``emosa.agent.pod`` reads).

    The fleet's settings apply to every agent; ``pods.<serial>`` overrides them
    for one pod (a different pod model's profile, or its uplink policy).
    """
    own = fleet.get("pods", {}).get(entry["pod_id"], {})
    return {
        "pod_id": entry["pod_id"],
        "serial": entry["pod_id"],
        "ovsdb": f"ptcp:{entry['port']}:127.0.0.1",
        "interface": entry["interface"],
        "al_mac": entry["al_mac"],
        "controller_al": fleet["controller_al"],
        **{k: own.get(k, fleet.get(k, v)) for k, v in AGENT_DEFAULTS.items()},
        "state_dir": str(Path(fleet["state_root"]) / entry["pod_id"]),
        "run_id": entry["pod_id"],
    }


def systemd_starter(pod_id, changed):
    """Start the pod's agent unit, or restart it when its configuration changed."""
    subprocess.run(["systemctl", "enable", "-q", f"emosa-agent@{pod_id}"], check=True)
    action = "restart" if changed else "start"
    subprocess.run(["systemctl", action, f"emosa-agent@{pod_id}"], check=True)


def systemd_stopper(pod_id):
    subprocess.run(["systemctl", "disable", "-q", "--now", f"emosa-agent@{pod_id}"], check=False)


class Fleet:
    def __init__(self, config, *, starter=systemd_starter, stopper=systemd_stopper):
        validate("fleet-config", config)
        self.config = config
        low, high = config["ports"]
        self.registry = Registry(
            Path(config["state_root"]) / "fleet.json",
            range(low, high + 1),
            reserved_als={config["controller_al"]},
        )
        self.config_dir = Path(config["config_dir"])
        self.starter, self.stopper = starter, stopper
        admit = config.get("admit", "*")
        self.admit = None if admit == "*" else set(admit)
        self.stop = threading.Event()
        self.slots = threading.BoundedSemaphore(config.get("concurrency", 16))

    # -- one pod connection --------------------------------------------------

    def _call(self, conn, method, params, deadline):
        request = ovs.jsonrpc.Message.create_request(method, params)
        if conn.send(request):
            raise ConnectionError("send failed")
        while True:
            conn.run()
            error, message = conn.recv()
            if error == 0:
                if message.type == ovs.jsonrpc.Message.T_REQUEST and message.method == "echo":
                    conn.send(ovs.jsonrpc.Message.create_reply(message.params, message.id))
                elif message.id == request.id:
                    if message.type == ovs.jsonrpc.Message.T_ERROR:
                        raise ConnectionError(f"{method} refused: {message.error}")
                    return message.result
                continue
            if error != errno.EAGAIN:
                raise ConnectionError(f"connection lost: {os.strerror(error)}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method}: no reply")
            poller = ovs.poller.Poller()
            conn.wait(poller)
            conn.recv_wait(poller)
            poller.timer_wait(int(remaining * 1000))
            poller.block()

    def handle(self, conn, peer):
        """Identify one connected pod, bind its agent and hand it over."""
        deadline = time.monotonic() + TIMEOUT
        db = self.config.get("database", "Open_vSwitch")
        select = {"op": "select", "table": "AWLAN_Node", "where": [], "columns": IDENTITY}
        rows = self._call(conn, "transact", [db, select], deadline)[0].get("rows", [])
        if len(rows) != 1:
            raise ValueError(f"{peer}: expected one AWLAN_Node row, got {len(rows)}")
        identity = {k: rows[0].get(k) for k in IDENTITY}
        serial = identity["serial_number"]
        if not isinstance(serial, str) or not SERIAL.fullmatch(serial):
            raise ValueError(f"{peer}: unusable serial {serial!r}")
        if self.admit is not None and serial not in self.admit:
            log.warning("pod %s (%s) not admitted: left unchanged", serial, peer)
            return None
        entry = self.registry.assign(identity)
        if entry is None:
            log.warning("pod %s (%s): no free agent port, left unchanged", serial, peer)
            return None
        path = self.config_dir / f"{entry['pod_id']}.json"
        text = json.dumps(agent_config(entry, self.config), indent=2) + "\n"
        changed = path.exists() and path.read_text() != text
        if not path.exists() or changed:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text)
            os.replace(tmp, path)
        self.starter(entry["pod_id"], changed)
        target = f"tcp:{self.config['advertise']}:{entry['port']}"
        update = {
            "op": "update",
            "table": "AWLAN_Node",
            "where": [],
            "row": {"manager_addr": target},
        }
        result = self._call(conn, "transact", [db, update], deadline)
        if not result or result[0].get("error") or result[0].get("count") != 1:
            raise ConnectionError(f"{serial}: manager_addr update failed: {result}")
        log.info(
            "pod %s (node %s, %s) -> agent AL %s on %s, manager_addr %s",
            serial,
            identity["id"],
            peer,
            entry["al_mac"],
            entry["interface"],
            target,
        )
        return entry

    def _serve_one(self, stream):
        conn = ovs.jsonrpc.Connection(stream)
        try:
            self.handle(conn, stream.name)
        except (
            ConnectionError,
            TimeoutError,
            ValueError,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            log.warning("pod connection %s: %s", stream.name, exc)
        finally:
            conn.close()
            self.slots.release()

    # -- the front port --------------------------------------------------------

    def listen(self):
        name = self.config["listen"]
        if name.startswith("ptcp:"):
            _, port, host = name.split(":")  # C form ptcp:PORT:HOST; ovs Python takes HOST:PORT
            name = f"ptcp:{host}:{port}"
        error, pstream = ovs.stream.PassiveStream.open(name)
        if error:
            raise OSError(error, f"cannot listen on {self.config['listen']}")
        pstream.socket.setblocking(False)
        return pstream

    def serve(self, pstream=None, ready=None):
        pstream = pstream or self.listen()
        if ready is not None:
            ready.set()
        log.info("fleet: %s, agents on ports %s", self.config["listen"], self.config["ports"])
        try:
            while not self.stop.is_set():
                error, stream = pstream.accept()
                if error == errno.EAGAIN:
                    poller = ovs.poller.Poller()
                    pstream.wait(poller)
                    poller.timer_wait(200)
                    poller.block()
                    continue
                if error:
                    log.warning("accept: %s", os.strerror(error))
                    continue
                if not self.slots.acquire(timeout=TIMEOUT):
                    stream.close()
                    continue
                threading.Thread(target=self._serve_one, args=(stream,), daemon=True).start()
        finally:
            pstream.close()

    def forget(self, serial):
        """Release a pod: stop its agent, drop its entry and configuration, and
        archive its state. A pod handed over again starts a new ownership period;
        conflicts recorded in the old one (someone else changed the pod after the
        release) must not block it. The archive keeps the history."""
        entry = self.registry.forget(serial)
        if entry is not None:
            self.stopper(entry["pod_id"])
            (self.config_dir / f"{entry['pod_id']}.json").unlink(missing_ok=True)
            state = Path(self.config["state_root"]) / entry["pod_id"]
            if state.is_dir():
                archive = state.with_name(f"{state.name}.released-{time.strftime('%Y%m%dT%H%M%S')}")
                state.rename(archive)
                entry["archived_state"] = str(archive)
        return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("serve", "list", "forget"))
    parser.add_argument("config", type=Path, help="fleet JSON (listen, advertise, ports, ...)")
    parser.add_argument("serial", nargs="?")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = json.loads(args.config.read_text())
    Path(config["state_root"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(config["config_dir"]).mkdir(parents=True, exist_ok=True)
    fleet = Fleet(config)
    if args.command == "list":
        print(json.dumps(fleet.registry.agents, indent=2, sort_keys=True))
    elif args.command == "forget":
        if not args.serial:
            parser.error("forget needs a SERIAL")
        print(json.dumps(fleet.forget(args.serial), indent=2, sort_keys=True))
    else:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: fleet.stop.set())
        fleet.serve()


if __name__ == "__main__":
    main()
