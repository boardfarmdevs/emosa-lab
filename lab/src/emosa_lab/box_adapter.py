# SPDX-License-Identifier: Apache-2.0
"""The lab in a box for the adapter around the agents (easymesh-labs alignment plan 8.3):
the fleet (spec §4) and the GRE termination point (spec §8.2), each run by either
implementation, Python or C, as the agent scenarios run either agent.

The fleet's scenarios hand the box's pod to the real fleet at its front port, as an
operator's redirector does, and play the pod's ``cm``: when the fleet writes
``manager_addr``, the pod's database dials the agent there. The agents' units are a
``systemctl`` of the box's (plain processes, as ``emosa-agent@.service`` runs them). The
GTP's scenario runs it on links of the box's own: an underlay bridge with the
pod-backhaul AP's port, a LAN port, and the lease events dnsmasq would report.
"""

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from emosa_lab.box import (
    AUTOCONFIG_SEARCH,
    AUTOCONFIG_WSC,
    ROOT,
    Box,
    agent_command,
    dial,
    onboarded,
    provision,
    wait_for_port,
)

FLEET_PORT = 6650
FLEET_PORTS = [6651, 6652]
FLEET_INTERFACE = "em1"  # the fleet's name for its first agent's interface
FLEET_ADDRESS = f"tcp:127.0.0.1:{FLEET_PORT}"
AGENT_ADDRESS = f"tcp:127.0.0.1:{FLEET_PORTS[0]}"

SYSTEMCTL = '''#!{python}
"""systemctl in the lab in a box: emosa-agent@POD units as plain processes, as
deploy/adapter's unit runs them (emosa-agent-link gives the interface its AL MAC first)."""
import json, os, signal, subprocess, sys, time
from pathlib import Path

units = Path(os.environ["EMOSA_BOX_UNITS"])
with (units / "systemctl.log").open("a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
words = [a for a in sys.argv[1:] if not a.startswith("-")]
action, pod = words[0], words[-1].split("@", 1)[1]
pidfile = units / f"{{pod}}.pid"


def running():
    try:
        pid = int(pidfile.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def stop():
    pid = running()
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if running() is None:
                break
            time.sleep(0.1)
    pidfile.unlink(missing_ok=True)


def start():
    path = Path(os.environ["EMOSA_BOX_CONFIG_DIR"]) / f"{{pod}}.json"
    config = json.loads(path.read_text())
    link = ["ip", "link", "set", config["interface"], "address", config["al_mac"]]
    subprocess.run(link, check=True)
    agent = json.loads(os.environ["EMOSA_BOX_AGENT"])
    command = [str(path) if a == "{{config}}" else a for a in agent]
    log = (units / f"{{pod}}.log").open("a")
    process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
    pidfile.write_text(str(process.pid))


if action == "start" and running() is None:
    start()
elif action == "restart":
    stop()
    start()
elif action == "disable" and "--now" in sys.argv:
    stop()
'''


def other(implementation):
    return "python" if implementation == "c" else "c"


def alive(pid):
    """A process that runs (a zombie, ended but not reaped yet, does not)."""
    try:
        os.kill(pid, 0)
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


class AgentProcess:
    """The agent a fleet started (through the box's systemctl), by its pid file: running
    (None) also while there is none yet, as a starting process is."""

    def __init__(self, pidfile):
        self.pidfile = pidfile

    def pid(self):
        try:
            return int(self.pidfile.read_text())
        except (OSError, ValueError):
            return None

    def poll(self):
        pid = self.pid()
        return None if pid is None or alive(pid) else 0

    def terminate(self):
        pid = self.pid()
        if pid and self.poll() is None:
            os.kill(pid, signal.SIGTERM)

    def kill(self):
        pid = self.pid()
        if pid and self.poll() is None:
            os.kill(pid, signal.SIGKILL)

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout or 0)
        while self.poll() is None:
            if timeout is not None and time.monotonic() > deadline:
                raise subprocess.TimeoutExpired("agent", timeout)
            time.sleep(0.1)
        return 0


