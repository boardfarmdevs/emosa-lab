# SPDX-License-Identifier: Apache-2.0
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

With ``files`` the fleet also serves files for the pods' own modules (spec 4): plain HTTP on
its advertise address, each file named by its SHA-256 (``FileServer``).

Run: ``python -m emosa.agent.fleet serve /etc/emosa-fleet.json``
(also ``list`` and ``forget SERIAL``).
"""

import argparse
import contextlib
import ctypes
import ctypes.util
import errno
import hashlib
import http.server
import ipaddress
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
from emosa.errors import EmosaError, Reason
from emosa.opensync.profiles import DEFAULT as DEFAULT_PROFILE
from emosa.secrets import REFERENCE, FileSecrets

log = logging.getLogger("emosa.fleet")

SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")
FILES_PORT = 6650
FILES_RETRY = 10  # seconds between tries to listen while the advertise address is not there
FILE_MAX = 1 << 20  # bytes of one served file, at most
TIME_ERROR = 5  # adjtimex(2)'s clock state while the kernel says the clock is unsynchronized
CLOCK_CHECK = 60  # seconds a clock check stands (a time_synced_command is not run more often)
IDENTITY = ["id", "serial_number", "model", "firmware_version"]
TIMEOUT = 5  # seconds for one pod's whole identify-and-hand-over exchange
AGENT_DEFAULTS = {
    "message_set": "easymesh-6.1",
    "multi_bss": False,
    "m2_session": "distinct",
    "profile": DEFAULT_PROFILE,
    "uplink": {"mode": "off"},
    "telemetry": {"mode": "off"},
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


def free_underlay(gtp_underlay, used):
    """The lowest 169.254.N.0/24 (N 1 to 254) that overlaps neither the site's GTP underlay nor
    one in ``used``: a pod's underlay when it is a GRE parent (spec 8.6). None when none is left."""
    gtp = ipaddress.ip_network(gtp_underlay, strict=False)
    for n in range(1, 255):
        net = ipaddress.ip_network(f"169.254.{n}.0/24")
        if not net.overlaps(gtp) and str(net) not in used:
            return str(net)
    return None


class Registry:
    """serial -> virtual agent; a JSON file rewritten atomically on every change. The file
    is the state: it is read again before each change, so a forget run by another process
    (emosa-fleet forget while the fleet serves) takes effect at once.

    With the site's GTP underlay given (fleet ``gtp_underlay``), every entry also holds the
    underlay it uses as a GRE parent (spec 8.6), allocated on first sight like its port and kept;
    one that overlaps the GTP underlay (its configuration changed) is allocated again, at the
    pod's next arrival or the fleet's next start, whichever comes first."""

    def __init__(self, path, ports, *, reserved_als=(), gtp_underlay=None):
        self.path, self.ports = Path(path), tuple(ports)
        self.reserved = set(reserved_als)
        self.gtp_underlay = gtp_underlay
        self.lock = threading.Lock()
        self._load()

    def _underlay(self, entry):
        gtp = ipaddress.ip_network(self.gtp_underlay, strict=False)
        mine = entry.get("underlay")
        if mine is not None and not ipaddress.ip_network(mine).overlaps(gtp):
            return mine
        used = {a["underlay"] for a in self.agents.values() if a is not entry and "underlay" in a}
        return free_underlay(self.gtp_underlay, used)

    def _place(self, entry):
        """The entry's underlay brought in line with the GTP underlay; whether it changed."""
        underlay = self._underlay(entry)
        if underlay is None:
            log.warning("%s: no 169.254.N.0/24 left for its GRE parent underlay", entry["pod_id"])
        if underlay == entry.get("underlay"):
            return False
        if underlay is None:
            del entry["underlay"]
        else:
            entry["underlay"] = underlay
        return True

    def settle(self):
        """At the fleet's start, before its agents start: every entry's underlay placed, in
        the registry's order (by serial), and the registry saved when one changed."""
        if self.gtp_underlay is None:
            return
        with self.lock:
            self._load()
            if [serial for serial in sorted(self.agents) if self._place(self.agents[serial])]:
                self._save()

    def _load(self):
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
            self._load()
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
            if self.gtp_underlay is not None:
                self._place(entry)
            self._save()
            return dict(entry)

    def forget(self, serial):
        with self.lock:
            self._load()
            entry = self.agents.pop(serial, None)
            self._save()
            return entry


