"""Owned native controller → EMOSA → simulated pod → independent radio/client.

Run only on the existing emosa-lab VM. A candidate wrapper backs up/restores the
controller. No native agent runs on the simulated pod. Every label is retained.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import runpy
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from run import CONTROLLER, node, stop_collect
from setup import ROOT, run

from emosa.opensync.session import OvsSession
from emosa.simulation.database import SimDatabase
from emosa.simulation.native_onboarding import OWNER, RADIO_ROOT
from emosa.simulation.radio import MONITOR, seed_radio_database
from emosa.simulation.wsc_provisioning import SERIAL
from emosa.simulation.wsc_wire import RADIO_BSSID, write

RADIO_ROOT_DIR = RADIO_ROOT.parent


def inventory_bss(objects):
    rows = {key: value for obj in objects for key, value in obj.items()}
    device = next(
        (k for k, v in rows.items() if isinstance(v, dict) and v.get("ID") == "02:00:00:00:30:01"),
        None,
    )
    return {
        k: v
        for k, v in rows.items()
        if device and k.startswith(device) and re.search(r"Radio\.\d+\.(BSS\.\d+\.)?$", k)
    }


def inventory_stations(objects):
    bsses = [k for k, v in inventory_bss(objects).items() if v.get("BSSID") == RADIO_BSSID]
    if len(bsses) != 1:
        return {}
    return {
        k: v
        for obj in objects
        for k, v in obj.items()
        if k.startswith(bsses[0] + "STA.")
        and isinstance(v, dict)
        and v.get("MACAddress") == "02:00:00:00:02:00"
    }


async def experiment(label, *, active_seconds=0):
    helpers = runpy.run_path(str(ROOT / "controller-trial.py"))
    helpers["idle"]()
    sys.path.insert(0, str(RADIO_ROOT_DIR))
    radio = runpy.run_path(str(RADIO_ROOT_DIR / "run.py"))
    discovery = runpy.run_path(str(ROOT / "discovery-trial.py"))
    directory = RADIO_ROOT / label
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write(directory / "native-owner.json", OWNER)
    report = {
        "label": label,
        "status": "running",
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "native_controller": True,
        "semantic_submission": False,
        "cases": {},
        "active_seconds_requested": active_seconds,
        "sustained_operation_proven": False,
    }
    write(directory / "result.json", report)
    write(
        directory / "source-hashes.json",
        {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (
                Path(__file__),
                ROOT / "controller-trial.py",
                ROOT / "node.py",
                ROOT / "prplmesh.reference.json",
                RADIO_ROOT_DIR / "manager.py",
                RADIO_ROOT_DIR / "node.py",
                *sorted((RADIO_ROOT_DIR / "source/emosa").rglob("*.py")),
            )
        },
    )
    db = SimDatabase(directory / "database")
    admin = OvsSession(db.endpoint, monitor_columns=MONITOR)
    manager = worker = broker = None
    captures, logs = [], []
    namespace, link = "em-native-" + uuid.uuid4().hex[:8], "en" + uuid.uuid4().hex[:8]
    ns_created = link_created = False

    def log(name):
        stream = (directory / (name + ".log")).open("ab")
        logs.append(stream)
        return stream

    def capture(interface, name, *filters):
        console = log(name + "-capture")
        child = subprocess.Popen(
            [
                "tcpdump",
                "--immediate-mode",
                "-i",
                interface,
                "-U",
                "-s",
                "0",
                "-w",
                str(directory / (name + ".pcap")),
                *filters,
            ],
            stdout=console,
            stderr=console,
        )
        captures.append(child)

    async def wait_json(name, predicate, timeout=40):
        end = time.monotonic() + timeout
        path = directory / name
        while time.monotonic() < end:
            if worker is not None and worker.returncode is not None:
                raise RuntimeError("worker exited; inspect native-worker.log and session")
            if path.is_file() and predicate(value := json.loads(path.read_text())):
                return value
            await asyncio.sleep(0.1)
        raise TimeoutError("waiting for " + name)

    async def state():
        raw = await admin.snapshot()
        return raw["schema"].row(
            "Wifi_VIF_State", next(iter(raw["tables"]["Wifi_VIF_State"].values()))
        )

    try:
        write(directory / "topology.json", radio["setup"](directory))
        if active_seconds:
            mqtt_root = RADIO_ROOT_DIR / "mqtt-inputs/stage"
            executable = mqtt_root / "usr/sbin/mosquitto"
            configuration = directory / "mosquitto.conf"
            configuration.write_text(
                f"listener 0 {directory}/mqtt.sock\n"
                "allow_anonymous true\nuser root\npersistence false\n"
                "message_size_limit 65536\nmax_connections 4\n"
            )
            write(
                directory / "mqtt-provenance.json",
                {
                    "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                    "packages": {
                        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted((RADIO_ROOT_DIR / "mqtt-inputs").glob("*.deb"))
                    },
                    "transport": "owned private Unix socket",
                    "physical_qualified": False,
                },
            )
            broker = await asyncio.create_subprocess_exec(
                str(executable),
                "-c",
                str(configuration),
                env={**os.environ, "LD_LIBRARY_PATH": str(mqtt_root / "usr/lib/x86_64-linux-gnu")},
                stdout=log("mqtt"),
                stderr=asyncio.subprocess.STDOUT,
            )
            end = time.monotonic() + 5
            while not (directory / "mqtt.sock").exists():
                if broker.returncode is not None or time.monotonic() > end:
                    raise RuntimeError("owned MQTT broker did not start")
                await asyncio.sleep(0.05)
        await db.start()
        await seed_radio_database(admin)
        results = await admin.transact(
            [
                {
                    "op": "insert",
                    "table": "AWLAN_Node",
                    "row": {
                        "serial_number": SERIAL,
                        "model": "EMOSA hwsim native onboarding fixture",
                        "firmware_version": "simulation-only",
                    },
                }
            ]
        )
        assert len(results) == 1 and "uuid" in results[0]
        write(directory / "policy.json", {"withhold": False})
        manager = await asyncio.create_subprocess_exec(
            sys.executable,
            str(RADIO_ROOT_DIR / "manager.py"),
            str(directory),
            stdout=log("manager"),
            stderr=asyncio.subprocess.STDOUT,
        )
        await radio["eventually"](state, lambda v: v.get("enabled") is True)
        # Initial provisioning remains separate from the later client cycles.
        write(directory / "policy.json", {"withhold": True})
        await wait_json("policy.json", lambda v: v["withhold"])
        capture("hwsim0", "radio")
        report["previous_controller_collection"] = stop_collect(
            directory / "previous-controller", names=(CONTROLLER,)
        )
        node(CONTROLLER, "prepare", "--backhaul", "wired")
        node(CONTROLLER, "hostap")
        node(CONTROLLER, "services")
        node(CONTROLLER, "agent")
        end = time.monotonic() + 55
        while True:
            try:
                before = helpers["inventory"]()
                if not discovery["transport_ready"]()[
                    "ready"
                ] or "02:00:00:ec:01:00" not in json.dumps(before):
                    raise RuntimeError("waiting for colocated gateway agent/radio")
                break
            except (RuntimeError, subprocess.CalledProcessError):
                if time.monotonic() > end:
                    raise
                await asyncio.sleep(0.25)
        write(directory / "controller-before.json", before)
        assert not inventory_bss(before)
        report["controller_policy"] = helpers["bml_policy"]()
        run("ip", "netns", "add", namespace)
        ns_created = True
        run("ip", "netns", "exec", namespace, "ip", "link", "set", "lo", "up")
        run("ip", "link", "add", link, "type", "veth", "peer", "name", link + "p")
        link_created = True
        run("ip", "link", "set", link + "p", "netns", namespace)
        for args in (
            ("set", link + "p", "name", "probe0"),
            ("set", "probe0", "address", "02:00:00:00:30:01"),
            ("set", "probe0", "up"),
        ):
            run("ip", "netns", "exec", namespace, "ip", "link", *args)
        run("ip", "link", "set", link, "master", "em-base-bh")
        run("ip", "link", "set", link, "up")
        capture(
            link,
            "ethernet",
            "ether",
            "proto",
            "0x893a",
            "and",
            "ether",
            "host",
            "02:00:00:00:30:01",
        )
        await asyncio.sleep(0.2)
        await db.manager_remote("unix:" + str(db.directory / "native-pod.sock"))
        worker = await asyncio.create_subprocess_exec(
            "ip",
            "netns",
            "exec",
            namespace,
            sys.executable,
            "-m",
            "emosa.simulation.native_onboarding",
            str(directory),
            *(["--telemetry", "--duration", str(active_seconds + 180)] if active_seconds else []),
            stdout=log("native-worker"),
            stderr=asyncio.subprocess.STDOUT,
        )
        withheld = await wait_json("native-operation.json", lambda v: v["writes"] == 1)
        assert withheld["config_ssid"] == "emosa-controller-trial"
        assert withheld["observed_ssid"] == "emosa-radio-initial"
        assert withheld["operation"]["state"] == "CONFIG_COMMITTED"
        write(directory / "withheld-operation.json", withheld)
        report["cases"]["withheld"] = True
        write(directory / "policy.json", {"withhold": False})
        applied = await wait_json(
            "native-operation.json", lambda v: v["operation"]["state"] == "OBSERVED_APPLIED"
        )
        assert applied["operation_count"] == applied["writes"] == 1
        await wait_json(
            "native-session.json", lambda v: v["counts"].get("ap_capability_report_sent", 0) >= 1
        )
        end = time.monotonic() + 35
        while True:
            after = helpers["inventory"]()
            bsses = inventory_bss(after)
            if any(
                v.get("BSSID") == RADIO_BSSID and v.get("SSID") == "emosa-controller-trial"
                for v in bsses.values()
            ):
                break
            if time.monotonic() > end:
                write(directory / "controller-after.json", after)
                raise TimeoutError("native controller radio/BSS inventory incomplete")
            await asyncio.sleep(0.3)
        write(directory / "controller-after.json", after)
        report["controller_radio_bss"] = bsses
        if not active_seconds:
            # Reproduce the earlier bounded onboarding experiment unchanged.
            (directory / "stop-worker").touch()
            await asyncio.wait_for(worker.wait(), 10)
            assert worker.returncode == 0
        radio["connect"]("emosa-controller-trial", helpers["KEY"])
        report["cases"]["clients"] = await radio["clients"](
            directory, "onboarded", "emosa-controller-trial"
        )
        if active_seconds:
            started = time.monotonic()
            samples = []
            next_disconnect = 25
            while time.monotonic() - started < active_seconds:
                if worker.returncode is not None:
                    raise RuntimeError("adapter exited during client activity")
                index = len(samples)
                current = json.loads((directory / "native-session.json").read_text())
                native = helpers["inventory"](depth=8)
                write(directory / f"active-inventory-{index:04d}.json", native)
                probes = await radio["clients"](
                    directory, f"active-{index:04d}", "emosa-controller-trial"
                )
                sample = {
                    "elapsed": time.monotonic() - started,
                    "adapter_pid": worker.pid,
                    "session": current,
                    "clients": probes,
                    "controller_station_present": bool(inventory_stations(native)),
                    "process_status": Path(f"/proc/{worker.pid}/status").read_text(),
                    "open_descriptors": len(list(Path(f"/proc/{worker.pid}/fd").iterdir())),
                }
                samples.append(sample)
                write(directory / "active-samples.json", samples)
                if time.monotonic() - started >= next_disconnect:
                    next_disconnect += 25
                    radio["inside"](
                        radio["CLIENTS"][1],
                        "systemctl",
                        "stop",
                        "emosa-radio-manager-client.service",
                    )
                    await asyncio.sleep(4)
                    detached = helpers["inventory"](depth=8)
                    write(directory / f"detached-inventory-{index:04d}.json", detached)
                    write(
                        directory / f"detached-session-{index:04d}.json",
                        json.loads((directory / "native-session.json").read_text()),
                    )
                    radio["connect"]("emosa-controller-trial", helpers["KEY"])
                await asyncio.sleep(3)
            report["active_observed_seconds"] = time.monotonic() - started
            report["active_samples"] = len(samples)
            report["active_controller_station_seen"] = any(
                s["controller_station_present"] for s in samples
            )
            # This is a pilot, not the full soak/recovery acceptance verdict.
            (directory / "stop-worker").touch()
            await asyncio.wait_for(worker.wait(), 10)
            assert worker.returncode == 0
        report.update(
            status="observed_pending_capture_review",
            operation=applied,
            controller_onboarding_proven=False,
        )
    except BaseException as exc:
        report.update(status="failed", error=type(exc).__name__, detail=str(exc))
        raise
    finally:
        errors = []
        for child in (worker, manager, broker):
            if child and child.returncode is None:
                try:
                    child.terminate()
                    await asyncio.wait_for(child.wait(), 35)
                except Exception as exc:
                    child.kill()
                    await child.wait()
                    errors.append(type(exc).__name__)
        for child in captures:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    await asyncio.to_thread(child.wait, 10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait)
                    errors.append("capture_timeout")
            if child.returncode != 0:
                errors.append("capture_failed")
        await admin.close()
        await db.stop()
        for container in radio["NODES"]:
            for unit in ("ap", "client", "data"):
                radio["inside"](
                    container,
                    "systemctl",
                    "stop",
                    f"emosa-radio-manager-{unit}.service",
                    check=False,
                )
        try:
            report["native_shutdown"] = stop_collect(
                directory / "native-shutdown", names=(CONTROLLER,)
            )
        except Exception as exc:
            errors.append("native_shutdown:" + type(exc).__name__)
        if ns_created:
            run("ip", "netns", "del", namespace)
        if link_created:
            subprocess.run(["ip", "link", "del", link], capture_output=True, check=False)
        for stream in logs:
            stream.close()
        report["cleanup_errors"] = errors
        write(directory / "result.json", report)
    assert not errors
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--active-seconds",
        type=int,
        default=0,
        help="Keep adapter active for a measured client-traffic pilot",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", args.label):
        parser.error("use a new label of 1–24 lowercase letters, digits or hyphens")
    if args.build.resolve().parent != ROOT or not args.build.name.startswith("candidate-"):
        parser.error("stage a separate candidate directory directly under /opt/emosa-baseline")
    if args.active_seconds and not 30 <= args.active_seconds <= 3600:
        parser.error("active-seconds must be zero or 30–3600")
    os.umask(0o077)
    wrapper = runpy.run_path(str(ROOT / "compatibility/controller-candidate.py"))
    with (RADIO_ROOT_DIR / "run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        try:
            wrapper["trial"](
                args.build,
                args.label,
                experiment=lambda label: asyncio.run(
                    experiment(label, active_seconds=args.active_seconds)
                ),
            )
        finally:
            signal.signal(signal.SIGTERM, previous)