class FleetBox(Box):
    """The box with the real fleet in front of the agent: the pod is handed to the fleet,
    which starts the agent and hands the pod over to it."""

    def __init__(self, agent, directory, *, fleet=None, first=None, handover_seconds=20, **options):
        super().__init__(agent, directory, **options)
        self.interface = FLEET_INTERFACE
        self.settings = fleet or {}
        self.implementation = first or agent  # whose fleet runs now
        self.handover_seconds = handover_seconds
        self.fleet_process = self.fleet_log = None
        self.handed = False

    def fleet_command(self, command, *args, implementation=None):
        implementation = implementation or self.implementation
        if implementation == "c":
            agent = Path(self.binary) if self.binary else ROOT / "c/build/emosa-agent-c"
            return [str(agent.parent / "emosa-fleet-c"), command, str(self.fleet_path), *args]
        return [sys.executable, "-m", "emosa.agent.fleet", command, str(self.fleet_path), *args]

    async def start(self, config, fleet):
        self.config_dir = self.directory / "etc"
        self.units = self.directory / "units"
        self.units.mkdir(exist_ok=True)
        bin_dir = self.directory / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "systemctl").write_text(SYSTEMCTL.format(python=sys.executable))
        (bin_dir / "systemctl").chmod(0o755)
        self.fleet_config = {
            "listen": f"ptcp:{FLEET_PORT}:127.0.0.1",
            "advertise": "127.0.0.1",
            "ports": FLEET_PORTS,
            **fleet,
            "config_dir": str(self.config_dir),
            **self.settings,
        }
        self.fleet_path = self.directory / "fleet.json"
        self.fleet_env = {
            **self.env,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "EMOSA_BOX_UNITS": str(self.units),
            "EMOSA_BOX_CONFIG_DIR": str(self.config_dir),
            "EMOSA_BOX_AGENT": json.dumps(agent_command(self.agent, "{config}", self.binary)),
        }
        self.process = AgentProcess(self.units / f"{self.serial}.pid")
        self.fleet_log = (self.directory / "fleet.log").open("a")
        await self.start_fleet()
        self.handed = await self.hand_over(self.handover_seconds)

    async def start_fleet(self):
        self.fleet_path.write_text(json.dumps(self.fleet_config, indent=2) + "\n")
        self.fleet_log.write(f"--- the {self.implementation} fleet\n")
        self.fleet_log.flush()
        self.fleet_process = subprocess.Popen(
            self.fleet_command("serve"),
            stdout=self.fleet_log,
            stderr=self.fleet_log,
            env=self.fleet_env,
        )
        await wait_for_port(FLEET_PORT)

    def stop_fleet(self):
        if self.fleet_process and self.fleet_process.poll() is None:
            self.fleet_process.terminate()
            try:
                self.fleet_process.wait(10)
            except subprocess.TimeoutExpired:
                self.fleet_process.kill()

    async def restart_fleet(self, implementation=None, **settings):
        """The fleet stopped and started again: another implementation's, or with other
        settings, on the same files."""
        self.stop_fleet()
        self.implementation = implementation or self.implementation
        self.fleet_config.update(settings)
        await self.start_fleet()

    async def manager_addr(self):
        rows = await self.rows("AWLAN_Node")
        return next(iter(rows.values()), {}).get("manager_addr")

    async def hand_over(self, seconds=20):
        """The pod handed to the fleet (its redirector's manager_addr), then, as its cm
        does on a new manager_addr, dialing there instead. True once handed over."""
        await self.transact(
            [
                {
                    "op": "update",
                    "table": "AWLAN_Node",
                    "where": [],
                    "row": {"manager_addr": FLEET_ADDRESS},
                }
            ]
        )
        await dial(self.db, FLEET_ADDRESS)
        deadline, handed = time.monotonic() + seconds, False
        while time.monotonic() < deadline and not handed:
            handed = await self.manager_addr() == AGENT_ADDRESS
            if not handed:
                await asyncio.sleep(0.2)
        await dial(self.db, FLEET_ADDRESS, connect=False)
        if handed:
            await dial(self.db, AGENT_ADDRESS)
        return handed

    async def return_to_fleet(self, seconds=20):
        """The pod back at the front port, as after a reboot or its cloud's redirect."""
        await dial(self.db, AGENT_ADDRESS, connect=False)
        return await self.hand_over(seconds)

    def registry(self):
        """The fleet's pods, as the running implementation's list command shows them."""
        out = subprocess.run(
            self.fleet_command("list"), capture_output=True, text=True, env=self.fleet_env
        )
        try:
            return json.loads(out.stdout)
        except ValueError:
            return {"error": out.stderr[-300:]}

    def systemctl(self):
        try:
            return (self.units / "systemctl.log").read_text().splitlines()
        except OSError:
            return []

    async def __aexit__(self, *exc):
        await super().__aexit__(*exc)
        self.stop_fleet()
        for pidfile in self.units.glob("*.pid") if hasattr(self, "units") else ():
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
        if self.fleet_log:
            self.fleet_log.close()