def module_settings(modules, fleet):
    """The agent's ``modules`` from the fleet's (spec 4): each module's settings, with
    ``update_url`` (the fleet's file server) and ``update_sha256`` for its ``update``."""
    settings = {}
    for module, own in sorted((modules or {}).items()):
        values = dict(sorted((own.get("config") or {}).items()))
        if own.get("update"):
            port = (fleet.get("files") or {}).get("port", FILES_PORT)
            values["update_url"] = f"http://{fleet['advertise']}:{port}/{own['update']}"
            values["update_sha256"] = own["update"]
        if values:
            settings[module] = values
    return settings


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
        # the experiment's switch (spec 3.10, anchor_ap), the pod's or the fleet's, only when set:
        # off otherwise, and the configurations of a fleet without it stay as they were
        **(
            {"anchor_ap": own.get("anchor_ap", fleet.get("anchor_ap"))}
            if "anchor_ap" in own or "anchor_ap" in fleet
            else {}
        ),
        # a property of the controller, so the fleet's only, and only when set: the
        # configurations of a fleet without it stay as they were
        **(
            {"topology_query_window": fleet["topology_query_window"]}
            if "topology_query_window" in fleet
            else {}
        ),
        # its modules' settings (spec 4), only when there are any
        **(
            {"modules": modules}
            if (modules := module_settings(own.get("modules", fleet.get("modules")), fleet))
            else {}
        ),
        # its underlay as a GRE parent (spec 8.6), while the fleet allocates them
        **(
            {"underlay": entry["underlay"]}
            if "underlay" in entry and "gtp_underlay" in fleet
            else {}
        ),
        "state_dir": str(Path(fleet["state_root"]) / entry["pod_id"]),
        **(
            {"run_dir": str(Path(fleet["run_root"]) / entry["pod_id"])}
            if "run_root" in fleet
            else {}
        ),
        "run_id": entry["pod_id"],
    }


def systemd_starter(pod_id, changed):
    """Start the pod's agent unit, or restart it when its configuration changed."""
    subprocess.run(["systemctl", "enable", "-q", f"emosa-agent@{pod_id}"], check=True)
    action = "restart" if changed else "start"
    subprocess.run(["systemctl", action, f"emosa-agent@{pod_id}"], check=True)


def systemd_stopper(pod_id):
    subprocess.run(["systemctl", "disable", "-q", "--now", f"emosa-agent@{pod_id}"], check=False)


def servable(directory, name):
    """The file ``name`` of ``directory`` when it is served (spec 4): a SHA-256 name, a regular
    file of at most FILE_MAX bytes whose content has that SHA-256. (content, None), or (None,
    why)."""
    if not SHA256.fullmatch(name):
        return None, "not a SHA-256"
    path = Path(directory) / name
    try:
        if not path.is_file() or path.is_symlink():
            return None, "no such file"
        if path.stat().st_size > FILE_MAX:
            return None, "too large"
        content = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable: {exc.strerror}"
    if hashlib.sha256(content).hexdigest() != name:
        return None, "its content is not its name"
    return content, None


