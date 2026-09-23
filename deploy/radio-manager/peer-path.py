"""Owned VM bridge isolation and passive path evidence for native link metrics.

Only the lab orchestrator changes isolation/offloads. The separate manager
observes the resulting path; no physical-pod endpoint is accepted.
"""

import hashlib
import json
import runpy
import select
import socket
import time
import uuid
from pathlib import Path

from common import AP, ROOT, SERVER, guard, inside, run, write

PROFILE = "owned-isolated-peer-path-v1"


def dump(*args):
    value = run(*args, timeout=2)
    if len(value) > 262144:
        raise ValueError("path dump budget")
    return json.loads(value)


def identity(row):
    return {k: row[k] for k in ("ifindex", "ifname", "address", "link_index")}


def features(command, name):
    (row,) = json.loads(command("ethtool", "--json", "--show-features", name))
    return {
        k: v
        for k, v in row.items()
        if isinstance(v, dict)
        and any(token in k for token in ("segmentation", "receive-offload", "gso", "gro", "vlan"))
    }


class PeerPath:
    def __init__(self, directory):
        guard()
        self.directory = directory
        self.before = self.after = None
        self.changed = []
        self.offloads = []
        self.pod = json.loads(inside(AP, "ip", "-j", "-d", "link", "show", "eth1"))[0]
        self.controller = json.loads(inside(SERVER, "ip", "-j", "-d", "link", "show", "eth1"))[0]
        self.pod_peer = next(
            r
            for r in dump("ip", "-j", "-d", "link", "show")
            if r["ifindex"] == self.pod["link_index"]
        )
        self.descriptor = None

    def start(self, control):
        rows = dump("ip", "-j", "-d", "link", "show", "master", "em-base-bh")
        peers = {r["ifindex"]: r for r in rows}
        proxy = next(r for r in rows if r["ifname"] == control)
        if set(peers) != {self.pod["link_index"], self.controller["link_index"], proxy["ifindex"]}:
            raise RuntimeError("unexpected bridge peer inventory")
        if any(r["linkinfo"]["info_slave_data"]["isolated"] for r in rows):
            raise RuntimeError("preserve pre-existing isolated bridge")
        self.before = rows
        for row in (self.pod_peer, proxy):
            self.changed.append(identity(row))
            run("bridge", "link", "set", "dev", row["ifname"], "isolated", "on")
        for node, name in ((AP, "eth1"), (None, self.pod_peer["ifname"])):
            command = (lambda *args: inside(AP, *args)) if node else run
            old = features(command, name)
            self.offloads.append({"node": node, "name": name, "before": old})
            change = [
                v for k, f in old.items() if f["active"] and f["fixed"] is False for v in (k, "off")
            ]
            if change:
                command("ethtool", "-K", name, *change)
            new = features(command, name)
            if any(v["active"] for v in new.values()):
                raise RuntimeError("packet aggregation or VLAN offload remains enabled")
        self.descriptor = {
            "profile": PROFILE,
            "run_label": self.directory.name,
            "profile_epoch": str(uuid.uuid4()),
            "pod": identity(self.pod),
            "controller": identity(self.controller),
            "pod_peer": identity(self.pod_peer),
            "control_peer": identity(proxy),
        }
        write(self.directory / "peer-path-profile.json", self.descriptor)
        return {"descriptor": self.descriptor, "before": self.before, "offloads": self.offloads}

    def close(self):
        path = self.directory / "peer-path-profile.json"
        if path.exists():
            path.unlink()
        # Restore only matching owned interfaces; never overwrite a replacement.
        rows = {r["ifname"]: r for r in dump("ip", "-j", "-d", "link", "show")}
        for row in self.changed:
            if identity(rows[row["ifname"]]) != row:
                raise RuntimeError("bridge port ownership changed")
            run("bridge", "link", "set", "dev", row["ifname"], "isolated", "off")
        for saved in self.offloads:
            command = (lambda *args: inside(AP, *args)) if saved["node"] else run
            current = features(command, saved["name"])
            if any(
                v["active"] and (self.descriptor is not None or not saved["before"][k]["active"])
                for k, v in current.items()
            ):
                raise RuntimeError("offload ownership changed")
            change = [
                v
                for k, f in saved["before"].items()
                if f["active"] and f["fixed"] is False
                for v in (k, "on")
            ]
            if change:
                command("ethtool", "-K", saved["name"], *change)
            saved["after"] = features(command, saved["name"])
            if saved["after"] != saved["before"]:
                raise RuntimeError("original offloads not restored")
        self.after = dump("ip", "-j", "-d", "link", "show", "master", "em-base-bh")
        if any(r["linkinfo"]["info_slave_data"]["isolated"] for r in self.after):
            raise RuntimeError("isolation not restored")


class PathObserver:
    def __init__(self, directory):
        self.directory, self.epoch = directory, 0
        self.collector = str(uuid.uuid4())
        self.events = runpy.run_path(str(ROOT / "egress-observer.py"))["configuration_events"]
        self.channel = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
        self.channel.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
        self.channel.bind((0, 1 | 8 | 0x10 | 0x100))  # Link, TC, IPv4/IPv6 address changes.
        self.error = None

    def close(self):
        self.channel.close()

    def drain(self):
        for _ in range(128):
            if not select.select([self.channel], [], [], 0)[0]:
                return
            data, _, flags, peer = self.channel.recvmsg(65536)
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or peer[0] != 0:
                raise ValueError("lost or foreign path notification")
            self.epoch += self.events(data)
        raise ValueError("path notification budget")

    def observe(self):
        try:
            self.drain()
            path = self.directory / "peer-path-profile.json"
            if not path.exists():
                return None
            descriptor = json.loads(path.read_text())
            start, epoch = time.monotonic_ns(), self.epoch
            ports = dump("ip", "-j", "-d", "link", "show", "master", "em-base-bh")
            bridge = dump("ip", "-j", "-d", "addr", "show", "dev", "em-base-bh")
            vlans = {
                r["ifname"]: dump("bridge", "-j", "vlan", "show", "dev", r["ifname"]) for r in ports
            }
            offloads = features(run, descriptor["pod_peer"]["ifname"])
            self.drain()
            if epoch != self.epoch or self.error:
                return None
            return {
                "descriptor": descriptor,
                "collector_epoch": self.collector,
                "configuration_epoch": epoch,
                "started_ns": start,
                "ended_ns": time.monotonic_ns(),
                "ports": ports,
                "bridge": bridge,
                "offloads": offloads,
                "vlans": vlans,
                "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
        except (OSError, ValueError, KeyError) as error:
            self.error = type(error).__name__  # A lost stream never regains authority.
            return None