def unit_actions(lines, serial):
    """The actions on the pod's agent unit, in order (enable, start, restart, disable)."""
    unit = f"emosa-agent@{serial}"
    words = ([w for w in line.split() if not w.startswith("-")] for line in lines if unit in line)
    return [w[0] for w in words]


async def fleet_handover(box):
    """spec §4: a pod handed to the fleet gets its agent: a registry entry (the first
    port, em1, its AL MAC), its configuration, the unit enabled and started, manager_addr
    written; the agent onboards the pod."""
    entry = box.registry().get(box.serial, {})
    config = box.config_dir / f"{box.serial}.json"
    actions = unit_actions(box.systemctl(), box.serial)
    onboarding = await onboarded(box) if box.handed else {"failed": "not handed over"}
    passed = (
        box.handed
        and entry.get("port") == FLEET_PORTS[0]
        and entry.get("interface") == FLEET_INTERFACE
        and entry.get("al_mac") == box.al_mac
        and config.is_file()
        and actions[:2] == ["enable", "start"]
        and bool(onboarding.get("applied"))
    )
    return box.result(passed=passed, entry=entry, units=actions, onboarding=onboarding)


async def fleet_return(box):
    """A pod back at the front port (a reboot, its cloud's redirect again) gets the same
    agent: the same entry and port, one more handover, its unit started (a no-op), never
    restarted; the agent, still the same process, provisions the pod again."""
    first = await onboarded(box)
    before = box.registry().get(box.serial, {})
    pid = box.process.pid()
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    back = await box.return_to_fleet()
    after = box.registry().get(box.serial, {})
    actions = unit_actions(box.systemctl(), box.serial)
    again = await provision(box, searches, m1s) if back else False
    passed = (
        bool(first.get("applied"))
        and back
        and after.get("port") == before.get("port")
        and after.get("al_mac") == before.get("al_mac")
        and after.get("handovers", 0) > before.get("handovers", 0)
        and "restart" not in actions
        and box.process.pid() == pid
        and again
    )
    return box.result(
        passed=passed,
        handovers=[before.get("handovers"), after.get("handovers")],
        units=actions,
        same_process=box.process.pid() == pid,
        provisioning_again=again,
    )


async def fleet_upgrade(box):
    """An image upgrade (spec §4): the gateway's persistent storage keeps the registry and
    the configurations, not the agents' enabled units. The agent and the fleet stopped, the
    fleet started again: it starts the registry's agent at once (enabled, started, not
    restarted), with no pod at its front port; the pod, still dialing the agent's port, is
    provisioned again."""
    first = await onboarded(box)
    before = box.registry().get(box.serial, {})
    pid = box.process.pid()
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.stop_fleet()
    box.process.terminate()  # the old image's agent, gone with its unit link
    box.process.wait(15)
    (box.units / f"{box.serial}.pid").unlink(missing_ok=True)
    logged = len(box.systemctl())
    await box.restart_fleet()
    started = await box.until(lambda: box.process.pid(), seconds=15)
    actions = unit_actions(box.systemctl()[logged:], box.serial)
    again = await provision(box, searches, m1s) if started else False
    after = box.registry().get(box.serial, {})
    passed = (
        bool(first.get("applied"))
        and bool(started)
        and started != pid
        and actions == ["enable", "start"]
        and after.get("handovers") == before.get("handovers")
        and again
    )
    return box.result(
        passed=passed,
        units_after_start=actions,
        new_process=bool(started) and started != pid,
        handovers=[before.get("handovers"), after.get("handovers")],
        provisioning_again=again,
    )


