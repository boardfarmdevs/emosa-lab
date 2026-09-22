"""Measure native peers in the owned VM; preserve every attempt and failure."""

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from node import BACKHAUL, FRONTHAUL, HOSTAP, UNITS
from setup import NODES, OWNER, ROOT, inside, lxc, run

CONTROLLER, AGENT, WIRED, WIFI = NODES
AL = "02:00:00:e0:00:02"
RUID = "02:00:00:ec:02:00"


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def node(name, action, *args):
    return inside(name, "python3", str(ROOT / "node.py"), action, *args)


def ap(name, *args, iface="wlan0"):
    return inside(name, str(HOSTAP / "bin/hostapd_cli"), "-i", iface, *args).strip()


def sta(name, *args, iface="wlan1"):
    return inside(name, str(HOSTAP / "bin/wpa_cli"), "-i", iface, *args).strip()


def values(output):
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def unit(name, action, item):
    if action == "reset-failed":
        state = inside(
            name,
            "systemctl",
            "show",
            f"emosa-baseline-{item}.service",
            "-p",
            "LoadState",
            "--value",
        ).strip()
        if state == "not-found":
            return ""
    return inside(name, "systemctl", action, f"emosa-baseline-{item}.service")


@contextmanager
def controller_policy_barrier(enabled):
    """Keep native 1905 traffic queued while restoring the controller's BSS policy."""

    def send(signum):
        inside(
            CONTROLLER,
            "systemctl",
            "kill",
            "--kill-whom=main",
            "--signal=" + signum,
            "emosa-baseline-transport.service",
        )

    if enabled:
        send("SIGSTOP")
    try:
        yield
    finally:
        if enabled:
            send("SIGCONT")


def check_ok(output):
    if output.strip() != "OK":
        raise RuntimeError(f"Control operation failed: {output}")


def journal(name):
    try:
        return inside(name, "journalctl", "--no-pager", "-u", "emosa-baseline-*")
    except subprocess.CalledProcessError as error:
        if error.returncode == 1 and (
            "-- No entries --" in error.stdout
            or "Failed to add filter for units: No data available" in error.stderr
        ):
            return error.stdout + error.stderr
        raise


def guard():
    if socket.gethostname() != "emosa-lab" or os.geteuid() != 0:
        raise SystemExit("Run only as root inside the dedicated emosa-lab VM")
    if run("systemd-detect-virt", "--vm").strip() != "kvm":
        raise SystemExit("Expected the dedicated KVM guest")
    state = json.loads((ROOT / "ownership.json").read_text())
    if state["owner"] != OWNER or not state["setup_complete"]:
        raise SystemExit("Owned baseline setup is incomplete")
    for name in NODES:
        if lxc("config", "get", name, "user.emosa.baseline").strip() != OWNER:
            raise SystemExit("Baseline ownership mismatch")
        if inside(name, "test", "!", "-e", "/sys/class/net/eth0"):
            raise SystemExit("Unexpected setup interface")


def stop_collect(directory, names=NODES):
    """Collect before resetting; abnormal native exits remain visible in JSON."""
    directory.mkdir(parents=True, exist_ok=False)
    results = {}
    for name in names:
        items = (*reversed(UNITS), "client", "data", "capture")
        outcomes = []
        for item in items:
            service = f"emosa-baseline-{item}.service"
            loaded = inside(name, "systemctl", "show", service, "-p", "LoadState", "--value")
            if loaded.strip() == "not-found":
                continue
            before = inside(
                name, "systemctl", "show", service, "-p", "ActiveState", "-p", "MainPID"
            )
            unit(name, "stop", item)
            after = inside(
                name, "systemctl", "show", service, "-p", "Result", "-p", "ExecMainStatus"
            )
            outcomes.append({"unit": item, "before": values(before), "after": values(after)})
        results[name] = outcomes
        write(directory / "shutdown.json", results)
        (directory / f"{name}-journal.txt").write_text(journal(name))
        # Keep logs/configs under an immutable attempt-specific directory in each node.
        archive = str(ROOT / "archive" / directory.parent.name / directory.name)
        inside(
            name,
            "python3",
            "-c",
            "\n".join(
                [
                    "from pathlib import Path",
                    "import shutil",
                    f"dest=Path({archive!r})",
                    "dest.mkdir(parents=True,exist_ok=False)",
                    "for item in ['/tmp/beerocks', '/opt/emosa-baseline/hostapd.log', "
                    "'/opt/emosa-baseline/backhaul.log', '/opt/emosa-baseline/client.log']:",
                    " p=Path(item)",
                    " if p.exists(): shutil.move(str(p),str(dest/p.name))",
                ]
            ),
        )
        for item in items:
            unit(name, "reset-failed", item)
    return results


