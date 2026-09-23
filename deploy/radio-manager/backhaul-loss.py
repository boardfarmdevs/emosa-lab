"""Owned idle-lab counter qualification: selected wired ICMP loss before veth TX.

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


def snapshot():
    return {
        "monotonic_ns": time.monotonic_ns(),
        "wall_ns": time.time_ns(),
        "links": json.loads(inside(AP, "ip", "-j", "-d", "-s", "link", "show")),
        "qdiscs": json.loads(inside(AP, "tc", "-j", "-s", "qdisc", "show", "dev", "eth1")),
        "filters": json.loads(
            inside(AP, "tc", "-j", "-s", "filter", "show", "dev", "eth1", "egress")
        ),
    }


def experiment(label):
    guard()
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
    }
    write(directory / "result.json", result)
    captures, handles, owned_qdisc = [], [], False
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
            before = snapshot()
            result["before"] = before
            if (
                len(before["qdiscs"]) != 1
                or before["qdiscs"][0]["kind"] != "noqueue"
                or before["filters"]
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
            # Warm ARP and independently establish the existing path. No address changes.
            inside(CLIENTS[0], "ping", "-n", "-I", "eth1", "-c", "2", "-W", "1", "192.0.2.1")
            for name, command in (
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
            ):
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
                        "egress",
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
                        "192.0.2.20",
                        "dst_ip",
                        "192.0.2.1",
                        "action",
                        "drop",
                    )
                elif phase == "restored":
                    inside(AP, "tc", "qdisc", "del", "dev", "eth1", "clsact")
                    owned_qdisc = False
                value = {"phase": phase, "icmp_id": identifier, "before": snapshot()}
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
                    after=snapshot(),
                )
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
                result["after"] = snapshot()
                result["traffic_control_restored"] = (
                    result["after"]["qdiscs"] == result["before"]["qdiscs"]
                    and result["after"]["filters"] == result["before"]["filters"]
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
    experiment(parser.parse_args().label)
