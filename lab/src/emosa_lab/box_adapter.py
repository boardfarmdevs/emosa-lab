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
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

from emosa_lab.box import (
    AUTOCONFIG_SEARCH,
    AUTOCONFIG_WSC,
    ROOT,
    Box,
    agent_command,
    back,
    dial,
    kept_reconnections,
    onboarded,
    provision,
    wait_for_port,
)

FLEET_PORT = 6650
FLEET_PORTS = [6651, 6652]
FLEET_INTERFACE = "em1"  # the fleet's name for its first agent's interface
FILES_PORT = 6660  # the file server's in the box (6650 by default: the box's front port here)
MODULE_FILE = b"#!/bin/sh\n# a module's file, served by its SHA-256\n"
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

LINK = '''#!{python}
"""emosa-agent-link in the lab in a box (spec §4, a supervised fleet's): the agent's
interface given its AL MAC, as the systemctl above does."""
import json, os, subprocess, sys
from pathlib import Path

pod = sys.argv[1]
config = json.loads((Path(os.environ["EMOSA_AGENT_CONFIG_DIR"]) / f"{{pod}}.json").read_text())
subprocess.run(["ip", "link", "set", config["interface"], "address", config["al_mac"]], check=True)
print("link", pod, config["interface"], config["al_mac"], flush=True)
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

    def __init__(
        self,
        agent,
        directory,
        *,
        fleet=None,
        first=None,
        handover_seconds=20,
        supervised=False,
        **options,
    ):
        super().__init__(agent, directory, **options)
        self.interface = FLEET_INTERFACE
        self.settings = fleet or {}
        self.implementation = first or agent  # whose fleet runs now
        self.handover_seconds = handover_seconds
        self.supervised = supervised  # the C fleet's own children instead of units (spec §4)
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
            # the platform's state root, as /etc/default/emosa gives it on a gateway (spec 4)
            "EMOSA_STATE_ROOT": self.fleet_config["state_root"],
        }
        self.process = AgentProcess(self.units / f"{self.serial}.pid")
        if self.supervised:
            self.supervise(bin_dir)
        self.fleet_log = (self.directory / "fleet.log").open("a")
        await self.start_fleet()
        self.handed = await self.hand_over(self.handover_seconds)

    def supervise(self, bin_dir):
        """The fleet supervises the agents itself: the box's link helper and agent, its own
        environment files' directory (empty: not the host's /etc/default), each agent's
        output and pid file under the run root."""
        self.run_root = self.directory / "run"
        defaults = self.directory / "default"
        defaults.mkdir(exist_ok=True)
        link = bin_dir / "emosa-agent-link"
        link.write_text(LINK.format(python=sys.executable))
        link.chmod(0o755)
        command = agent_command(self.agent, "{config}", self.binary)
        words = ['"$1"' if a == "{config}" else shlex.quote(a) for a in command]
        agent = bin_dir / "emosa-agent"
        agent.write_text("#!/bin/sh\nexec " + " ".join(words) + "\n")
        agent.chmod(0o755)
        self.fleet_config.update(agents="supervised", run_root=str(self.run_root))
        self.fleet_env.update(
            EMOSA_DEFAULTS_DIR=str(defaults), EMOSA_AGENT=str(agent), EMOSA_AGENT_LINK=str(link)
        )
        self.process = AgentProcess(self.run_root / self.serial / "agent.pid")

    async def start_fleet(self, text=None):
        """The fleet started on its configuration (``text``: the file's own, whatever it is)."""
        self.fleet_path.write_text(
            json.dumps(self.fleet_config, indent=2) + "\n" if text is None else text
        )
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
    """A pod back at the front port (its cloud's redirect again) gets the same agent: the same
    entry and port, one more handover, its unit started (a no-op), never restarted; the agent,
    still the same process, keeps the pod's session (spec 2.5: the same pod and database back
    within the reconnection window)."""
    first = await onboarded(box)
    before = box.registry().get(box.serial, {})
    pid = box.process.pid()
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    continued = kept_reconnections(box)
    returned = await box.return_to_fleet()
    after = box.registry().get(box.serial, {})
    actions = unit_actions(box.systemctl(), box.serial)
    mode = await back(box, searches, m1s, continued) if returned else None
    passed = (
        bool(first.get("applied"))
        and returned
        and after.get("port") == before.get("port")
        and after.get("al_mac") == before.get("al_mac")
        and after.get("handovers", 0) > before.get("handovers", 0)
        and "restart" not in actions
        and box.process.pid() == pid
        and mode == "kept"
    )
    return box.result(
        passed=passed,
        handovers=[before.get("handovers"), after.get("handovers")],
        units=actions,
        same_process=box.process.pid() == pid,
        session=mode,
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


async def fleet_readmit(box):
    """spec 4, re-admission: a factory reset of the gateway. The fleet and the agent stopped, the
    registry and the agent's state gone, the configurations back (the fleet's), the fleet started
    again: it starts no agent (its registry is empty), and the pod, still dialing its agent's
    port, reaches the fleet's listener there. It is re-admitted on that port, with no turn
    through the front port: the same port, interface and AL MAC, its agent enabled and started,
    the pod provisioned again."""
    first = await onboarded(box)
    before = box.registry().get(box.serial, {})
    pid = box.process.pid()
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.stop_fleet()
    box.process.terminate()
    box.process.wait(15)
    (box.units / f"{box.serial}.pid").unlink(missing_ok=True)
    state = Path(box.fleet_config["state_root"])
    (state / "fleet.json").unlink(missing_ok=True)
    shutil.rmtree(state / box.serial, ignore_errors=True)  # the agent's state, lost with it
    logged = len(box.systemctl())
    await box.restart_fleet()
    started = await box.until(lambda: box.process.pid(), seconds=20)
    actions = unit_actions(box.systemctl()[logged:], box.serial)
    again = await provision(box, searches, m1s) if started else False
    after = box.registry().get(box.serial, {})
    log = (box.directory / "fleet.log").read_text(errors="replace").split("--- the ")[-1]
    same = {key: after.get(key) == before.get(key) for key in ("port", "interface", "al_mac")}
    passed = (
        bool(first.get("applied"))
        and bool(started)
        and started != pid
        and actions[:2] == ["enable", "start"]
        and all(same.values())
        and after.get("handovers") == 1
        and "re-admitted on the agent port it dialled" in log
        and again
    )
    return box.result(
        passed=passed,
        entry=after,
        same=same,
        units_after_start=actions,
        new_process=bool(started) and started != pid,
        provisioning_again=again,
    )


async def fleet_config_truncated(box):
    """spec 4: the likeliest bad file in the field, a partial write: the fleet's configuration cut
    in half, which names no state root. The fleet stopped with its agent gone, started again on it:
    it rejects it ("not JSON"), logs it once and records it under the platform's state root
    (EMOSA_STATE_ROOT, as /etc/default/emosa gives it), keeps running on the last valid
    configuration kept there and starts the registry's agent on it, which provisions the pod
    again."""
    first = await onboarded(box)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.stop_fleet()
    box.process.terminate()
    box.process.wait(15)
    (box.units / f"{box.serial}.pid").unlink(missing_ok=True)
    text = json.dumps(box.fleet_config, indent=2) + "\n"
    await box.start_fleet(text[: len(text) // 2])
    started = await box.until(lambda: box.process.pid(), seconds=15)
    again = await provision(box, searches, m1s) if started else False
    await asyncio.sleep(3)
    root = Path(box.fleet_env["EMOSA_STATE_ROOT"])
    record = root / "fleet-config.rejected.json"
    rejected = json.loads(record.read_text()) if record.is_file() else {}
    log = (box.directory / "fleet.log").read_text(errors="replace").split("--- the ")[-1]
    checks = {
        "onboarded": bool(first.get("applied")),
        "fleet_running": box.fleet_process.poll() is None,
        "agent_started": bool(started),
        "provisioned_again": again,
        "rejected_once": log.count("rejected") == 1,
        "recorded_not_json": rejected.get("error") == "not JSON",
    }
    return box.result(passed=all(checks.values()), checks=checks, rejected=rejected)


async def fleet_config_rejected(box):
    """spec 4 (15 October): one pod's key its schema does not know kept every pod off EMOSA, the
    fleet ending at once and started again and again. The fleet stopped with its agent gone (as
    the router's supervised agents go with it), its configuration given that key: the fleet
    started again rejects it, logs it once, records the key (fleet-config.rejected.json), keeps
    running on the last valid configuration and starts the registry's agent on it, which
    provisions the pod again."""
    first = await onboarded(box)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    box.stop_fleet()
    box.process.terminate()
    box.process.wait(15)
    (box.units / f"{box.serial}.pid").unlink(missing_ok=True)
    box.fleet_config.setdefault("pods", {})[box.serial] = {"not_a_setting": True}
    await box.restart_fleet()
    started = await box.until(lambda: box.process.pid(), seconds=15)
    again = await provision(box, searches, m1s) if started else False
    await asyncio.sleep(3)
    root = Path(box.fleet_config["state_root"])
    record = root / "fleet-config.rejected.json"
    rejected = json.loads(record.read_text()) if record.is_file() else {}
    log = (box.directory / "fleet.log").read_text(errors="replace").split("--- the ")[-1]
    checks = {
        "onboarded": bool(first.get("applied")),
        "fleet_running": box.fleet_process.poll() is None,
        "agent_started": bool(started),
        "provisioned_again": again,
        "rejected_once": log.count("rejected") == 1,
        "the_key_recorded": "not_a_setting" in rejected.get("error", ""),
        "last_valid_kept": (root / "fleet-config.valid.json").is_file(),
    }
    return box.result(passed=all(checks.values()), checks=checks, rejected=rejected)


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


LEGACY_MODEL = "BCM_LEGACY_POD"  # the legacy fixture's AWLAN_Node model (firmware 2.4.3-0-legacy)
LEGACY_PROFILE = "bcm-pod-2.4.3-gre-v1"
PLAIN_PROFILE = "opensync-lab-hwsim-6.6.1-v1"


async def fleet_admit_by_model(box):
    """spec 4, the pods' models: a legacy pod that no serial admits, of a model the fleet's
    models know (firmware 2.4.3), whose profile only that entry names (the fleet's own is 6.6's,
    which the agent would refuse the pod on). With the router's admit switch off the fleet
    refuses it and writes nothing; turned on, the fleet admits it by its model and says so, its
    agent gets the model's profile and onboards it."""
    refused = not box.handed and not list(box.config_dir.glob("*.json"))
    await box.restart_fleet(admit_by_model=True)
    handed = await box.hand_over(box.handover_seconds)
    path = box.config_dir / f"{box.serial}.json"
    profile = json.loads(path.read_text()).get("profile") if path.is_file() else None
    said = (
        f"admitted by model, models[0] ({LEGACY_MODEL})"
        in (box.directory / "fleet.log").read_text()
    )
    onboarding = await onboarded(box) if handed else {"failed": "not handed over"}
    refused_pod = (box.status() or {}).get("pod_refused")
    passed = refused and handed and profile == LEGACY_PROFILE and said
    passed = passed and bool(onboarding.get("applied")) and not refused_pod
    return box.result(
        passed=passed,
        refused_while_off=refused,
        handed_over=handed,
        profile=profile,
        logged=said,
        onboarding=onboarding,
        pod_refused=refused_pod,
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
    configuration gone, its state directory archived without the agent's status (its link
    into the run directory would show the next agent's); handed over again, it starts a
    new entry with a new agent."""
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
    status = archived / "status.json"
    gone = box.serial not in box.registry() and not (box.config_dir / f"{box.serial}.json").exists()
    back = await box.return_to_fleet()
    after = box.registry().get(box.serial, {})
    actions = unit_actions(box.systemctl(), box.serial)
    checks = {
        "onboarded": bool(first.get("applied")),
        "released": released.get("pod_id") == box.serial,
        "stopped": stopped,
        "archived": archived.is_dir(),
        "archived_without_status": not status.exists() and not status.is_symlink(),
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


async def new_agent(box, old, seconds=15):
    """An agent other than old, once it runs: its pid, or None."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        box.controller.poll()
        pid = box.process.pid()
        if pid and pid != old and alive(pid):
            return pid
        await asyncio.sleep(0.2)
    return None


async def ended(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and pid and alive(pid):
        await asyncio.sleep(0.2)
    return bool(pid) and not alive(pid)


async def fleet_supervised(box):
    """spec §4: the C fleet without systemd, the agents its own children. The pod is
    handed over and onboarded with no systemctl; an agent that ends is started again and
    provisions the pod again; the fleet stopped stops its agent, and started again starts
    the registry's agent at once; forget, its own process, stops the agent, which is not
    started again; the agent's output is in its log under the run root."""
    first = await onboarded(box)
    pid = box.process.pid()
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    if pid:
        os.kill(pid, signal.SIGKILL)
    restarted = await new_agent(box, pid)
    again = await provision(box, searches, m1s) if restarted else False
    box.stop_fleet()
    with_the_fleet = await ended(restarted)
    searches, m1s = len(box.sent(AUTOCONFIG_SEARCH)), len(box.sent(AUTOCONFIG_WSC))
    await box.restart_fleet()
    registered = await new_agent(box, restarted)
    after_start = await provision(box, searches, m1s) if registered else False
    out = subprocess.run(
        box.fleet_command("forget", box.serial), capture_output=True, text=True, env=box.fleet_env
    )
    forgotten = out.returncode == 0 and await ended(registered)
    await asyncio.sleep(5)  # past the restart delay (3 s)
    log = box.run_root / box.serial / "agent.log"
    checks = {
        "onboarded": bool(first.get("applied")),
        "started_again_after_it_ended": bool(restarted),
        "provisioned_again": again,
        "stopped_with_the_fleet": with_the_fleet,
        "started_from_the_registry": bool(registered),
        "provisioned_after_the_fleet_started": after_start,
        "stopped_by_forget": forgotten,
        "not_started_again": box.process.pid() is None,
        "its_log": log.is_file() and log.stat().st_size > 0,
        "no_systemctl": not box.systemctl(),
    }
    return box.result(passed=all(checks.values()), checks=checks)


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
    gretap per leased pod into the LAN bridge (a renewal changes nothing) and delete it once
    the lease ran its length; reconcile makes the tunnels the current leases; a lease outside
    the underlay is refused. A "del" before the lease ran its length on the monotonic
    clock (the system clock stepped) keeps the tunnel, and a kept tunnel goes once a whole
    lease passes without a renewal (here leases of a few seconds, as dnsmasq gives the hook
    in DNSMASQ_LEASE_LENGTH)."""
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

    def run(*args, length=None):
        lease_env = {**env, "DNSMASQ_LEASE_LENGTH": str(length)} if length is not None else env
        out = subprocess.run(args, capture_output=True, text=True, env=lease_env)
        log.write(f"$ {' '.join(map(str, args))}\n{out.stdout}{out.stderr}")
        log.flush()
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
    code, _ = run(hook, "old", *second, length=1)  # its last renewal, a 1 s lease
    time.sleep(1.5)
    code, _ = run(hook, "del", *second)
    checks["lease_del"] = code == 0 and link("gtp2_60") is None
    # the clock steps forward and dnsmasq ends an hour's lease at once: the tunnel stays
    third, fourth = ("02:00:00:00:07:00", "169.254.2.61"), ("02:00:00:00:08:00", "169.254.2.62")
    run(hook, "add", *third)
    code, _ = run(hook, "del", *third)
    checks["clock_step_kept"] = (
        code == 0
        and tunnel_ok("gtp2_61", "169.254.2.61", 1562, "br-gtp")
        and "the system clock stepped" in (directory / "gtp.log").read_text()
    )
    # a 4 s lease ended at once (kept), then no renewal for a whole lease: the next event sweeps it
    run(hook, "old", *third, length=4)
    run(hook, "del", *third)
    kept = link("gtp2_61") is not None
    time.sleep(4.5)
    code, _ = run(hook, "add", *fourth, length=1)
    checks["kept_tunnel_swept"] = code == 0 and kept and link("gtp2_61") is None
    time.sleep(1.5)
    run(hook, "del", *fourth)
    code, _ = run(hook, "add", first[0], "169.254.1.57")
    checks["outside_refused"] = code == 1 and link("gtp1_57") is None
    code, listed = run(*command, "list", config_path)
    checks["list"] = code == 0 and listed == {}
    log.close()
    return {"scenario": "gtp", "agent": implementation, "passed": all(checks.values()), **checks}


def fetch(url, method="GET"):
    """(status, headers, body) of one plain HTTP request."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), b""


async def fleet_files(box):
    """spec 4, the pods' modules: the fleet with files (a module's file named by its SHA-256, and
    one whose content is not its name) and a module with that update. The agent's configuration
    gets the module's settings with update_url (the fleet's file server on its advertise address)
    and update_sha256. HEAD / answers 200, an empty body and a Date in GMT (now); GET /<sha256>
    the file; the misnamed file and any other path 404, each with a Date. The fleet logs the
    file served and the one it does not serve. Its clock then not synchronized (the box's
    EMOSA_FILES_CLOCK, as the kernel or files.time_synced_command would say): no reply carries a
    Date, HEAD / is 503 with a Retry-After, the file is still served, and the fleet logs it."""
    files = box.directory / "files"
    files.mkdir()
    sha = hashlib.sha256(MODULE_FILE).hexdigest()
    (files / sha).write_bytes(MODULE_FILE)
    (files / ("0" * 64)).write_bytes(b"not its name")
    box.fleet_env["EMOSA_FILES_CLOCK"] = "synchronized"  # whatever the host's clock says
    await box.restart_fleet(
        files={"directory": str(files), "port": FILES_PORT},
        modules={"hook": {"config": {"redirector": "auto"}, "update": sha}},
    )
    await wait_for_port(FILES_PORT)
    base = f"http://127.0.0.1:{FILES_PORT}"
    head = await asyncio.to_thread(fetch, base + "/", "HEAD")
    got = await asyncio.to_thread(fetch, f"{base}/{sha}")
    misnamed = await asyncio.to_thread(fetch, f"{base}/{'0' * 64}")
    other = await asyncio.to_thread(fetch, f"{base}/fleet.json")
    date = head[1].get("Date") or ""
    skew = abs(parsedate_to_datetime(date).timestamp() - time.time()) if date else None
    config = json.loads((box.config_dir / f"{box.serial}.json").read_text())
    box.fleet_env["EMOSA_FILES_CLOCK"] = "unsynchronized"
    await box.restart_fleet()
    await wait_for_port(FILES_PORT)
    untimed = await asyncio.to_thread(fetch, base + "/", "HEAD")
    untimed_file = await asyncio.to_thread(fetch, f"{base}/{sha}")
    # the fleet started before it can listen (a gateway's LAN bridge gets the address minutes
    # later, 14 October; here another listener holds the port): it serves its pods meanwhile and
    # tries again every 10 s, so the file server answers once the port is free
    box.stop_fleet()
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # the last replies' TIME_WAIT
    holder.bind(("127.0.0.1", FILES_PORT))
    holder.listen()
    await box.start_fleet()
    await asyncio.sleep(2)
    waiting = "trying again every 10 s" in (box.directory / "fleet.log").read_text(errors="replace")
    holder.close()
    late = (None, {}, b"")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and late[0] is None:
        await asyncio.sleep(1)
        with contextlib.suppress(OSError):
            late = await asyncio.to_thread(fetch, base + "/", "HEAD")
    log = (box.directory / "fleet.log").read_text(errors="replace")
    passed = (
        head[0] == 200
        and head[2] == b""
        and date.endswith(" GMT")
        and skew is not None
        and skew < 60
        and got[0] == 200
        and got[2] == MODULE_FILE
        and misnamed[0] == 404
        and bool(misnamed[1].get("Date"))
        and other[0] == 404
        and config.get("modules")
        == {
            "hook": {
                "redirector": "auto",
                "update_url": f"{base}/{sha}",
                "update_sha256": sha,
            }
        }
        and f"files: 127.0.0.1 GET /{sha}: 200" in log
        and "not served" in log
        and untimed[0] == 503
        and untimed[1].get("Retry-After") == "60"
        and not untimed[1].get("Date")
        and untimed_file[0] == 200
        and untimed_file[2] == MODULE_FILE
        and not untimed_file[1].get("Date")
        and "not synchronized: no time served" in log
        and waiting
        and late[0] == 503
        and f"files: serving on 127.0.0.1:{FILES_PORT} after" in log
    )
    return box.result(
        passed=passed,
        head=[head[0], date],
        file=[got[0], len(got[2])],
        misnamed=misnamed[0],
        other=other[0],
        modules=config.get("modules"),
        unsynchronized=[untimed[0], untimed[1].get("Date"), untimed_file[0]],
        late_listener=[waiting, late[0]],
    )


SCENARIOS = {
    "fleet-handover": fleet_handover,
    "fleet-return": fleet_return,
    "fleet-upgrade": fleet_upgrade,
    "fleet-readmit": fleet_readmit,
    "fleet-refusals": fleet_refusals,
    "fleet-admit-by-model": fleet_admit_by_model,
    "fleet-takeover": fleet_takeover,
    "fleet-forget": fleet_forget,
    "fleet-supervised": fleet_supervised,
    "fleet-files": fleet_files,
    "fleet-config-rejected": fleet_config_rejected,
    "fleet-config-truncated": fleet_config_truncated,
}
OPTIONS = {
    "fleet-refusals": {"fleet": {"admit": [OTHER_POD]}, "handover_seconds": 6},
    "fleet-admit-by-model": {
        "legacy": True,
        "fleet": {
            "admit": [OTHER_POD],
            "profile": PLAIN_PROFILE,
            "models": [
                {"model": LEGACY_MODEL, "firmware_prefix": "2.4.3", "profile": LEGACY_PROFILE}
            ],
        },
        "handover_seconds": 6,
    },
    "fleet-supervised": {"supervised": True},
}
STANDALONE = {"gtp": gtp}  # no pod, agent or controller: links of the box's own


def first_fleet(scenario, agent):
    """The fleet that runs first: the other implementation's for a takeover, the C fleet
    to supervise either agent, the agent's own otherwise."""
    if scenario == "fleet-supervised":
        return "c"
    return other(agent) if scenario == "fleet-takeover" else agent


async def run(scenario, agent, directory, *, binary=None):
    if scenario in STANDALONE:
        return await STANDALONE[scenario](directory, agent, binary)
    options = {**OPTIONS.get(scenario, {}), "first": first_fleet(scenario, agent)}
    async with FleetBox(agent, directory, binary=binary, **options) as box:
        return await SCENARIOS[scenario](box)
