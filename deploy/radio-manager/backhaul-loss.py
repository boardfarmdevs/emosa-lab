"""Owned idle-lab counter qualification: selected wired ICMP egress/ingress loss.

Adds one temporary clsact/filter only to the owned simulated pod, preserves each
attempt and removes only that owned qdisc. No physical target is supported.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

from common import AP, CLIENTS, NODES, ROOT, guard, inside, run, write

PREF = "49190"
COUNT = 17


def snapshot(direction="egress"):
    return {
        "monotonic_ns": time.monotonic_ns(),
        "wall_ns": time.time_ns(),
        "links": json.loads(inside(AP, "ip", "-j", "-d", "-s", "link", "show")),
        "qdiscs": json.loads(inside(AP, "tc", "-j", "-s", "qdisc", "show", "dev", "eth1")),
        "filters": json.loads(
            inside(AP, "tc", "-j", "-s", "filter", "show", "dev", "eth1", direction)
        ),
        "opposite_filters": json.loads(
            inside(
                AP,
                "tc",
                "-j",
                "-s",
                "filter",
                "show",
                "dev",
                "eth1",
                "ingress" if direction == "egress" else "egress",
            )
        ),
    }


def experiment(label, observe_egress=False, direction="egress"):
    guard()
    if direction not in ("egress", "ingress"):
        raise ValueError("unsupported owned loss direction")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", label):
        raise ValueError("use a new owned run label")
    os.umask(0o077)
    directory = ROOT / "counter-probes" / label
    directory.mkdir(parents=True, exist_ok=False)
    result = {
        "scope": "owned idle simulated-pod wired backhaul loss-accounting probe",
        "label": label,
        "status": "started",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "kernel": run("uname", "-r").strip(),
        "phases": [],
        "cleanup_errors": [],
        "physical_pod_changed": False,
        "measurement_source_qualified": False,
        "sustained_operation_proven": False,
        "egress_observation_requested": observe_egress,
        "loss_direction": direction,
    }
    write(directory / "result.json", result)
    captures, handles, owned_qdisc = [], [], False
    egress_unit = None
    egress_path = ROOT / "egress-observations" / (label + ".json")

    def wait_egress(after):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                value = json.loads(inside(AP, "cat", str(egress_path)))
                obs = value["observation"]
                if value["running"] and not value["errors"] and obs and obs["started_ns"] > after:
                    return value
            except (ValueError, subprocess.SubprocessError):
                pass
            time.sleep(0.1)
        raise RuntimeError("egress observer did not provide a fresh sample")

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
            before = snapshot(direction)
            result["before"] = before
            if (
                len(before["qdiscs"]) != 1
                or before["qdiscs"][0]["kind"] != "noqueue"
                or before["filters"]
                or before["opposite_filters"]
            ):
                raise RuntimeError("preserve existing traffic-control configuration")
            links = {v["ifname"]: v for v in before["links"]}
            backhaul = links["eth1"]
            if backhaul["linkinfo"]["info_kind"] != "veth" or backhaul["master"] != "br-lan":
                raise RuntimeError("expected owned pod backhaul veth")
            (peer,) = [
                v
                for v in json.loads(run("ip", "-j", "-d", "link", "show"))
                if v["ifindex"] == backhaul["link_index"]
            ]
            if peer.get("master") != "em-base-bh" or peer.get("link_index") != backhaul["ifindex"]:
                raise RuntimeError("unexpected pod backhaul bridge path")
            result["backhaul_peer"] = peer
            result["reported_veth_speed_mbps"] = int(inside(AP, "cat", "/sys/class/net/eth1/speed"))
            result["reported_speed_is_measured_capacity"] = False
            if observe_egress:
                collector = ROOT / "egress-observer.py"
                run(
                    "lxc",
                    "--force-local",
                    "--project",
                    "default",
                    "file",
                    "push",
                    "--quiet",
                    str(collector),
                    AP + str(collector),
                )
                result["egress_collector_sha256"] = hashlib.sha256(
                    collector.read_bytes()
                ).hexdigest()
                egress_unit = "emosa-native-egress-" + label + ".service"
                inside(
                    AP,
                    "systemd-run",
                    "--quiet",
                    "--property=Type=exec",
                    "--property=RemainAfterExit=yes",
                    "--property=TimeoutStopSec=5",
                    "--unit",
                    egress_unit,
                    "python3",
                    str(collector),
                    label,
                    "--seconds",
                    "90",
                )
                wait_egress(time.monotonic_ns())
            # Warm ARP and independently establish the existing path. No address changes.
            inside(CLIENTS[0], "ping", "-n", "-I", "eth1", "-c", "2", "-W", "1", "192.0.2.1")
            capture_commands = [
                (
                    "ingress",
                    [
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
                        "8192",
                        "-s",
                        "0",
                        "-i",
                        "eth2",
                        "-w",
                        "-",
                        "icmp and host 192.0.2.20 and host 192.0.2.1",
                    ],
                ),
                (
                    "backhaul",
                    [
                        "tcpdump",
                        "--immediate-mode",
                        "-n",
                        "-U",
                        "-B",
                        "8192",
                        "-s",
                        "0",
                        "-i",
                        peer["ifname"],
                        "-w",
                        "-",
                        "icmp and host 192.0.2.20 and host 192.0.2.1",
                    ],
                ),
            ]
            if direction == "ingress":
                # VM peer transmit is pod receive, before the pod's ingress TC.
                # SLL2 preserves ifindex/direction. Capture both directions;
                # libpcap direction-only selection obscures filter totals.
                capture_commands.append(
                    (
                        "receive",
                        [
                            "tcpdump",
                            "--immediate-mode",
                            "-n",
                            "-U",
                            "-B",
                            "8192",
                            "-s",
                            "0",
                            "-i",
                            "any",
                            "-y",
                            "LINUX_SLL2",
                            "-w",
                            "-",
                            "ifindex " + str(peer["ifindex"]),
                        ],
                    )
                )
            result["capture_commands"] = dict(capture_commands)
            for name, command in capture_commands:
                out = (directory / (name + ".pcap")).open("xb")
                err = (directory / (name + "-capture.log")).open("x")
                handles.extend((out, err))
                captures.append(subprocess.Popen(command, stdout=out, stderr=err))
            time.sleep(0.5)
            if any(p.poll() is not None for p in captures):
                raise RuntimeError("capture terminated before probe")
            for phase, identifier in (("normal", 31001), ("drop", 31002), ("restored", 31003)):
                if phase == "drop":
                    inside(AP, "tc", "qdisc", "add", "dev", "eth1", "clsact")
                    owned_qdisc = True
                    inside(
                        AP,
                        "tc",
                        "filter",
                        "add",
                        "dev",
                        "eth1",
                        direction,
                        "protocol",
                        "ip",
                        "pref",
                        PREF,
                        "handle",
                        "1",
                        "flower",
                        "skip_hw",
                        "ip_proto",
                        "icmp",
                        "src_ip",
                        "192.0.2.20" if direction == "egress" else "192.0.2.1",
                        "dst_ip",
                        "192.0.2.1" if direction == "egress" else "192.0.2.20",
                        "action",
                        "drop",
                    )
                elif phase == "restored":
                    inside(AP, "tc", "qdisc", "del", "dev", "eth1", "clsact")
                    owned_qdisc = False
                value = {"phase": phase, "icmp_id": identifier, "before": snapshot(direction)}
                if observe_egress:
                    value["egress_before"] = wait_egress(time.monotonic_ns())
                result["phases"].append(value)
                write(directory / "result.json", result)
                completed = subprocess.run(
                    [
                        "lxc",
                        "--force-local",
                        "--project",
                        "default",
                        "exec",
                        CLIENTS[0],
                        "--",
                        "ping",
                        "-n",
                        "-I",
                        "eth1",
                        "-e",
                        str(identifier),
                        "-c",
                        str(COUNT),
                        "-i",
                        "0.05",
                        "-W",
                        "1",
                        "192.0.2.1",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                value.update(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    after=snapshot(direction),
                )
                if observe_egress:
                    value["egress_after"] = wait_egress(time.monotonic_ns())
                write(directory / "result.json", result)
                if completed.returncode != (1 if phase == "drop" else 0):
                    raise RuntimeError("unexpected probe result; preserve capture")
            result["status"] = "observed_pending_independent_review"
        except Exception as error:
            result.update(status="failed", error=type(error).__name__ + ":" + str(error))
            raise
        finally:
            if owned_qdisc:
                try:
                    inside(AP, "tc", "qdisc", "del", "dev", "eth1", "clsact")
                except Exception as error:
                    result["cleanup_errors"].append("owned_qdisc:" + str(error))
            if egress_unit:
                try:
                    inside(AP, "systemctl", "stop", egress_unit)
                    final = json.loads(inside(AP, "cat", str(egress_path)))
                    write(directory / "egress-observer-final.json", final)
                    (directory / "egress-observations.jsonl").write_text(
                        inside(AP, "cat", str(egress_path.with_suffix(".jsonl")))
                    )
                    if final["running"] or final["errors"]:
                        raise RuntimeError("egress observer did not complete cleanly")
                except Exception as error:
                    result["cleanup_errors"].append("egress_observer:" + str(error))
            # Let the final received frame reach the capture reader before
            # requesting shutdown; -U alone only flushes userspace file buffers.
            time.sleep(2)
            for process in captures:
                try:
                    process.send_signal(signal.SIGINT)
                    code = process.wait(timeout=10)
                    if code != 0:
                        result["cleanup_errors"].append("capture_exit:" + str(code))
                except Exception as error:
                    result["cleanup_errors"].append("capture:" + str(error))
            for handle in handles:
                handle.close()
            try:
                result["after"] = snapshot(direction)
                result["traffic_control_restored"] = (
                    result["after"]["qdiscs"] == result["before"]["qdiscs"]
                    and result["after"]["filters"] == result["before"]["filters"]
                    and result["after"]["opposite_filters"] == result["before"]["opposite_filters"]
                )
                if not result["traffic_control_restored"]:
                    result["cleanup_errors"].append("traffic_control_not_restored")
            except Exception as error:
                result["cleanup_errors"].append("restoration_check:" + str(error))
            write(directory / "result.json", result)
    if result["cleanup_errors"]:
        raise RuntimeError("cleanup requires inspection; preserve this run")
    print(json.dumps({"directory": str(directory), "status": result["status"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    parser.add_argument("--observe-egress", action="store_true")
    parser.add_argument("--direction", choices=("egress", "ingress"), default="egress")
    args = parser.parse_args()
    experiment(args.label, args.observe_egress, args.direction)