def kernel_clock_synchronized():
    """The kernel's clock status, as ntpd keeps it (``ntpq -c kerninfo``): adjtimex(2) with no
    change asked; synchronized unless it says TIME_ERROR (STA_UNSYNC, the kernel's state until a
    time daemon synchronizes it) or fails."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        buf = ctypes.create_string_buffer(512)  # a struct timex, modes 0: read only
        state = libc.adjtimex(buf)
    except (OSError, AttributeError):
        return False
    return state >= 0 and state != TIME_ERROR


def clock_synchronized(files, environ=None):
    """Whether the fleet's host trusts its clock (spec 4, the pods' time): the box's test hook
    EMOSA_FILES_CLOCK, else ``files.time_synced_command`` (exit 0: synchronized) when set, else the
    kernel's clock status."""
    environ = os.environ if environ is None else environ
    forced = environ.get("EMOSA_FILES_CLOCK")
    if forced in ("synchronized", "unsynchronized"):
        return forced == "synchronized"
    command = files.get("time_synced_command")
    if command:
        try:
            return subprocess.run(["/bin/sh", "-c", command], timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return kernel_clock_synchronized()


class FileServer:
    """The fleet's file server (spec 4): plain HTTP on ``host``:``port``. GET or HEAD ``/``:
    200, an empty body (a pod reads the time from its Date header); ``/<sha256>``: a servable file
    of ``directory``, checked at each request, else 404. Every reply has a Date header (GMT, the
    standard library's) while the host's clock is synchronized (``synchronized``); else none, and
    ``/`` is 503 (Retry-After 60): a server without a reliable clock sends no Date (RFC 7231).
    Each request is logged with the peer, the path and the answer."""

    def __init__(self, directory, host, port, synchronized=lambda: True):
        self.directory = Path(directory)
        self.synchronized = synchronized
        self.checked, self.trusted = None, None  # the last check (monotonic) and its answer
        self.lock = threading.Lock()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version, sys_version = "emosa-fleet", ""

            def send_response(self, code, message=None):
                if server.clock():
                    super().send_response(code, message)
                    return
                self.log_request(code)  # no Date without a clock to trust
                self.send_response_only(code, message)
                self.send_header("Server", self.version_string())

            def answer(self, body):
                path = self.path.split("?", 1)[0]
                if path == "/" and not server.clock():
                    self.send_response(503)
                    self.send_header("Retry-After", "60")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/":
                    content, why = b"", None
                else:
                    content, why = servable(server.directory, path.lstrip("/"))
                if content is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    log.warning(
                        "files: %s %s %s: 404, %s", self.client_address[0], self.command, path, why
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                if body:
                    self.wfile.write(content)
                if path != "/":
                    log.info(
                        "files: %s %s %s: 200, %d bytes",
                        self.client_address[0],
                        self.command,
                        path,
                        len(content),
                    )

            def do_GET(self):  # noqa: N802 (the standard library's name)
                self.answer(True)

            def do_HEAD(self):  # noqa: N802
                self.answer(False)

            def log_message(self, format, *args):  # noqa: A002
                pass  # answer() logs each request

        self.httpd = http.server.ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.address = self.httpd.server_address

    def clock(self):
        """Whether the host's clock is trusted now: checked at most once a CLOCK_CHECK, each change
        logged."""
        with self.lock:
            now = time.monotonic()
            if self.checked is None or now - self.checked >= CLOCK_CHECK:
                trusted = bool(self.synchronized())
                if trusted != self.trusted:
                    log.warning(
                        "files: the clock is %s",
                        "synchronized: the time served"
                        if trusted
                        else "not synchronized: no time served",
                    )
                self.checked, self.trusted = now, trusted
            return self.trusted

    def start(self):
        names = sorted(p.name for p in self.directory.iterdir()) if self.directory.is_dir() else []
        served = [n for n in names if servable(self.directory, n)[0] is not None]
        for name in names:
            if name not in served:
                log.warning("files: %s not served: %s", name, servable(self.directory, name)[1])
        log.info(
            "files: %d served from %s on %s:%d", len(served), self.directory, *self.address[:2]
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Fleet:
    def __init__(self, config, *, starter=systemd_starter, stopper=systemd_stopper):
        validate("fleet-config", config)
        modules = [config.get("modules") or {}] + [
            own.get("modules") or {} for own in (config.get("pods") or {}).values()
        ]
        if not config.get("files") and any(m.get("update") for ms in modules for m in ms.values()):
            raise EmosaError(Reason.INVALID_INPUT, "a module's update needs the fleet's files")
        self.config = config
        low, high = config["ports"]
        self.registry = Registry(
            Path(config["state_root"]) / "fleet.json",
            range(low, high + 1),
            reserved_als={config["controller_al"]},
            gtp_underlay=config.get("gtp_underlay"),
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
        self.bind(entry)
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

    def bind(self, entry):
        """The entry's agent configuration written when new or changed, and its agent
        started, or restarted when the configuration changed (spec §4 step 4). Whether it
        changed."""
        path = self.config_dir / f"{entry['pod_id']}.json"
        config = agent_config(entry, self.config)
        self.provision_secrets(config)
        text = json.dumps(config, indent=2) + "\n"
        changed = path.exists() and path.read_text() != text
        if not path.exists() or changed:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text)
            os.replace(tmp, path)
        self.starter(entry["pod_id"], changed)
        return changed

    def provision_secrets(self, config):
        """spec §4: each secret reference the agent configuration names (its uplink's, its GTP
        station's) that the agent's own store lacks, copied from the fleet's store (``secrets``):
        created, never replaced, durable, mode 0600. One the fleet's store lacks too is logged
        by reference only; the agent then says what it misses."""
        if not self.config.get("secrets"):
            return
        uplink = config.get("uplink") or {}
        refs = sorted(
            {
                ref
                for ref in (
                    uplink.get("secret_ref"),
                    (uplink.get("bootstrap") or {}).get("secret_ref"),
                )
                if isinstance(ref, str) and REFERENCE.fullmatch(ref)
            }
        )
        if not refs:
            return
        fleet = FileSecrets(Path(self.config["secrets"]))
        own = FileSecrets(Path(config["state_dir"]) / "secrets")
        for ref in refs:
            try:
                own.read(ref)
                continue  # the agent's own: never replaced
            except EmosaError:
                pass
            try:
                value = fleet.read(ref)
            except EmosaError:
                log.warning(
                    "pod %s: secret %s in neither its store nor the fleet's", config["pod_id"], ref
                )
                continue
            with contextlib.suppress(FileExistsError):
                own.create(ref, value)
            log.info("pod %s: secret %s provisioned from the fleet's store", config["pod_id"], ref)

    def start_registered(self):
        """At the fleet's start, every admitted pod's agent in the registry, as step 4
        does, without waiting for the pod to come back through the front port: an image
        upgrade keeps the registry and the configurations (on /nvram), not the agents'
        enabled units. In the registry's order (by serial); a failed start is logged."""
        self.registry.settle()
        with self.registry.lock:
            self.registry._load()
            agents = self.registry.agents
            entries = [dict(agents[serial]) for serial in sorted(agents)]
        for entry in entries:
            if self.admit is not None and entry["pod_id"] not in self.admit:
                continue
            try:
                changed = self.bind(entry)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("pod %s: its agent not started: %s", entry["pod_id"], exc)
                continue
            log.info(
                "pod %s: its agent %s from the registry",
                entry["pod_id"],
                "restarted, its configuration changed," if changed else "started",
            )

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

    def _files(self, failures):
        """The file server on the advertise address, or None when it cannot listen yet: the
        fleet serves its pods meanwhile and tries again every FILES_RETRY s, as a gateway's LAN
        bridge may get the address minutes after the fleet starts (14 October). ``failures``: the
        tries failed so far; the first failure and the server's start after one are logged."""
        port = self.config["files"].get("port", FILES_PORT)
        try:
            files = FileServer(
                self.config["files"]["directory"],
                self.config["advertise"],
                port,
                lambda: clock_synchronized(self.config["files"]),
            )
            files.start()
        except OSError as exc:
            if not failures:
                log.warning(
                    "files: cannot serve on %s:%d: %s; trying again every %d s",
                    self.config["advertise"],
                    port,
                    exc,
                    FILES_RETRY,
                )
            return None
        if failures:
            log.info(
                "files: serving on %s:%d after %d tries",
                self.config["advertise"],
                port,
                failures + 1,
            )
        return files

    def serve(self, pstream=None, ready=None):
        pstream = pstream or self.listen()
        files, failures, retry_at = None, 0, None
        if self.config.get("files"):
            files = self._files(failures)
            if files is None:
                failures, retry_at = 1, time.monotonic() + FILES_RETRY
        self.start_registered()
        if ready is not None:
            ready.set()
        log.info("fleet: %s, agents on ports %s", self.config["listen"], self.config["ports"])
        try:
            while not self.stop.is_set():
                if retry_at is not None and time.monotonic() >= retry_at:  # the address yet?
                    files = self._files(failures)
                    failures += files is None
                    retry_at = None if files is not None else time.monotonic() + FILES_RETRY
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
            if files is not None:
                files.stop()

    def forget(self, serial):
        """Release a pod: stop its agent, drop its entry and configuration, and
        archive its state. A pod handed over again starts a new ownership period;
        conflicts recorded in the old one (someone else changed the pod after the
        release) must not block it. The archive keeps the history, not the status: that
        is a running agent's (in the run directory, its link would show the next agent's)."""
        entry = self.registry.forget(serial)
        if entry is not None:
            self.stopper(entry["pod_id"])
            (self.config_dir / f"{entry['pod_id']}.json").unlink(missing_ok=True)
            state = Path(self.config["state_root"]) / entry["pod_id"]
            if state.is_dir():
                archive = state.with_name(f"{state.name}.released-{time.strftime('%Y%m%dT%H%M%S')}")
                state.rename(archive)
                (archive / "status.json").unlink(missing_ok=True)
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
    if config.get("agents") == "supervised" and args.command != "list":
        # spec §4: the agents as the fleet's own children are the C fleet's
        parser.error('"agents": "supervised" is emosa-fleet-c\'s; this fleet runs systemd units')
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
