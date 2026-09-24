"""Measure the pinned native controller's answer to EMOSA's bounded Search.

Owned VM only. Creates a temporary probe namespace on the owned baseline bridge,
starts only the native controller and its colocated helper, and retains an
independent capture plus before/after controller inventory. No M1, pod, database
or operation engine participates. Existing native shutdown failures stay visible.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import runpy
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from run import CONTROLLER, node, stop_collect
from setup import OWNER, ROOT, inside, lxc, run

from emosa.errors import EmosaError
from emosa.wire.autoconfiguration import PeerBinding
from emosa.wire.ethernet import EthernetEndpoint
from emosa_lab.wire.controller_probe import ControllerProbe

AGENT = bytes.fromhex("020000003001")
PEER = bytes.fromhex("020000e00001")


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def transport_ready():
    """An inventory root exists before the native transport binds any interface."""
    script = """
import json, os, subprocess
from pathlib import Path
pid = int(subprocess.check_output(['systemctl', 'show', 'emosa-baseline-transport',
                                  '-p', 'MainPID', '--value']))
inodes = {os.readlink(p).removeprefix('socket:[').removesuffix(']')
          for p in Path(f'/proc/{pid}/fd').iterdir()
          if os.readlink(p).startswith('socket:[')} if pid > 1 else set()
interfaces = {int(Path(f'/sys/class/net/{name}/ifindex').read_text()): name
              for name in ('eth1', 'br-lan')}
bound = [interfaces[int(row[4])] for line in Path('/proc/net/packet').read_text().splitlines()[1:]
         if (row := line.split()) and row[-1] in inodes and int(row[4]) in interfaces
         and row[3].lower() in ('0003', '893a')]
