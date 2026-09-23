"""Calibrate the owned simulated Ethernet service against independent packets.

Installs only a temporary owned qdisc, preserves failures and restores the idle
lab. No physical pod, generic endpoint or external network is supported.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import runpy
import signal
import subprocess
import time
import uuid
from contextlib import ExitStack

from common import AP, CLIENTS, NODES, ROOT, SERVER, guard, inside, lxc, run, write


def experiment(label, queue_loss=False):
    guard()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", label):
        raise ValueError("use a new owned run label")
    os.umask(0o077)
    directory = ROOT / "link-calibration" / label
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    nonce = uuid.uuid4().hex[:16]
    module = runpy.run_path(str(ROOT / "virtual-link.py"))
    shaping = module["VirtualLink"]()
    result = {
        "label": label,
        "status": "started",
        "nonce": nonce,
        "scope": "owned 100-Mbit TBF queue-loss probe with eight sender sockets"
        if queue_loss
        else "owned 100-Mbit simulated Ethernet service calibration",
        "queue_loss_requested": queue_loss,
        "phases": [],
        "cleanup_errors": [],
        "physical_pod_changed": False,
        "measurement_source_qualified": False,
        "sustained_operation_proven": False,
    }
    result["source_hashes"] = {
        n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest()
        for n in ("calibrate-link.py", "virtual-link.py", "link-traffic.py", "egress-observer.py")
    }
    write(directory / "result.json", result)
    captures, handles = [], []
    sink_unit = observer_unit = None
    with ExitStack() as stack:
        for name in ("run.lock", "manager.lock"):
            lock = stack.enter_context((ROOT / name).open("a+"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            for node in NODES:
                if inside(
                    node,
                    "systemctl",
                    "list-units",
                    "--state=active,activating,deactivating",
                    "--no-legend",
                    "--plain",
                    "emosa-baseline-*",
                    "emosa-radio-manager-*",
                    "emosa-native-*",
                ).strip():
                    raise RuntimeError("owned lab must be idle")
            result["configured_qdisc"] = shaping.start()
            pod = json.loads(inside(AP, "ip", "-j", "-d", "link", "show", "eth1"))[0]
            (peer,) = [
                r
                for r in json.loads(run("ip", "-j", "-d", "link", "show"))
                if r["ifindex"] == pod["link_index"]
            ]
            if peer["master"] != "em-base-bh" or peer["link_index"] != pod["ifindex"]:
                raise RuntimeError("unexpected backhaul peer")
            result.update(pod_backhaul=pod, vm_peer=peer, kernel=run("uname", "-r").strip())
            for node in (CLIENTS[0], SERVER):
                lxc(
                    "file",
                    "push",
                    "--quiet",
                    str(ROOT / "link-traffic.py"),
                    node + str(ROOT / "link-traffic.py"),
                )
            sink_unit = "emosa-native-link-sink-" + label + ".service"
            inside(
                SERVER,
                "systemd-run",
                "--quiet",
                "--property=Type=exec",
                "--property=RemainAfterExit=yes",
                "--property=TimeoutStopSec=5",
                "--unit",
                sink_unit,
                "python3",
                str(ROOT / "link-traffic.py"),
                label,
                nonce,
                "--receive",
            )
            for _ in range(50):
                try:
                    if (
                        inside(
                            SERVER, "cat", str(ROOT / "link-calibration" / (label + ".ready"))
                        ).strip()
                        == nonce
                    ):
                        break
                except subprocess.SubprocessError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("traffic receiver not ready")
            inside(CLIENTS[0], "ping", "-n", "-I", "eth1", "-c", "2", "-W", "1", "192.0.2.1")
            commands = {
                "offered": [
                    "lxc",
                    "--force-local",
                    "--project",
                    "default",
                    "exec",
                    AP,
                    "--",
                    "tcpdump",
                    "--immediate-mode",
                    "-n",
                    "-U",
                    "-B",
                    "32768",
                    "-s",
                    "0",
                    "-i",
                    "eth2",
                    "-w",
                    "-",
                ],
                "backhaul": [
                    "tcpdump",
                    "--immediate-mode",
                    "-n",
                    "-U",
                    "-B",
                    "32768",
                    "-s",
                    "0",
                    "-i",
                    peer["ifname"],
                    "-w",
                    "-",
                ],
            }
            result["capture_commands"] = commands
            for name, command in commands.items():
                out = (directory / (name + ".pcap")).open("xb")
                err = (directory / (name + "-capture.log")).open("x")
                handles.extend((out, err))
                captures.append(subprocess.Popen(command, stdout=out, stderr=err))
            lxc(
                "file",
                "push",
                "--quiet",
                str(ROOT / "egress-observer.py"),
                AP + str(ROOT / "egress-observer.py"),
            )
            observer_unit = "emosa-native-link-observer-" + label + ".service"
            inside(
                AP,
                "systemd-run",
                "--quiet",
                "--property=Type=exec",
                "--property=RemainAfterExit=yes",
                "--property=TimeoutStopSec=5",
                "--unit",
                observer_unit,
                "python3",
                str(ROOT / "egress-observer.py"),
                label,
                "--seconds",
                "75",
            )
            time.sleep(1)
            if any(p.poll() is not None for p in captures):
                raise RuntimeError("capture stopped before calibration")
            for phase, size, rate in (
                (1, 1472, 50_000_000),
                (2, 1472, 160_000_000),
                (3, 16, 5_000_000),
            ):
                value = {
                    "phase": phase,
                    "before": module["qdiscs"](),
                    "started_ns": time.monotonic_ns(),
                    "wall_ns": time.time_ns(),
                }
                result["phases"].append(value)
                write(directory / "result.json", result)
                value["sender"] = json.loads(
                    inside(
                        CLIENTS[0],
                        "python3",
                        str(ROOT / "link-traffic.py"),
                        label,
                        nonce,
                        "--phase",
                        str(phase),
                        "--size",
                        str(size),
                        "--rate",
                        str(rate),
                        *(["--queue-loss"] if queue_loss else []),
                    )
                )
                time.sleep(1)
                value.update(after=module["qdiscs"](), finished_ns=time.monotonic_ns())
                write(directory / "result.json", result)
            result["status"] = "observed_pending_independent_review"
        except BaseException as error:
            result.update(status="failed", error=type(error).__name__ + ":" + str(error))
            raise
        finally:
            for node, unit in ((SERVER, sink_unit), (AP, observer_unit)):
                if not unit:
                    continue
                try:
                    inside(node, "systemctl", "stop", unit)
                except Exception as error:
                    result["cleanup_errors"].append("unit:" + str(error))
            if sink_unit:
                try:
                    write(
                        directory / "receiver.json",
                        json.loads(
                            inside(
                                SERVER, "cat", str(ROOT / "link-calibration" / (label + ".json"))
                            )
                        ),
                    )
                except Exception as error:
                    result["cleanup_errors"].append("receiver:" + str(error))
            if observer_unit:
                try:
                    base = ROOT / "egress-observations" / label
                    (directory / "egress-observations.jsonl").write_text(
                        inside(AP, "cat", str(base.with_suffix(".jsonl")))
                    )
                    write(
                        directory / "egress-observer-final.json",
                        json.loads(inside(AP, "cat", str(base.with_suffix(".json")))),
                    )
                except Exception as error:
                    result["cleanup_errors"].append("observer:" + str(error))
            time.sleep(2)
            for process in captures:
                try:
                    process.send_signal(signal.SIGINT)
                    if process.wait(timeout=10) != 0:
                        raise RuntimeError("capture exit")
                except Exception as error:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    result["cleanup_errors"].append("capture:" + str(error))
            for handle in handles:
                handle.close()
            try:
                shaping.close()
            except Exception as error:
                result["cleanup_errors"].append("qdisc:" + str(error))
            result.update(
                original_qdisc=shaping.before,
                restored_qdisc=shaping.after,
                traffic_control_restored=shaping.before is not None
                and shaping.before == shaping.after,
            )
            write(directory / "result.json", result)
    if result["cleanup_errors"]:
        raise RuntimeError("cleanup requires inspection")
    print(json.dumps({"directory": str(directory), "status": result["status"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    parser.add_argument("--queue-loss", action="store_true")

    def terminated(_signal, _frame):
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminated)
    args = parser.parse_args()
    experiment(args.label, args.queue_loss)
