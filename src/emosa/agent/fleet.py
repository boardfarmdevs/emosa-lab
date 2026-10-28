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
import hmac
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

from emosa import logs
from emosa.config import validate, validator
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
ADOPT_CHECK = 1  # seconds between the serving loop's looks at the free agent ports (re-admission)
ADOPT_MAX = 128  # free agent ports listened on at most (the C fleet's and the forwarder's 128)
MISSING_SECRETS = "fleet-secrets.missing.json"  # under the state root: references with no source
STALE_SECRETS = "fleet-secrets.stale.json"  # under the state root: hand copies that differ
RELEASE_WAIT = 2  # seconds a handler waits for the loop to stop listening on an agent port
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


LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
INET_COLUMNS = ["if_name", "inet_addr", "netmask", "ip_assign_scheme"]


def held_underlay(rows):
    """The underlay a pod's GRE parent AP already has (spec 8.6: its Inet row static, the
    underlay's .1 with netmask 255.255.255.0, a 169.254.N.0/24, N 1 to 254), when its rows name
    exactly one; None otherwise. On first sight the fleet keeps it (spec 4, re-admission)."""
    held = set()
    for row in rows or ():
        address, netmask = row.get("inet_addr"), row.get("netmask")
        if row.get("ip_assign_scheme") != "static" or netmask != "255.255.255.0":
            continue
        try:
            address = ipaddress.ip_address(address)
        except (TypeError, ValueError):
            continue  # unset (an empty set) or not an address
        net = ipaddress.ip_network(f"{address}/24", strict=False)
        n = int(net.network_address) >> 8 & 0xFF
        if address == net.network_address + 1 and net.subnet_of(LINK_LOCAL) and 1 <= n <= 254:
            held.add(str(net))
    return held.pop() if len(held) == 1 else None


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

    def _underlay(self, entry, held=None):
        gtp = ipaddress.ip_network(self.gtp_underlay, strict=False)
        mine = entry.get("underlay")
        if mine is not None and not ipaddress.ip_network(mine).overlaps(gtp):
            return mine
        used = {a["underlay"] for a in self.agents.values() if a is not entry and "underlay" in a}
        # the one the pod's GRE parent already has (held_underlay), when no entry uses it and it
        # is clear of the GTP: its children's tunnels stay up (spec 4, re-admission)
        if held is not None and held not in used and not ipaddress.ip_network(held).overlaps(gtp):
            return held
        return free_underlay(self.gtp_underlay, used)

    def _place(self, entry, held=None):
        """The entry's underlay brought in line with the GTP underlay; whether it changed."""
        underlay = self._underlay(entry, held)
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

    def _create(self, serial, port):
        """A new entry on ``port``: its interface ``em<index>`` the port's place in the range, its
        AL MAC derived from the serial (§2.2), so a pod given its port again gets them again."""
        taken = self.reserved | {a["al_mac"] for a in self.agents.values()}
        entry = {
            "pod_id": serial,
            "al_mac": derive_al(serial, taken),
            "port": port,
            "interface": f"em{self.ports.index(port) + 1}",
            "first_seen": time.time(),
            "handovers": 0,
        }
        self.agents[serial] = entry
        return entry

    def _seen(self, entry, identity, held=None):
        entry.update(
            node_id=identity.get("id"),
            model=identity.get("model"),
            firmware=identity.get("firmware_version"),
            last_seen=time.time(),
            handovers=entry["handovers"] + 1,
        )
        if self.gtp_underlay is not None:
            self._place(entry, held)
        self._save()
        return dict(entry)

    def free_ports(self):
        """The agent ports no entry holds."""
        with self.lock:
            self._load()
            used = {a["port"] for a in self.agents.values()}
        return [p for p in self.ports if p not in used]

    def assign(self, identity, held=None):
        """The pod's agent entry, allocated on first sight; None when full. ``held``: the
        underlay its GRE parent already has (held_underlay), kept on first sight when free."""
        serial = identity["serial_number"]
        with self.lock:
            self._load()
            entry = self.agents.get(serial)
            if entry is None:
                used = {a["port"] for a in self.agents.values()}
                port = next((p for p in self.ports if p not in used), None)
                if port is None:
                    return None
                entry = self._create(serial, port)
            return self._seen(entry, identity, held)

    def adopt(self, identity, port, held=None):
        """A pod that dialled agent port ``port`` (spec 4, re-admission): ("adopted", its entry
        made on that port) when it has none and no entry holds the port, as after a lost registry
        (a factory reset); ("registered", its entry) when it has one, wherever it is; ("taken",
        None) when another pod's entry holds the port."""
        serial = identity["serial_number"]
        with self.lock:
            self._load()
            entry = self.agents.get(serial)
            if entry is not None:
                return "registered", self._seen(entry, identity, held)
            if port not in self.ports or any(a["port"] == port for a in self.agents.values()):
                return "taken", None
            return "adopted", self._seen(self._create(serial, port), identity, held)

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


