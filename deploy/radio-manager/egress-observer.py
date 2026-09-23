"""Passive counters and configuration epochs for the owned pod's eth1 egress.

Route/TC multicast loss invalidates the collector. No interfaces, filters or
qdiscs are changed. This simulation helper is never installed on physical pods.
"""

import argparse
import hashlib
import json
import os
import re
import runpy
import select
import signal
import socket
import struct
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path("/opt/emosa-radio-manager")


def json_command(*args):
    data = subprocess.check_output(args, timeout=2)
    if len(data) > 65536:
        raise ValueError("observation_dump_budget")
    return json.loads(data)


def configuration_events(data):
    count, offset = 0, 0
    while offset < len(data):
        if offset + 16 > len(data):
            raise ValueError("truncated_netlink_header")
        size, kind, _, _, _ = struct.unpack_from("=IHHII", data, offset)
        # nlmsg_pid can carry the original TC requester's port ID. Kernel
        # provenance is checked using recvmsg's sockaddr_nl, not this header.
        if size < 16 or offset + size > len(data) or kind in (2, 4):
            raise ValueError("invalid_or_lost_netlink_notification")
        if kind not in (1, 3):  # NOOP, DONE; all link/TC changes conservatively start a new epoch.
            count += 1
        offset += (size + 3) & ~3
    return count


def observe(label, seconds):
    runpy.run_path(str(ROOT / "node.py"))["guard"]()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", label) or not 30 <= seconds <= 86400:
        raise ValueError("invalid owned observation scope")
    os.umask(0o077)
    directory = ROOT / "egress-observations"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (label + ".json")
    path.open("x").close()
    log = (directory / (label + ".jsonl")).open("x")
    identity = {
        "profile": "owned-veth-egress-observation-v1",
        "run_label": label,
        "collector_epoch": str(uuid.uuid4()),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "netns_inode": Path("/proc/self/ns/net").stat().st_ino,
        "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "interface": "eth1",
        "ifindex": socket.if_nametoindex("eth1"),
        "mac": Path("/sys/class/net/eth1/address").read_text().strip(),
    }
    epoch, errors, stopping = 0, [], False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    def publish(observation, running=True):
        value = {
            **identity,
            "configuration_epoch": epoch,
            "heartbeat_ns": time.monotonic_ns(),
            "running": running,
            "errors": errors,
            "observation": observation,
        }
        data = json.dumps(value)
        if len(data) > 262144:
            raise ValueError("publication_budget")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(data + "\n")
        temporary.replace(path)
        log.write(data + "\n")
        log.flush()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with log, socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as channel:
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
        channel.bind((0, 1 | 8))  # RTMGRP_LINK | RTMGRP_TC before the first dump.

        def drain():
            nonlocal epoch
            for _ in range(128):
                if not select.select([channel], [], [], 0)[0]:
                    return
                data, _, flags, peer = channel.recvmsg(65536)
                if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or peer[0] != 0:
                    raise ValueError("truncated_or_foreign_netlink_notification")
                epoch += configuration_events(data)
            raise ValueError("netlink_notification_budget")

        next_sample, deadline = 0.0, time.monotonic() + seconds
        current = None
        try:
            publish(None)
            while not stopping and time.monotonic() < deadline:
                old = epoch
                drain()
                if epoch != old:
                    current = None
                    publish(None)  # Invalidate even when a delete/recreate leaves the same shape.
                if time.monotonic() >= next_sample:
                    before_epoch = epoch
                    start = time.monotonic_ns()
                    link = json_command("ip", "-j", "-d", "-s", "link", "show", "eth1")
                    # Detailed output retains the STAB framing table and rate
                    # options needed to distinguish a shaped service contract.
                    qdiscs = json_command("tc", "-j", "-d", "-s", "qdisc", "show", "dev", "eth1")
                    filters = {
                        hook: json_command("tc", "-j", "-s", "filter", "show", "dev", "eth1", hook)
                        for hook in ("root", "ingress", "egress")
                    }
                    features = json_command("ethtool", "--json", "--show-features", "eth1")
                    after = json_command("ip", "-j", "-d", "link", "show", "eth1")
                    end = time.monotonic_ns()
                    drain()
                    current = {
                        "started_ns": start,
                        "ended_ns": end,
                        "link": link,
                        "link_after": after,
                        "qdiscs": qdiscs,
                        "filters": filters,
                        "features": features,
                    }
                    if epoch != before_epoch:
                        current = None
                    publish(current)
                    next_sample = time.monotonic() + 0.5
                select.select([channel], [], [], 0.05)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(type(error).__name__ + ":" + str(error))
        finally:
            publish(None, running=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    observe(args.label, args.seconds)