OTHER_POD = "MVXPOD0000000000AA"


async def fleet_refusals(box):
    """Pods the fleet leaves unchanged (spec §4, steps 1 and 2): one not admitted, one with
    an unusable serial, one for which no port is free. manager_addr is not written, no
    agent configured or started for it. The registry's own pod, which holds the port, gets
    its agent when the fleet starts (spec §4)."""
    not_admitted = not box.handed
    await box.transact(
        [
            {
                "op": "update",
                "table": "AWLAN_Node",
                "where": [],
                "row": {"serial_number": "bad serial"},
            }
        ]
    )
    await box.restart_fleet(admit="*")
    unusable = not await box.hand_over(6)
    await box.transact(
        [{"op": "update", "table": "AWLAN_Node", "where": [], "row": {"serial_number": box.serial}}]
    )
    registry = Path(box.fleet_config["state_root"]) / "fleet.json"
    registry.write_text(
        json.dumps(
            {
                OTHER_POD: {
                    "pod_id": OTHER_POD,
                    "al_mac": "02:11:22:33:44:55",
                    "port": FLEET_PORTS[0],
                    "interface": "em1",
                    "first_seen": 1759500000.5,
                    "last_seen": 1759500000.5,
                    "handovers": 1,
                    "node_id": OTHER_POD,
                    "model": None,
                    "firmware": None,
                }
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    await box.restart_fleet(ports=[FLEET_PORTS[0], FLEET_PORTS[0]])
    full = not await box.hand_over(6)
    listed = box.registry()
    configs = sorted(p.name for p in box.config_dir.glob("*.json"))
    actions = unit_actions(box.systemctl(), box.serial)
    registered = unit_actions(box.systemctl(), OTHER_POD)
    passed = not_admitted and unusable and full and list(listed) == [OTHER_POD]
    passed = passed and configs == [f"{OTHER_POD}.json"] and not actions
    passed = passed and registered == ["enable", "start"]
    return box.result(
        passed=passed,
        not_admitted=not_admitted,
        unusable_serial=unusable,
        no_free_port=full,
        registry=sorted(listed),
        configs=configs,
        units=actions,
        registered_units=registered,
    )


async def fleet_takeover(box):
    """Either fleet takes over from the other: the other implementation's fleet hands
    the pod over first; this one, started on the same registry and configurations, lists
    the same entry, and the returning pod gets the same agent, left running (its
    configuration's text is the same, so not restarted)."""
    first = box.handed
    before = box.registry().get(box.serial, {})
    pid = box.process.pid()
    await box.restart_fleet(box.agent)
    listed = box.registry().get(box.serial, {})
    back = await box.return_to_fleet()
    after = box.registry().get(box.serial, {})
    actions = unit_actions(box.systemctl(), box.serial)
    passed = (
        first
        and listed == before
        and back
        and after.get("port") == before.get("port")
        and after.get("al_mac") == before.get("al_mac")
        and after.get("handovers", 0) > before.get("handovers", 0)
        and "restart" not in actions
        and box.process.pid() == pid
    )
    return box.result(
        passed=passed,
        first_fleet=other(box.agent),
        listed_alike=listed == before,
        handovers=[before.get("handovers"), after.get("handovers")],
        units=actions,
        same_process=box.process.pid() == pid,
    )


async def fleet_forget(box):
    """forget releases a pod (spec §4): its agent stopped and disabled, its entry and
    configuration gone, its state directory archived; handed over again, it starts a new
    entry with a new agent."""
    first = await onboarded(box)
    pid = box.process.pid()
    out = subprocess.run(
        box.fleet_command("forget", box.serial), capture_output=True, text=True, env=box.fleet_env
    )
    try:
        released = json.loads(out.stdout) or {}
    except ValueError:
        released = {}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and pid and alive(pid):
        await asyncio.sleep(0.2)
    stopped = bool(pid) and not alive(pid)
    archived = Path(released.get("archived_state") or "/nonexistent")
    gone = box.serial not in box.registry() and not (box.config_dir / f"{box.serial}.json").exists()
    back = await box.return_to_fleet()
    after = box.registry().get(box.serial, {})
    actions = unit_actions(box.systemctl(), box.serial)
    checks = {
        "onboarded": bool(first.get("applied")),
        "released": released.get("pod_id") == box.serial,
        "stopped": stopped,
        "archived": archived.is_dir(),
        "entry_and_configuration_gone": gone,
        "handed_over_again": back,
        "a_new_entry": after.get("first_seen", 0) > released.get("first_seen", 0),
        "disabled": "disable" in actions,
        "a_new_agent": box.process.poll() is None,
    }
    return box.result(
        passed=all(checks.values()),
        checks=checks,
        released=released.get("pod_id"),
        archived=archived.name if archived.is_dir() else None,
        stopped=stopped,
        units=actions,
        handovers_after=after.get("handovers"),
    )


# -- the GTP ------------------------------------------------------------------------------

GTP_CONFIG = {
    "underlay": {
        "interface": "podbh",
        "address": "169.254.2.1/25",
        "mtu": 1600,
        "dhcp_range": ["169.254.2.10", "169.254.2.126"],
        "lease_time": "1h",
    },
    "lan": {"bridge": "br-gtp", "ports": ["eth1"]},
    "tunnel_mtu": 1562,
}


def ip_json(*args):
    out = subprocess.run(["ip", "-j", *args], capture_output=True, text=True)
    try:
        return json.loads(out.stdout or "[]")
    except ValueError:
        return []


def link(name):
    found = ip_json("-d", "link", "show", "dev", name)
    return found[0] if found else None


def tunnel_ok(name, remote, mtu, bridge):
    t = link(name)
    info = (t or {}).get("linkinfo", {})
    data = info.get("info_data", {})
    return bool(
        t
        and info.get("info_kind") == "gretap"
        and data.get("remote") == remote
        and data.get("local") == "169.254.2.1"
        and t.get("mtu") == mtu
        and t.get("master") == bridge
    )


async def gtp(directory, implementation, binary=None):
    """spec §8.2: the GTP on links of the box's own. setup addresses the underlay (raising
    its port's MTU too), bridges the LAN port and writes a dnsmasq configuration dnsmasq
    accepts; the lease events dnsmasq reports, through the hook setup wrote, add one
    gretap per leased pod into the LAN bridge (a renewal changes nothing) and delete it;
    reconcile makes the tunnels the current leases; a lease outside the underlay is
    refused."""
    directory.mkdir(parents=True, exist_ok=True)
    for command in (
        "link set lo up",
        "link add podbh type bridge",
        "link add wlan0 type dummy",
        "link set wlan0 master podbh up",
        "link add eth1 type dummy",
        "link set eth1 up",
    ):
        subprocess.run(["ip", *command.split()], check=True)
    state = directory / "gtp"
    config_path = directory / "gtp.json"
    config_path.write_text(json.dumps({**GTP_CONFIG, "state_dir": str(state)}, indent=2) + "\n")
    if implementation == "c":
        agent = Path(binary) if binary else ROOT / "c/build/emosa-agent-c"
        command = [str(agent.parent / "emosa-gtp-c")]
    else:
        command = [sys.executable, "-m", "emosa.gtp"]
    env = {**os.environ, "EMOSA_SCHEMAS": str(ROOT / "schemas")}
    log = (directory / "gtp.log").open("w")

    def run(*args):
        out = subprocess.run(args, capture_output=True, text=True, env=env)
        log.write(f"$ {' '.join(map(str, args))}\n{out.stdout}{out.stderr}")
        try:
            return out.returncode, json.loads(out.stdout or "null")
        except ValueError:
            return out.returncode, None

    checks = {}
    code, _ = run(*command, "setup", config_path)
    address = [
        a
        for a in (ip_json("addr", "show", "dev", "podbh") or [{}])[0].get("addr_info", [])
        if a.get("local") == "169.254.2.1" and a.get("prefixlen") == 25
    ]
    checks["setup"] = (
        code == 0
        and bool(address)
        and (link("podbh") or {}).get("mtu") == 1600
        and (link("wlan0") or {}).get("mtu") == 1600
        and (link("eth1") or {}).get("master") == "br-gtp"
        and (link("podbh") or {}).get("master") is None
    )
    hook = state / "hook"
    checks["dnsmasq_configuration"] = hook.is_file() and os.access(hook, os.X_OK)
    if shutil.which("dnsmasq"):
        test = subprocess.run(
            ["dnsmasq", "--test", f"--conf-file={state / 'dnsmasq.conf'}"],
            capture_output=True,
            text=True,
        )
        checks["dnsmasq_configuration"] = checks["dnsmasq_configuration"] and test.returncode == 0
    first, second = ("02:00:00:00:05:00", "169.254.2.57"), ("02:00:00:00:06:00", "169.254.2.60")
    code, listed = run(hook, "add", *first, "pod-1")
    checks["lease_add"] = code == 0 and tunnel_ok("gtp2_57", "169.254.2.57", 1562, "br-gtp")
    index = (link("gtp2_57") or {}).get("ifindex")
    code, _ = run(hook, "old", *first)
    checks["lease_renewal"] = code == 0 and (link("gtp2_57") or {}).get("ifindex") == index
    code, _ = run(hook, "add", *second)
    checks["second_lease"] = code == 0 and tunnel_ok("gtp2_60", "169.254.2.60", 1562, "br-gtp")
    (state / "leases").write_text(f"0 {second[0]} {second[1]} pod-2 *\n")
    code, listed = run(*command, "reconcile", config_path)
    checks["reconcile"] = (
        code == 0
        and listed == {"gtp2_60": {"mac": second[0], "remote": second[1]}}
        and link("gtp2_57") is None
    )
    code, _ = run(hook, "del", *second)
    checks["lease_del"] = code == 0 and link("gtp2_60") is None
    code, _ = run(hook, "add", first[0], "169.254.1.57")
    checks["outside_refused"] = code == 1 and link("gtp1_57") is None
    code, listed = run(*command, "list", config_path)
    checks["list"] = code == 0 and listed == {}
    log.close()
    return {"scenario": "gtp", "agent": implementation, "passed": all(checks.values()), **checks}


SCENARIOS = {
    "fleet-handover": fleet_handover,
    "fleet-return": fleet_return,
    "fleet-upgrade": fleet_upgrade,
    "fleet-refusals": fleet_refusals,
    "fleet-takeover": fleet_takeover,
    "fleet-forget": fleet_forget,
}
OPTIONS = {
    "fleet-refusals": {"fleet": {"admit": [OTHER_POD]}, "handover_seconds": 6},
}
STANDALONE = {"gtp": gtp}  # no pod, agent or controller: links of the box's own


def first_fleet(scenario, agent):
    return other(agent) if scenario == "fleet-takeover" else agent


async def run(scenario, agent, directory, *, binary=None):
    if scenario in STANDALONE:
        return await STANDALONE[scenario](directory, agent, binary)
    options = {**OPTIONS.get(scenario, {}), "first": first_fleet(scenario, agent)}
    async with FleetBox(agent, directory, binary=binary, **options) as box:
        return await SCENARIOS[scenario](box)