def recognised(models, model, firmware):
    """The fleet's ``models`` entry that recognises a pod of this model and firmware (spec 4,
    the pods' models), with its index: the first whose model is the pod's and whose
    firmware_prefix, when it has one, starts the pod's firmware. (None, None) when none does."""
    if not isinstance(model, str):
        return None, None
    for index, own in enumerate(models or ()):
        prefix = own.get("firmware_prefix")
        if own["model"] == model and (
            prefix is None or (isinstance(firmware, str) and firmware.startswith(prefix))
        ):
            return index, own
    return None, None


def agent_config(entry, fleet):
    """The per-pod agent configuration (the format ``emosa.agent.pod`` reads).

    The fleet's settings apply to every agent; the pod's model's (``models``, spec 4) override
    them, and ``pods.<serial>`` overrides both for one pod (its uplink policy, say).
    """
    _, model = recognised(fleet.get("models"), entry.get("model"), entry.get("firmware"))
    own = {
        **{k: v for k, v in (model or {}).items() if k not in ("model", "firmware_prefix")},
        **fleet.get("pods", {}).get(entry["pod_id"], {}),
    }
    return {
        "pod_id": entry["pod_id"],
        "serial": entry["pod_id"],
        "ovsdb": f"ptcp:{entry['port']}:127.0.0.1",
        "interface": entry["interface"],
        "al_mac": entry["al_mac"],
        "controller_al": fleet["controller_al"],
        **{k: own.get(k, fleet.get(k, v)) for k, v in AGENT_DEFAULTS.items()},
        # the radios' own netdevs (spec 3.10, phy_netdev_up, on by default), the pod's or the
        # fleet's, only when set: the configurations of a fleet without it stay as they were
        **(
            {"phy_netdev_up": own.get("phy_netdev_up", fleet.get("phy_netdev_up"))}
            if "phy_netdev_up" in own or "phy_netdev_up" in fleet
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


# spec 4 (15 October: one pod's unknown key kept every pod off EMOSA while the fleet restarted
# again and again): the fleet's configuration is kept once valid, under its state root; one that is
# not is rejected, its error recorded, and the fleet serves the last valid one
VALID_CONFIG, REJECTED_CONFIG = "fleet-config.valid.json", "fleet-config.rejected.json"


def check_config(config):
    """A fleet configuration's checks (spec 4): its schema, and a module's update needs the
    fleet's files. EmosaError when it fails one."""
    validate("fleet-config", config)
    modules = [config.get("modules") or {}] + [
        own.get("modules") or {}
        for own in [*(config.get("pods") or {}).values(), *(config.get("models") or [])]
    ]
    if not config.get("files") and any(m.get("update") for ms in modules for m in ms.values()):
        raise EmosaError(Reason.INVALID_INPUT, "a module's update needs the fleet's files")


def schema_error(config):
    """Where a fleet configuration fails its schema, as the C fleet names it ("/pods/X/key:
    additionalProperties": a key it does not know, by name, never a value), or None."""
    errors = sorted(validator("fleet-config").iter_errors(config), key=lambda e: str(list(e.path)))
    if not errors:
        return None
    first = errors[0]
    path = [str(p) for p in first.absolute_path]
    if first.validator == "additionalProperties" and isinstance(first.instance, dict):
        known = set(first.schema.get("properties") or {})
        path += sorted(k for k in first.instance if k not in known)[:1]
    return f"/{'/'.join(path)}: {first.validator}"


def passive(name):
    """ovs PassiveStream.open on a listen name in the C form (``ptcp:PORT:HOST``, ``punix:PATH``):
    (error, stream)."""
    if name.startswith("ptcp:"):
        _, port, host = name.split(":")  # C form ptcp:PORT:HOST; ovs Python takes HOST:PORT
        name = f"ptcp:{host}:{port}"
    return ovs.stream.PassiveStream.open(name)


def marker_name(ref):
    """The file beside an agent's copy that marks it the fleet's (spec 4): a leading dot, so
    never a reference's name."""
    return f".{ref}.from-fleet"


def marker(own, value):
    """The marker of a value in an agent's store: its HMAC-SHA256 under that store's own
    fingerprint key, hex."""
    return hmac.new(own.key(), value, hashlib.sha256).hexdigest().encode()


def replace_private(directory, name, value):
    """value as directory/name, replacing what is there: a temporary private file written and
    flushed, renamed over it, the directory flushed (an agent's copy the fleet changes, spec 4).
    The files backend's layout (FileSecrets, the only store the fleet opens for an agent):
    an agent store on a platform backend would need that backend's own replace instead."""
    directory = Path(directory)
    path, tmp = directory / name, directory / f".tmp-{name}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_private(path, text):
    """text to path through path.tmp and a rename, owner-only."""
    tmp = path.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(text)
    os.replace(tmp, path)


def platform_state_root():
    """The platform's state root (EMOSA_STATE_ROOT, as /etc/default/emosa gives it), else the
    packaging's default."""
    return os.environ.get("EMOSA_STATE_ROOT") or "/var/lib/emosa"


def served_config(config, path):
    """The configuration a serving fleet runs on (spec 4): ``config`` (None: the file is not
    JSON) when it passes check_config, kept then as the last valid one (state_root/VALID_CONFIG,
    its rejection record gone); else the one kept under the state root it names, or the
    platform's when it names none, the rejection logged once and recorded (REJECTED_CONFIG: the
    file, its error, when). Raises when there is none to fall back to."""
    try:
        if config is None:
            raise EmosaError(Reason.INVALID_INPUT, "not JSON")
        check_config(config)
    except EmosaError as exc:
        # the state root it names, else the platform's (EMOSA_STATE_ROOT, as /etc/default/emosa
        # gives it): a file that is not JSON (a partial write) names none
        named = config.get("state_root") if isinstance(config, dict) else None
        root = named if isinstance(named, str) and named else platform_state_root()
        kept = Path(root) / VALID_CONFIG
        if not kept.is_file():
            raise
        last = json.loads(kept.read_text())
        check_config(last)
        error = (schema_error(config) if config is not None else None) or str(exc)
        log.error(
            "fleet configuration %s rejected (%s): serving the last valid one, %s",
            path,
            error,
            kept,
        )
        record = {"config": str(path), "error": error, "at": int(time.time())}
        write_private(Path(root) / REJECTED_CONFIG, json.dumps(record, indent=2) + "\n")
        return last
    root = Path(config["state_root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(root / VALID_CONFIG, json.dumps(config, indent=2) + "\n")
    (root / REJECTED_CONFIG).unlink(missing_ok=True)
    return config


class Fleet:
    def __init__(self, config, *, starter=systemd_starter, stopper=systemd_stopper):
        check_config(config)
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
        self.admit_by_model = config.get("admit_by_model", False)
        self.stop = threading.Event()
        self.slots = threading.BoundedSemaphore(config.get("concurrency", 16))
        # spec 4, re-admission: each agent port no entry holds, listened on by the serving loop
        # (on loopback, where the agents listen), which alone opens and closes them; a handler
        # about to start an agent on one asks it to close that one first (_release)
        self.adoption = {}  # port -> PassiveStream
        self.adoption_lock = threading.Lock()
        self.releases = {}  # port -> Event, set once the loop closed its listener
        self.adopt_failed = set()  # ports it could not listen on, logged once
        self.secrets_lock = threading.Lock()  # the record of secrets with no source

    def admission(self, serial, model, firmware):
        """How the fleet admits a pod (spec 4 step 2), for its log: "all" (admit is *), "serial"
        (admit lists it) or "model" (admit_by_model on and one of its models recognises it),
        with the recognising models entry's description, if any. (None, ...) when not admitted."""
        index, own = recognised(self.config.get("models"), model, firmware)
        known = f", models[{index}] ({own['model']})" if own is not None else ""
        if self.admit is None:
            return "all", known
        if serial in self.admit:
            return "serial", known
        if self.admit_by_model and own is not None:
            return "model", known
        return None, known

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

    def handle(self, conn, peer, port=None):
        """Identify one connected pod, bind its agent and hand it over. ``port``: the agent port
        it dialled, for a pod that came on one no entry holds (spec 4, re-admission)."""
        deadline = time.monotonic() + TIMEOUT
        db = self.config.get("database", "Open_vSwitch")
        select = {"op": "select", "table": "AWLAN_Node", "where": [], "columns": IDENTITY}
        ops = [db, select]
        if self.config.get("gtp_underlay") is not None:  # GRE parents: the underlay it has
            ops.append(
                {"op": "select", "table": "Wifi_Inet_Config", "where": [], "columns": INET_COLUMNS}
            )
        results = self._call(conn, "transact", ops, deadline)
        rows = results[0].get("rows", [])
        inet = results[1] if len(results) > 1 and isinstance(results[1], dict) else {}
        held = held_underlay(inet.get("rows"))
        if len(rows) != 1:
            raise ValueError(f"{peer}: expected one AWLAN_Node row, got {len(rows)}")
        identity = {k: rows[0].get(k) for k in IDENTITY}
        serial = identity["serial_number"]
        if not isinstance(serial, str) or not SERIAL.fullmatch(serial):
            raise ValueError(f"{peer}: unusable serial {serial!r}")
        how, known = self.admission(serial, identity["model"], identity["firmware_version"])
        if how is None:
            log.warning("pod %s (%s) not admitted: left unchanged", serial, peer)
            return None
        placed = "front port"
        with self.adoption_lock:  # the loop opens listeners on the free ports under it too
            if port is None:
                entry = self.registry.assign(identity, held)
            else:
                placed, entry = self.registry.adopt(identity, port, held)
        if entry is None and port is None:
            log.warning("pod %s (%s): no free agent port, left unchanged", serial, peer)
            return None
        if entry is None:
            log.warning(
                "pod %s (%s) on agent port %d, another pod's: left unchanged", serial, peer, port
            )
            return None
        self._release(entry["port"])
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
            "pod %s (node %s, %s) -> agent AL %s on %s, manager_addr %s; admitted by %s%s%s",
            serial,
            identity["id"],
            peer,
            entry["al_mac"],
            entry["interface"],
            target,
            how,
            known,
            {
                "front port": "",
                "adopted": f"; re-admitted on the agent port it dialled ({port}), no entry held it",
                "registered": f"; it dialled agent port {port}, its entry's is {entry['port']}",
            }[placed],
        )
        return entry

    def _release(self, port):
        """The serving loop's listener on agent port ``port`` closed, before an agent starts there
        (it waits RELEASE_WAIT at most: the loop looks every 200 ms)."""
        with self.adoption_lock:
            if port not in self.adoption:
                return
            done = self.releases.setdefault(port, threading.Event())
        if not done.wait(RELEASE_WAIT):
            log.warning("agent port %d: still listened on by the fleet", port)

    def _adopt_streams(self):
        """The serving loop's listeners on the agent ports in line with the registry: closed where
        a handler asked or an entry now holds the port (one made at the front port, say), opened on
        the other free ports. A port it cannot listen on (an agent of the other fleet's there) is
        tried again at the next check, its failure logged once."""
        with self.adoption_lock:
            free = set(self.registry.free_ports())
            for port in list(self.adoption):
                if port in self.releases or port not in free:
                    self.adoption.pop(port).close()
            released = set(self.releases)
            for done in self.releases.values():
                done.set()
            self.releases.clear()
            room = max(0, ADOPT_MAX - len(self.adoption))
            for port in sorted(free - set(self.adoption) - released)[:room]:
                error, pstream = passive(f"ptcp:{port}:127.0.0.1")
                if error:
                    if port not in self.adopt_failed:
                        log.warning(
                            "agent port %d: cannot listen for re-admission: %s",
                            port,
                            os.strerror(error),
                        )
                        self.adopt_failed.add(port)
                    continue
                self.adopt_failed.discard(port)
                pstream.socket.setblocking(False)
                self.adoption[port] = pstream

    def bind(self, entry):
        """The entry's agent configuration written when new or changed, and its agent
        started, or restarted when the configuration changed or a secret of its was replaced
        (spec §4 step 4). Whether it is restarted."""
        path = self.config_dir / f"{entry['pod_id']}.json"
        config = agent_config(entry, self.config)
        missing, stale, replaced, unsourced = self.provision_secrets(config)
        self.note_missing_secrets(entry["pod_id"], missing, unsourced)
        self.note_stale_secrets(entry["pod_id"], stale)
        text = json.dumps(config, indent=2) + "\n"
        changed = path.exists() and path.read_text() != text
        if not path.exists() or changed:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text)
            os.replace(tmp, path)
        if replaced:  # its vault takes a reference's value as fixed for a run (spec 6)
            log.info(
                "pod %s: its agent restarts: secret %s replaced from the fleet's store",
                entry["pod_id"],
                ", ".join(replaced),
            )
        self.starter(entry["pod_id"], changed or bool(replaced))
        return changed or bool(replaced)

    def provision_secrets(self, config):
        """spec §4: each secret reference the agent configuration names (its uplink's, its GTP
        station's) kept in line with the fleet's store (``secrets``). One the agent's own store
        lacks is copied from the fleet's: created, durable, mode 0600, with its marker (the
        HMAC of the value under the agent store's own fingerprint key, beside it in the 0700
        store: the copy is the fleet's while it matches it). One the fleet provisioned that
        still matches its marker is replaced when the fleet's value changed (a changed
        onboarding key): written atomically, its marker updated, and the agent restarted (bind).
        One with no marker that equals the fleet's value is marked then (an agent provisioned
        before markers). Any other copy (placed or changed by hand) is never replaced, and no
        copy is ever deleted or emptied because the fleet's value went missing.
        (missing, stale, replaced, unsourced): the references with no source, in neither store
        (the agent's alone when the fleet has none, as after a factory reset that lost the
        agent's store); those whose own copy, not the fleet's, differs from the fleet's value;
        those replaced; and those whose copy is the fleet's but whose value is gone from the
        fleet's store (the copy kept). No value, marker or HMAC is ever logged."""
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
            return [], [], [], []
        fleet = FileSecrets(Path(self.config["secrets"])) if self.config.get("secrets") else None
        own = FileSecrets(Path(config["state_dir"]) / "secrets")
        pod = config["pod_id"]
        missing, stale, replaced, unsourced = [], [], [], []
        for ref in refs:
            source = have = None
            with contextlib.suppress(EmosaError):
                source = fleet.read(ref) if fleet is not None else None
            with contextlib.suppress(EmosaError):
                have = own.read(ref)
            if have is None:
                if source is None:
                    missing.append(ref)
                    continue
                try:
                    own.create(ref, source)
                except FileExistsError:
                    continue  # made meanwhile: that one stands, judged at the next bind
                replace_private(own.directory, marker_name(ref), marker(own, source))
                log.info("pod %s: secret %s provisioned from the fleet's store", pod, ref)
                continue
            mark = None
            with contextlib.suppress(EmosaError):
                mark = own.read(marker_name(ref))
            if source is None:
                # the fleet's own source gone: its copy is never deleted or emptied for that, only
                # recorded as missing; one placed by hand is the agent's alone
                if mark is not None and hmac.compare_digest(mark, marker(own, have)):
                    unsourced.append(ref)
                continue
            if hmac.compare_digest(have, source):
                if mark is None or not hmac.compare_digest(mark, marker(own, have)):
                    replace_private(own.directory, marker_name(ref), marker(own, have))
                    log.info("pod %s: secret %s equals the fleet's: marked its copy", pod, ref)
                continue
            if mark is not None and hmac.compare_digest(mark, marker(own, have)):
                replace_private(own.directory, ref, source)
                replace_private(own.directory, marker_name(ref), marker(own, source))
                replaced.append(ref)
                log.info(
                    "pod %s: secret %s replaced from the fleet's store (its source changed)",
                    pod,
                    ref,
                )
                continue
            stale.append(ref)
        return missing, stale, replaced, unsourced

    def note_missing_secrets(self, pod_id, missing, unsourced=()):
        """spec §4: a secret reference with no source fails loudly, without taking any pod off
        EMOSA: an error logged by reference only (never a value) each time the pod's agent is
        bound, and kept under the state root (fleet-secrets.missing.json: each pod and its
        references with no source) until it has one. The agent starts all the same. So does one
        whose copy is the fleet's but whose value is gone from the fleet's store (unsourced): the
        copy is kept and works on, the record says the fleet can no longer give it."""
        for ref in missing:
            log.error(
                "pod %s: secret %s in neither its store nor the fleet's: whatever needs it (its"
                " uplink, its GTP station) cannot come up until it has one (the fleet's secrets)",
                pod_id,
                ref,
            )
        for ref in unsourced:
            log.error(
                "pod %s: secret %s gone from the fleet's store: the fleet's copy in its store is"
                " kept and used, but no new value can reach it until the fleet's has one",
                pod_id,
                ref,
            )
        self._record_secrets(MISSING_SECRETS, pod_id, [*missing, *unsourced])

    def note_stale_secrets(self, pod_id, stale):
        """spec §4: an agent's own copy (placed or changed by hand, never replaced) that differs
        from the fleet's value: a warning by reference only each time the pod's agent is bound,
        and kept under the state root (fleet-secrets.stale.json, as the missing record) while it
        differs."""
        for ref in stale:
            log.warning(
                "pod %s: secret %s differs from the fleet's store and is its own (placed or"
                " changed by hand): kept, never replaced",
                pod_id,
                ref,
            )
        self._record_secrets(STALE_SECRETS, pod_id, stale)

    def _record_secrets(self, name, pod_id, refs):
        """A record of secret references under the state root: each pod and its references
        (sorted), the pod's replaced, dropped when it has none, the file removed when no pod
        has any; owner-only."""
        record = Path(self.config["state_root"]) / name
        with self.secrets_lock:
            try:
                kept = json.loads(record.read_text())
            except (OSError, ValueError):
                kept = {}
            if refs:
                kept[pod_id] = sorted(refs)
            else:
                kept.pop(pod_id, None)
            if kept:
                write_private(record, json.dumps(kept, indent=2, sort_keys=True) + "\n")
            else:
                record.unlink(missing_ok=True)

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
            if (
                self.admission(entry["pod_id"], entry.get("model"), entry.get("firmware"))[0]
                is None
            ):
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

    def _serve_one(self, stream, port=None):
        conn = ovs.jsonrpc.Connection(stream)
        try:
            self.handle(conn, stream.name, port)
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
        error, pstream = passive(self.config["listen"])
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
        self._adopt_streams()  # after the registry's agents started: their ports are theirs
        adopt_at = time.monotonic() + ADOPT_CHECK
        if ready is not None:
            ready.set()
        log.info("fleet: %s, agents on ports %s", self.config["listen"], self.config["ports"])
        try:
            while not self.stop.is_set():
                if retry_at is not None and time.monotonic() >= retry_at:  # the address yet?
                    files = self._files(failures)
                    failures += files is None
                    retry_at = None if files is not None else time.monotonic() + FILES_RETRY
                with self.adoption_lock:
                    asked = bool(self.releases)
                if asked or time.monotonic() >= adopt_at:
                    self._adopt_streams()
                    adopt_at = time.monotonic() + ADOPT_CHECK
                accepted = False
                for port, listener in [(None, pstream), *self.adoption.items()]:
                    error, stream = listener.accept()
                    if error == errno.EAGAIN:
                        continue
                    if error:
                        log.warning("accept: %s", os.strerror(error))
                        continue
                    accepted = True
                    if not self.slots.acquire(timeout=TIMEOUT):
                        stream.close()
                        continue
                    threading.Thread(
                        target=self._serve_one, args=(stream, port), daemon=True
                    ).start()
                if not accepted:
                    poller = ovs.poller.Poller()
                    for listener in [pstream, *self.adoption.values()]:
                        listener.wait(poller)
                    poller.timer_wait(200)
                    poller.block()
        finally:
            pstream.close()
            with self.adoption_lock:
                for listener in self.adoption.values():
                    listener.close()
                self.adoption.clear()
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
    logs.configure()

    def refuse_supervised(config):
        # spec §4: the agents as the fleet's own children are the C fleet's; refused before the
        # fleet writes anything (served_config keeps the configuration it serves)
        if isinstance(config, dict) and config.get("agents") == "supervised":
            parser.error(
                '"agents": "supervised" is emosa-fleet-c\'s; this fleet runs systemd units'
            )

    if args.command == "serve":  # a configuration that is not valid: the last valid one (spec 4)
        try:
            config = json.loads(args.config.read_text())
        except ValueError:
            config = None
        refuse_supervised(config)
        config = served_config(config, args.config)
    else:
        config = json.loads(args.config.read_text())
    if args.command != "list":  # the one served, the last valid one included
        refuse_supervised(config)
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