print(json.dumps({'pid': pid, 'bound_interfaces': sorted(set(bound)), 'ready': bool(bound)}))
"""
    return json.loads(inside(CONTROLLER, "python3", "-c", script))


def inventory_projection(objects):
    rows = {key: value for obj in objects for key, value in obj.items()}
    devices = {
        value["ID"]: key
        for key, value in rows.items()
        if re.fullmatch(r"Device\.WiFi\.DataElements\.Network\.Device\.\d+\.", key)
        and isinstance(value, dict)
        and "ID" in value
    }
    prefix = devices.get("02:00:00:00:30:01")
    entry = None
    if prefix:
        radios = {
            key: value
            for key, value in rows.items()
            if re.fullmatch(re.escape(prefix) + r"Radio\.\d+\.", key)
        }
        bsses = {
            key: value
            for key, value in rows.items()
            if re.fullmatch(re.escape(prefix) + r"Radio\.\d+\.BSS\.\d+\.", key)
        }
        entry = {
            "path": prefix,
            "profile": rows[prefix]["MultiAPProfile"],
            "reported_radio_count": rows[prefix]["RadioNumberOfEntries"],
            "radio_ids": [v["ID"] for v in radios.values()],
            "bss_count": len(bsses),
            "agent_mode": rows.get(prefix + "MultiAPDevice.", {}).get("EasyMeshAgentOperationMode"),
            "backhaul_link_type": rows.get(prefix + "MultiAPDevice.Backhaul.", {}).get("LinkType"),
        }
    return {"device_ids": sorted(devices), "probe_entry": entry}


def worker(directory):
    # The parent creates this interface in a fresh namespace with no IP setup.
    if socket.gethostname() != "emosa-lab" or os.geteuid() != 0:
        raise RuntimeError("Probe worker requires the owned VM")
    binding = PeerBinding("probe0", 1, AGENT, PEER, (PEER,))
    with EthernetEndpoint("probe0", AGENT, timeout=0.05) as endpoint:
        probe = ControllerProbe(binding, endpoint.send)
        received = 0
        try:
            while probe.state == "searching" and received < 128:
                probe.tick()
                try:
                    frame = endpoint.receive()
                except EmosaError:
                    received += 1
                    continue
                if frame is not None:
                    received += 1
                    probe.receive(frame, ingress="probe0", generation=1)
        finally:
            probe.close()
            write(directory / "probe.json", probe.status() | {"frames_received": received})
    if probe.state != "response_observed":
        raise RuntimeError("No live correlated native Response; inspect probe.json")


def trial(label):
    helpers = runpy.run_path(str(ROOT / "controller-trial.py"))
    helpers["idle"]()
    if lxc("network", "get", "em-base-bh", "user.emosa.baseline").strip() != OWNER:
        raise RuntimeError("Probe bridge ownership mismatch")
    directory = ROOT / "discovery-trials" / label
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    namespace = "em-disc-" + uuid.uuid4().hex[:8]
    link = "ed" + uuid.uuid4().hex[:8]
    peer_link = link + "p"
    capture = None
    ns_created = link_created = peer_touched = False
    report = {
        "label": label,
        "status": "preparing",
        "exchange_observed": False,
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "operations_created": 0,
        "wsc_started": False,
        "namespace": namespace,
        "temporary_interface": link,
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (
                Path(__file__),
                ROOT / "controller-trial.py",
                ROOT / "node.py",
                ROOT / "reference.json",
                ROOT / "prplmesh.reference.json",
                ROOT / "run.py",
                ROOT / "setup.py",
                Path(sys.modules[ControllerProbe.__module__].__file__),
            )
        },
    }
    write(directory / "result.json", report)
    with (directory / "capture.log").open("wb") as console:
        try:
            report["previous_controller_collection"] = stop_collect(
                directory / "previous-controller", names=(CONTROLLER,)
            )
            peer_touched = True
            for filename in ("node.py", "reference.json"):
                lxc("file", "push", "--quiet", str(ROOT / filename), CONTROLLER + str(ROOT) + "/")
            node(CONTROLLER, "prepare", "--backhaul", "wired")
            node(CONTROLLER, "hostap")
            node(CONTROLLER, "services")
            node(CONTROLLER, "agent")
            end = time.monotonic() + 45
            while True:
                try:
                    before = helpers["inventory"]()
                    if "02:00:00:e0:00:01" not in json.dumps(before):
                        raise RuntimeError("Waiting for local controller inventory")
                    readiness = transport_ready()
                    if not readiness["ready"]:
                        raise RuntimeError("Waiting for native transport packet binding")
                    report["transport_readiness"] = readiness
                    break
                except (subprocess.CalledProcessError, RuntimeError):
                    if time.monotonic() >= end:
                        raise
                    time.sleep(0.2)
            write(directory / "controller-before.json", before)
            report["inventory_before"] = inventory_projection(before)
            if report["inventory_before"]["probe_entry"] is not None:
                raise RuntimeError("Probe identity already present before Search")
            run("ip", "netns", "add", namespace)
            ns_created = True
            run("ip", "link", "add", link, "type", "veth", "peer", "name", peer_link)
            link_created = True
            run("ip", "link", "set", peer_link, "netns", namespace)
            run("ip", "netns", "exec", namespace, "ip", "link", "set", peer_link, "name", "probe0")
            run(
                "ip",
                "netns",
                "exec",
                namespace,
                "ip",
                "link",
                "set",
                "probe0",
                "address",
                "02:00:00:00:30:01",
            )
            run("ip", "link", "set", link, "master", "em-base-bh")
            run("ip", "link", "set", link, "up")
            run("ip", "netns", "exec", namespace, "ip", "link", "set", "probe0", "up")
            capture = subprocess.Popen(
                [
                    "tcpdump",
                    "--immediate-mode",
                    "-i",
                    link,
                    "-U",
                    "-s",
                    "0",
                    "-w",
                    str(directory / "ethernet.pcap"),
                    "ether",
                    "proto",
                    "0x893a",
                    "and",
                    "ether",
                    "host",
                    "02:00:00:00:30:01",
                ],
                stdout=console,
                stderr=console,
            )
            end = time.monotonic() + 5
            while "listening on" not in (directory / "capture.log").read_text():
                if capture.poll() is not None or time.monotonic() >= end:
                    raise RuntimeError("Independent capture did not become ready")
                time.sleep(0.02)
            process = subprocess.run(
                [
                    "ip",
                    "netns",
                    "exec",
                    namespace,
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(directory),
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
            (directory / "worker.log").write_text(process.stdout + process.stderr)
            process.check_returncode()
            # A fast exchange can finish before libpcap's buffered delivery.
            # Require the independent file to contain the response before stop.
            end = time.monotonic() + 3
            while True:
                observed = subprocess.run(
                    [
                        "tshark",
                        "-n",
                        "-r",
                        str(directory / "ethernet.pcap"),
                        "-Y",
                        "ieee1905.message_type == 8 && eth.dst == 02:00:00:00:30:01",
                        "-T",
                        "fields",
                        "-e",
                        "frame.number",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if observed.returncode == 0 and observed.stdout.strip():
                    report["capture_response_observed"] = True
                    break
                if capture.poll() is not None or time.monotonic() >= end:
                    raise RuntimeError("Independent capture did not retain the native Response")
                time.sleep(0.02)
            report["probe"] = json.loads((directory / "probe.json").read_text())
            after = helpers["inventory"]()
            write(directory / "controller-after.json", after)
            report["inventory_after"] = inventory_projection(after)
            entry = report["inventory_after"]["probe_entry"]
            report["discovery_entry_observed"] = entry is not None
            if (
                entry is None
                or entry["reported_radio_count"]
                or entry["radio_ids"]
                or entry["bss_count"]
            ):
                raise RuntimeError(
                    "Expected only a discovered device, without radio/BSS onboarding"
                )
            report.update(status="response_observed_admission_pending", exchange_observed=True)
        except BaseException as exc:
            report.update(status="failed", error=type(exc).__name__)
            raise
        finally:
            errors = []
            if capture is not None:
                try:
                    if capture.poll() is None:
                        capture.send_signal(signal.SIGINT)
                    capture.wait(timeout=5)
                    if capture.returncode != 0:
                        raise RuntimeError("Independent capture failed")
                except Exception as exc:
                    if capture.poll() is None:
                        capture.kill()
                        capture.wait(timeout=5)
                    errors.append("capture:" + type(exc).__name__)
            for created, command in (
                (link_created, ("ip", "link", "delete", link)),
                (ns_created, ("ip", "netns", "delete", namespace)),
            ):
                if created:
                    try:
                        run(*command)
                    except Exception as exc:
                        errors.append("link_cleanup:" + type(exc).__name__)
            if peer_touched:
                try:
                    report["shutdown"] = stop_collect(directory / "shutdown", names=(CONTROLLER,))
                    report["native_shutdown_abnormal"] = any(
                        unit["after"].get("Result") != "success"
                        for units in report["shutdown"].values()
                        for unit in units
                    )
                except Exception as exc:
                    errors.append("controller_cleanup:" + type(exc).__name__)
            try:
                helpers["idle"]()
            except Exception as exc:
                errors.append("idle:" + type(exc).__name__)
            report["cleanup_errors"] = errors
            if errors:
                report.update(status="failed", exchange_observed=False)
            write(directory / "result.json", report)
    if not report["exchange_observed"]:
        raise RuntimeError("Native probe failed; inspect retained result")
    print(
        json.dumps(
            {"directory": str(directory), "status": report["status"], "probe": report["probe"]}
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--label")
    group.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if args.worker:
        worker(args.worker)
        return
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", args.label):
        raise SystemExit("Choose a new 1–24 character lowercase run label")
    # Same lock as the existing native-controller and radio-manager exercises.
    with Path("/opt/emosa-radio-manager/run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        try:
            trial(args.label)
        finally:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
