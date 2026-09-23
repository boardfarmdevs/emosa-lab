"""Owned simulated Ethernet service, with explicit framing and reversible shaping.

Only the existing isolated pod/controller lab is supported. This is an emulated
service contract, not a physical PHY measurement or a production pod profile.
"""

import json

from common import AP, guard, inside

HANDLE = "4e00:"


def qdiscs():
    return json.loads(inside(AP, "tc", "-j", "-d", "-s", "qdisc", "show", "dev", "eth1"))


def configuration(rows):
    return [
        {k: v for k, v in q.items() if k in ("kind", "handle", "root", "options", "stab")}
        for q in rows
    ]


class VirtualLink:
    def __init__(self):
        self.before = None
        self.installed = False
        self.after = None
        self.configured = None

    def start(self):
        guard()
        self.before = qdiscs()
        if len(self.before) != 1 or self.before[0]["kind"] != "noqueue":
            raise RuntimeError("preserve existing pod backhaul qdisc")
        link = json.loads(inside(AP, "ip", "-j", "-d", "link", "show", "eth1"))[0]
        if link["linkinfo"]["info_kind"] != "veth" or link.get("xdp") or link["master"] != "br-lan":
            raise RuntimeError("expected owned plain veth backhaul")
        for hook in ("root", "ingress", "egress"):
            if json.loads(inside(AP, "tc", "-j", "filter", "show", "dev", "eth1", hook)):
                raise RuntimeError("preserve existing filter path")
        self.installed = True
        inside(
            AP,
            "tc",
            "qdisc",
            "add",
            "dev",
            "eth1",
            "root",
            "handle",
            HANDLE,
            "stab",
            "mtu",
            "2048",
            "tsize",
            "2048",
            "overhead",
            "24",
            "mpu",
            "84",
            "linklayer",
            "ethernet",
            "tbf",
            "rate",
            "100mbit",
            "burst",
            "64kb",
            "limit",
            "512kb",
        )
        configured = qdiscs()
        if (
            len(configured) != 1
            or configured[0]["kind"] != "tbf"
            or configured[0]["handle"] != HANDLE
        ):
            raise RuntimeError("unexpected installed virtual service")
        self.configured = configured
        return self.configured

    def close(self):
        if self.installed:
            current = qdiscs()
            if self.configured and configuration(current) == configuration(self.configured):
                inside(AP, "tc", "qdisc", "del", "dev", "eth1", "root", "handle", HANDLE)
            elif current != self.before:
                raise RuntimeError("traffic-control ownership changed; preserve for inspection")
            self.after = qdiscs()
            if self.after != self.before:
                raise RuntimeError("original traffic control not restored")
            self.installed = False