class Attempt:
    def __init__(self, directory, mode, kind, policy="front-and-backhaul"):
        self.directory, self.mode, self.kind = directory, mode, kind
        self.policy = policy
        directory.mkdir(parents=True, exist_ok=False)
        self.started = time.monotonic()
        self.result = {
            "mode": mode,
            "kind": kind,
            "started_utc": datetime.now(UTC).isoformat(),
            "status": "running",
            "phases": {},
            "acceptance_version": 2,
            "post_client_stability_seconds": 30,
            "policy": policy,
        }
        self.capture = None
        self.capture_log = None
        self.ethernet_capture = False
        self.fresh_after = None
        self.client_batches = 0
        (directory / "runtime-reference.json").write_bytes((ROOT / "reference.json").read_bytes())
        self.save()

    def save(self):
        write(self.directory / "result.json", self.result)

    def poll(self, phase, operation, seconds=120, stable_seconds=0):
        started = time.monotonic()
        deadline = started + seconds
        observations = []
        ready_since = None
        while True:
            try:
                value = operation()
                ready = bool(value)
                observations.append(
                    {"elapsed": round(time.monotonic() - started, 3), "ready": ready}
                )
            except (subprocess.SubprocessError, ValueError, RuntimeError) as error:
                ready = False
                observations.append(
                    {"elapsed": round(time.monotonic() - started, 3), "error": str(error)}
                )
            if ready:
                if ready_since is None:
                    ready_since = time.monotonic()
            else:
                ready_since = None
            stable_for = 0 if ready_since is None else time.monotonic() - ready_since
            if stable_seconds:
                observations[-1]["stable_for"] = round(stable_for, 3)
            write(self.directory / f"{phase}-polls.json", observations)
            if ready and stable_for >= stable_seconds and time.monotonic() <= deadline:
                self.result["phases"][phase] = {
                    "seconds": observations[-1]["elapsed"],
                    "budget": seconds,
                }
                if stable_seconds:
                    self.result["phases"][phase]["required_stable_seconds"] = stable_seconds
                self.save()
                print(self.directory.name, phase, observations[-1]["elapsed"], "s", flush=True)
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{phase} did not pass within {seconds} seconds")
            time.sleep(1)

    def captures(self):
        run("ip", "link", "set", "hwsim0", "up")
        self.capture_log = (self.directory / "capture.log").open("w")
        self.capture = subprocess.Popen(
            ["tcpdump", "-U", "-i", "hwsim0", "-s", "0", "-w", str(self.directory / "radio.pcap")],
            stdout=self.capture_log,
            stderr=self.capture_log,
        )
        inside(
            CONTROLLER,
            "systemd-run",
            "--quiet",
            "--property=Type=exec",
            "--unit",
            "emosa-baseline-capture",
            "tcpdump",
            "-U",
            "-i",
            "br-lan",
            "-s",
            "0",
            "-w",
            str(ROOT / "ethernet.pcap"),
            "ether",
            "proto",
            "0x893a",
        )
        self.ethernet_capture = True
        time.sleep(0.5)
        if self.capture.poll() is not None:
            raise RuntimeError("Radio capture exited before measurement")

    def inventory(self):
        raw = inside(
            CONTROLLER,
            "ubus",
            "call",
            "Device.WiFi.DataElements.Network",
            "_get",
            '{"rel_path":"","depth":6}',
        )
        decoder = json.JSONDecoder()
        objects = []
        while raw.strip():
            obj, end = decoder.raw_decode(raw.lstrip())
            objects.append(obj)
            raw = raw.lstrip()[end:]
        if not objects or any(obj.get("amxd-error-code", 0) for obj in objects):
            raise RuntimeError("Controller inventory read failed")
        data = objects[0]
        write(self.directory / "controller-inventory.json", data)
        devices = [
            k
            for k, v in data.items()
            if v.get("ID") == AL
            and re.fullmatch(r"Device\.WiFi\.DataElements\.Network\.Device\.\d+\.", k)
        ]
        if len(devices) != 1:
            return False
        prefix = devices[0]
        if self.fresh_after is not None:
            stamp = data.get(prefix + "MultiAPDevice.", {}).get("LastContactTime", "")
            if (
                not stamp
                or datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                <= self.fresh_after
            ):
                return False
        radios = [
            k
            for k, v in data.items()
            if re.fullmatch(re.escape(prefix) + r"Radio\.\d+\.", k) and v.get("ID") == RUID
        ]
        expected_bss = 1 if self.policy == "sole-fronthaul" else 2
        if len(radios) != 1 or data[radios[0]].get("BSSNumberOfEntries") != expected_bss:
            return False
        bss_rows = [
            v for k, v in data.items() if re.fullmatch(re.escape(radios[0]) + r"BSS\.\d+\.", k)
        ]
        bsses = {v.get("BSSID"): v for v in bss_rows}
        if len(bss_rows) != expected_bss or len(bsses) != expected_bss:
            return False
        expected = [(RUID, FRONTHAUL, True)]
        if self.policy != "sole-fronthaul":
            expected.append(("02:00:00:ec:02:01", BACKHAUL, False))
        for bssid, ssid, front in expected:
            bss = bsses.get(bssid, {})
            if (
                bss.get("SSID") != ssid
                or not bss.get("Enabled")
                or bss.get("FronthaulUse") != front
                or bss.get("BackhaulUse") != (not front)
            ):
                return False
        backhaul = data.get(prefix + "MultiAPDevice.Backhaul.", {})
        write(
            self.directory / "inventory-identity.json",
            {"device": prefix, "radio": radios[0], "backhaul": backhaul},
        )
        expected_link = "Ethernet" if self.mode == "wired" else "Wi-Fi"
        return (
            backhaul.get("BackhaulDeviceID") == "02:00:00:e0:00:01"
            and backhaul.get("LinkType") == expected_link
        )

    def applied(self, name):
        status = ap(name, "status")
        (self.directory / f"{name}-hostap-status.txt").write_text(status)
        info = values(status)
        scope_ok = (
            "ssid[1]" not in info
            if name == AGENT and self.policy == "sole-fronthaul"
            else info.get("ssid[1]") == BACKHAUL
        )
        return info.get("state") == "ENABLED" and info.get("ssid[0]") == FRONTHAUL and scope_ok

    def operational(self, name):
        raw = inside(
            name,
            "ubus",
            "call",
            "X_PRPLWARE-COM_Agent.Info",
            "_get",
            '{"rel_path":"","depth":3}',
        )
        objects = []
        decoder = json.JSONDecoder()
        while raw.strip():
            obj, end = decoder.raw_decode(raw.lstrip())
            objects.append(obj)
            raw = raw.lstrip()[end:]
        if not objects or any(obj.get("amxd-error-code", 0) for obj in objects):
            raise RuntimeError("Native agent operational state read failed")
        data = objects[0]
        write(self.directory / f"{name}-agent-state.json", data)
        with (self.directory / f"{name}-agent-state.jsonl").open("a") as stream:
            stream.write(
                json.dumps({"elapsed": time.monotonic() - self.started, "state": data}) + "\n"
            )
        info = data.get("X_PRPLWARE-COM_Agent.Info.", {})
        fronts = [
            v
            for k, v in data.items()
            if re.fullmatch(r"X_PRPLWARE-COM_Agent\.Info\.Fronthaul\.\d+\.", k)
        ]
        return (
            info.get("CurrentState") == "OPERATIONAL (15)"
            and len(fronts) == 1
            and fronts[0].get("Iface") == "wlan0"
            and fronts[0].get("CurrentState") == "OPERATIONAL (4)"
        )

    def ready(self):
        checks = {}
        for label, operation in (
            ("external-agent-operational", lambda: self.operational(AGENT)),
            ("local-agent-operational", lambda: self.operational(CONTROLLER)),
            ("external-ap", lambda: self.applied(AGENT)),
            ("local-ap", lambda: self.applied(CONTROLLER)),
            ("controller-inventory", self.inventory),
            ("backhaul", lambda: self.mode == "wired" or self.backhaul()),
        ):
            checks[label] = bool(operation())
            if not checks[label]:
                break
        with (self.directory / "readiness.jsonl").open("a") as stream:
            stream.write(
                json.dumps({"elapsed": time.monotonic() - self.started, "checks": checks}) + "\n"
            )
        return len(checks) == 6 and all(checks.values())

    def stability(self):
        started = time.monotonic()
        samples = []
        while True:
            ready = self.ready()
            samples.append({"elapsed": round(time.monotonic() - started, 3), "ready": ready})
            write(self.directory / "post-client-stability.json", samples)
            if not ready:
                raise RuntimeError("Native onboarding regressed during post-client stability check")
            if time.monotonic() - started >= 30:
                break
            time.sleep(1)
        # Fresh traffic after the dwell cannot be satisfied by the earlier HTTP response.
        self.clients(prepare=False)
        if not self.ready():
            raise RuntimeError("Native onboarding regressed after final client probes")
        self.result["stability"] = {"seconds": samples[-1]["elapsed"], "samples": len(samples)}
        self.save()

    def backhaul(self):
        status = sta(AGENT, "status")
        info = values(status)
        write(self.directory / "backhaul-status.json", info)
        (self.directory / "backhaul-link.txt").write_text(
            inside(AGENT, "iw", "dev", "wlan1", "link")
        )
        return all(
            info.get(k) == v
            for k, v in {
                "wpa_state": "COMPLETED",
                "ssid": BACKHAUL,
                "bssid": "02:00:00:ec:01:01",
                "key_mgmt": "WPA2-PSK",
            }.items()
        )

    def bootstrap(self):
        empty = inside(AGENT, "cat", str(ROOT / "backhaul.conf"))
        (self.directory / "backhaul-before.conf").write_text(empty)
        if "network=" in empty or "psk=" in empty:
            raise RuntimeError("Wireless bootstrap was supplied credentials")
        node(AGENT, "supplicant")
        check_ok(ap(CONTROLLER, "wps_pbc"))
        check_ok(sta(AGENT, "wps_pbc", "02:00:00:ec:01:00", "multi_ap=1"))
        self.poll("wps-backhaul", self.backhaul)
        learned = inside(AGENT, "cat", str(ROOT / "backhaul.conf"))
        (self.directory / "backhaul-learned.conf").write_text(learned)
        if "multi_ap_backhaul_sta=1" not in learned:
            raise RuntimeError("WPS did not create a Multi-AP backhaul profile")
        network = values(sta(AGENT, "status"))["id"]
        check_ok(sta(AGENT, "set_network", network, "bssid", "02:00:00:ec:01:01"))
        check_ok(sta(AGENT, "save_config"))
        (self.directory / "agent-wireless-interfaces.txt").write_text(inside(AGENT, "iw", "dev"))
        (self.directory / "controller-backhaul-stations.txt").write_text(
            ap(CONTROLLER, "all_sta", iface="wlan0.0")
        )

    def clients(self, prepare):
        self.client_batches += 1
        batch = {"nonce": str(uuid.uuid4()), "clients": {}}
        batch_path = self.directory / f"client-probes-{self.client_batches:02d}.json"
        nonce = batch["nonce"]
        payload = json.dumps({"nonce": nonce, "attempt": self.directory.name})
        inside(
            CONTROLLER,
            "python3",
            "-c",
            f"from pathlib import Path; "
            f"Path({str(ROOT / 'response.json')!r}).write_text({payload!r})",
        )
        if prepare:
            inside(
                CONTROLLER,
                "systemd-run",
                "--quiet",
                "--property=Type=exec",
                "--unit",
                "emosa-baseline-data",
                "python3",
                str(ROOT / "observer.py"),
                "serve",
            )
            for name in (WIRED, WIFI):
                inside(name, "python3", str(ROOT / "observer.py"), "prepare")

        def observed(name):
            output = inside(name, "python3", str(ROOT / "observer.py"), "observe", "--nonce", nonce)
            observation = json.loads(output)
            write(self.directory / f"{name}-observation.json", observation)
            batch["clients"][name] = observation
            write(batch_path, batch)
            return True

        for name in (WIRED, WIFI):
            self.poll(
                f"clients-{self.client_batches:02d}-{name}",
                lambda name=name: observed(name),
                30,
            )

    def clean(self):
        entries = json.loads(lxc("list", AGENT, "--format", "json"))
        devices = next(item for item in entries if item["name"] == AGENT)["devices"]
        backhauls = [key for key, value in devices.items() if value.get("name") == "eth1"]
        if len(backhauls) > 1 or any(
            devices[key].get("type") != "nic" or devices[key].get("network") != "em-base-bh"
            for key in backhauls
        ):
            raise RuntimeError("Unexpected agent eth1 attachment; preserve and inspect it")
        if self.mode == "wireless" and backhauls:
            lxc("config", "device", "remove", AGENT, backhauls[0])
        elif self.mode == "wired" and not backhauls:
            lxc(
                "config",
                "device",
                "add",
                AGENT,
                "backhaul",
                "nic",
                "network=em-base-bh",
                "name=eth1",
            )
        for name in (CONTROLLER, AGENT):
            node(name, "prepare", "--backhaul", self.mode, "--policy", self.policy)
            node(name, "hostap")
            self.poll(
                name + "-initial-ap",
                lambda name=name: values(ap(name, "status")).get("state") == "ENABLED",
                15,
            )
            initial = ap(name, "status")
            (self.directory / f"{name}-initial-status.txt").write_text(initial)
            if FRONTHAUL in initial or BACKHAUL in initial:
                raise RuntimeError("Agent/controller BSS was already provisioned")
        self.captures()
        node(CONTROLLER, "services")
        node(CONTROLLER, "agent")
        self.poll(
            "platform",
            lambda: (
                "BML_RET_OK"
                in inside(
                    CONTROLLER, "/opt/prpl-install-nl80211/bin/beerocks_cli", "-c", "bml_ping"
                )
            ),
            15,
        )
        (self.directory / "policy.txt").write_text(
            node(CONTROLLER, "policy", "--policy", self.policy)
        )
        self.poll("controller-ap", lambda: self.applied(CONTROLLER))
        if self.mode == "wireless":
            self.bootstrap()
        node(AGENT, "services")
        node(AGENT, "agent")
        self.poll("onboarding", self.ready)
        if self.mode == "wireless" and not self.backhaul():
            raise RuntimeError("Wireless backhaul lost during onboarding")
        self.clients(prepare=True)
        self.stability()

    def recovery(self):
        if self.kind == "backhaul-loss" and not self.ready():
            raise RuntimeError("Backhaul outage must start from a healthy provisioned baseline")
        self.captures()
        self.fresh_after = time.time()
        if self.kind in {"agent-restart", "controller-restart"}:
            name, item = (
                (AGENT, "agent") if self.kind == "agent-restart" else (CONTROLLER, "controller")
            )
            self.result["policy_startup_barrier"] = self.kind == "controller-restart"
            with controller_policy_barrier(self.kind == "controller-restart"):
                if self.kind == "controller-restart":
                    helper_before = inside(
                        CONTROLLER,
                        "systemctl",
                        "show",
                        "emosa-baseline-agent.service",
                        "-p",
                        "MainPID",
                        "-p",
                        "ActiveState",
                    )
                    if values(helper_before).get("ActiveState") != "active":
                        raise RuntimeError("Controller helper was not running")
                    unit(CONTROLLER, "stop", "agent")
                    helper_after = inside(
                        CONTROLLER,
                        "systemctl",
                        "show",
                        "emosa-baseline-agent.service",
                        "-p",
                        "Result",
                        "-p",
                        "ExecMainStatus",
                    )
                    write(
                        self.directory / "helper-shutdown.json",
                        {"before": values(helper_before), "after": values(helper_after)},
                    )
                    unit(CONTROLLER, "reset-failed", "agent")
                    self.result["controller_restart_scope"] = ["controller", "local-agent"]
                before = inside(
                    name,
                    "systemctl",
                    "show",
                    f"emosa-baseline-{item}.service",
                    "-p",
                    "MainPID",
                    "-p",
                    "ActiveState",
                )
                if values(before).get("ActiveState") != "active":
                    raise RuntimeError("Restart target was not running")
                unit(name, "stop", item)
                after = inside(
                    name,
                    "systemctl",
                    "show",
                    f"emosa-baseline-{item}.service",
                    "-p",
                    "Result",
                    "-p",
                    "ExecMainStatus",
                )
                write(
                    self.directory / "restart-shutdown.json",
                    {"node": name, "before": values(before), "after": values(after)},
                )
                self.result["shutdown_clean"] = values(after).get("Result") == "success"
                unit(name, "reset-failed", item)
                node(name, item)
                if self.kind == "controller-restart":
                    node(CONTROLLER, "agent")
                    self.result["shutdown_clean"] = (
                        self.result["shutdown_clean"]
                        and values(helper_after).get("Result") == "success"
                    )
                    self.poll(
                        "controller-ready",
                        lambda: (
                            "BML_RET_OK"
                            in inside(
                                CONTROLLER,
                                "/opt/prpl-install-nl80211/bin/beerocks_cli",
                                "-c",
                                "bml_ping",
                            )
                        ),
                        15,
                    )
                    (self.directory / "policy.txt").write_text(node(CONTROLLER, "policy"))
        elif self.kind == "backhaul-loss":
            try:
                if self.mode == "wired":
                    inside(AGENT, "ip", "link", "set", "eth1", "down")
                else:
                    check_ok(sta(AGENT, "disable_network", "all"))
                time.sleep(10)
                observations = []
                for name, iface in [(WIRED, "eth1"), (WIFI, "wlan0")]:
                    try:
                        output = inside(
                            name, "ping", "-n", "-I", iface, "-c", "1", "-W", "2", "192.0.2.1"
                        )
                        observations.append({"node": name, "returncode": 0, "output": output})
                    except subprocess.CalledProcessError as error:
                        observations.append(
                            {
                                "node": name,
                                "returncode": error.returncode,
                                "output": error.stdout,
                                "stderr": error.stderr,
                            }
                        )
                write(self.directory / "during-outage.json", observations)
                if any(item["returncode"] == 0 for item in observations):
                    raise RuntimeError(
                        "A client still reached the controller during the backhaul outage"
                    )
            finally:
                if self.mode == "wired":
                    inside(AGENT, "ip", "link", "set", "eth1", "up")
                else:
                    check_ok(sta(AGENT, "enable_network", "all"))
            self.fresh_after = time.time()
        else:
            raise RuntimeError("Unknown recovery operation")
        self.poll("recovery", self.ready, stable_seconds=30 if self.kind == "backhaul-loss" else 0)
        self.clients(prepare=False)
        self.stability()

    def protocol_evidence(self):
        def decode(filename, display_filter, fields):
            args = [
                "tshark",
                "-r",
                str(self.directory / filename),
                "-Y",
                display_filter,
                "-T",
                "fields",
            ]
            for field in fields:
                args.extend(["-e", field])
            return run(*args)

        decoded = decode(
            "ethernet.pcap",
            "ieee1905",
            ["frame.number", "eth.src", "eth.dst", "ieee1905.message_type", "wps.message_type"],
        )
        (self.directory / "ieee1905.tsv").write_text(decoded)
        rows = [line.split("\t") for line in decoded.splitlines()]
        handshake = {}
        for label, source, message, wps in [
            ("search", AL, "0x0007", None),
            ("response", "02:00:00:e0:00:01", "0x0008", None),
            ("m1", AL, "0x0009", "0x04"),
            ("m2", "02:00:00:e0:00:01", "0x0009", "0x05"),
        ]:
            handshake[label] = [
                row[0]
                for row in rows
                if row[1] == source
                and row[3] == message
                and (wps is None or wps in row[4].split(","))
            ]
        report = {
            "decoder": run("tshark", "--version").splitlines()[0],
            "handshake_frames": handshake,
            "normative_validation": "not_claimed",
        }
        if self.kind == "clean" and not all(handshake.values()):
            raise RuntimeError("Capture lacks the native discovery/M1/M2 exchange")
        if self.kind != "clean" and not any(row[1] == AL for row in rows):
            raise RuntimeError("Capture lacks fresh traffic from the external agent")
        if self.mode == "wireless":
            four = decode(
                "radio.pcap",
                "wlan.fc.tods == 1 && wlan.fc.fromds == 1 && "
                "((wlan.ta == 02:00:00:ec:02:10 && wlan.ra == 02:00:00:ec:01:01) || "
                "(wlan.ra == 02:00:00:ec:02:10 && wlan.ta == 02:00:00:ec:01:01))",
                ["frame.number", "wlan.ta", "wlan.ra"],
            )
            (self.directory / "four-address.tsv").write_text(four)
            report["four_address_frames"] = len(four.splitlines())
            if not four.strip():
                raise RuntimeError("Capture lacks four-address backhaul frames")
            if self.kind == "clean":
                wps = decode(
                    "radio.pcap",
                    "wps.message_type && (wlan.sa == 02:00:00:ec:02:10 || "
                    "wlan.da == 02:00:00:ec:02:10)",
                    ["frame.number", "wlan.sa", "wlan.da", "wps.message_type"],
                )
                (self.directory / "wps.tsv").write_text(wps)
                present = {row.split("\t")[-1] for row in wps.splitlines()}
                report["wps_m1_to_m8_observed"] = (
                    set(["0x04", "0x05", "0x07", "0x08", "0x09", "0x0a", "0x0b", "0x0c"]) <= present
                )
                if not report["wps_m1_to_m8_observed"]:
                    raise RuntimeError("Capture lacks a complete WPS M1–M8 exchange")
        write(self.directory / "protocol-observation.json", report)

    def collect(self):
        # Stop captures before optional observations can fail during teardown.
        if self.capture:
            if self.capture.poll() is None:
                self.capture.send_signal(signal.SIGINT)
            self.capture.wait(timeout=10)
            self.capture_log.close()
        if self.ethernet_capture:
            unit(CONTROLLER, "stop", "capture")
            lxc(
                "file",
                "pull",
                "--quiet",
                CONTROLLER + str(ROOT / "ethernet.pcap"),
                str(self.directory / "ethernet.pcap"),
            )
            unit(CONTROLLER, "reset-failed", "capture")
        for name in NODES:
            (self.directory / f"{name}-ip.json").write_text(inside(name, "ip", "-j", "address"))
            if name != WIRED:
                (self.directory / f"{name}-iw.txt").write_text(inside(name, "iw", "dev"))
            (self.directory / f"{name}-journal.txt").write_text(journal(name))
        for name in (CONTROLLER, AGENT):
            (self.directory / f"{name}-ap.conf").write_text(
                inside(name, "cat", "/var/run/hostapd-phy0.conf")
            )

    def execute(self):
        try:
            if self.kind == "clean":
                self.clean()
            else:
                self.recovery()
            self.result["status"] = "passed"
        except (Exception, SystemExit) as error:
            self.result["status"] = "failed"
            self.result["error"] = str(error)
            if isinstance(error, subprocess.CalledProcessError):
                self.result["command_stdout"] = error.stdout
                self.result["command_stderr"] = error.stderr
        finally:
            try:
                self.collect()
                if self.result["status"] == "passed":
                    self.protocol_evidence()
            except Exception as error:
                self.result["collection_error"] = str(error)
                self.result["status"] = "failed"
            self.result["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
            self.save()
            print(
                self.directory.name, self.result["status"], self.result.get("error", ""), flush=True
            )
        return self.result["status"] == "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("wired", "wireless"), required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--policy", choices=("front-and-backhaul", "sole-fronthaul"), default="front-and-backhaul"
    )
    parser.add_argument(
        "--suite", action="store_true", help="Five clean starts and three of each recovery"
    )
    parser.add_argument(
        "--kind",
        choices=("clean", "agent-restart", "controller-restart", "backhaul-loss"),
        default="clean",
    )
    args = parser.parse_args()
    if args.policy == "sole-fronthaul" and (
        args.mode != "wired" or args.kind != "clean" or args.suite
    ):
        parser.error("Sole-fronthaul currently supports a single clean wired trial")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,70}", args.label):
        raise SystemExit("Use a unique lowercase label")
    guard()
    suite = ROOT / "runs" / args.label
    suite.mkdir(parents=True, exist_ok=False)
    (suite / "harness").mkdir()
    for path in Path(__file__).parent.glob("*.py"):
        (suite / "harness" / path.name).write_bytes(path.read_bytes())
    write(
        suite / "harness.json",
        {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parent.glob("*.py")
        },
    )
    (suite / "kernel.txt").write_text(run("uname", "-a") + run("iw", "reg", "get"))
    write(suite / "containers.json", json.loads(lxc("list", "--format", "json")))
    if args.suite and args.kind != "clean":
        raise SystemExit("A suite starts with clean onboarding")
    kinds = (
        ["clean"] * 5 + ["agent-restart"] * 3 + ["controller-restart"] * 3 + ["backhaul-loss"] * 3
        if args.suite
        else [args.kind]
    )
    counts, results = {}, []
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
        label = f"{kind}-{counts[kind]:02d}"
        if kind == "clean":
            stop_collect(suite / f"before-{label}")
        attempt = Attempt(suite / label, args.mode, kind, args.policy)
        passed = attempt.execute()
        results.append(attempt.result)
        write(
            suite / "summary.json",
            {
                "planned_attempts": len(kinds),
                "executed_attempts": len(results),
                "results": results,
                "all_passed": passed and len(results) == len(kinds),
            },
        )
        if not passed:
            break
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
