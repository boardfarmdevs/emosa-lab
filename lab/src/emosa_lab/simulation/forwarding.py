"""Owned Linux-bridge observations carried by the pinned OpenSync OVSDB schema.

These are whole-interface rtnetlink counters, NOT IEEE 1905 link metrics.
The external_ids clock/epoch convention belongs only to this simulation profile.
Nothing here installs a manager on, or qualifies, a physical pod.
"""

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass

from ovs.db.error import Error as OvsError

from emosa.errors import EmosaError

PROFILE = "owned-linux-bridge-observation-v1"
NAMES = ("eth1", "eth2", "wlan0")
COUNTERS = tuple(
    f"{direction}_{field}"
    for direction in ("rx", "tx")
    for field in ("packets", "bytes", "errors", "dropped")
)
TABLES = {
    "Open_vSwitch": ["bridges"],
    "Bridge": ["name", "ports", "external_ids"],
    "Port": ["name", "interfaces"],
    "Interface": [
        "name",
        "ifindex",
        "mac_in_use",
        "admin_state",
        "link_state",
        "mtu",
        "statistics",
        "external_ids",
    ],
}


def seed_entries():
    """Stable disposable graph. No observed MAC, counter or positive state yet."""
    entries = []
    for index, name in enumerate(NAMES):
        entries += [
            ("Interface", f"forwardif{index}", {"name": name}),
            (
                "Port",
                f"forwardport{index}",
                {"name": name, "interfaces": ["set", [["named-uuid", f"forwardif{index}"]]]},
            ),
        ]
    return entries + [
        (
            "Bridge",
            "forwardbridge",
            {
                "name": "br-lan",
                "ports": ["set", [["named-uuid", f"forwardport{i}"] for i in range(len(NAMES))]],
            },
        ),
        ("Open_vSwitch", "forwardroot", {"bridges": ["set", [["named-uuid", "forwardbridge"]]]}),
    ]


def integer(value, low=0, high=2**63 - 1):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid forwarding observation integer")
    return value


def mac(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", value)
        or int(value[:2], 16) & 1
        or value == "00:00:00:00:00:00"
    ):
        raise ValueError("invalid forwarding interface MAC")
    return value


def link_identity(rows):
    """The complete bridge-member set must match the owned three-port profile."""
    by_name = {r["ifname"]: r for r in rows}
    if len(by_name) != len(rows):
        raise ValueError("duplicate interface name")
    bridge = by_name["br-lan"]
    if (
        bridge["linkinfo"]["info_kind"] != "bridge"
        or "UP" not in bridge["flags"]
        or {r["ifname"] for r in rows if r.get("master") == "br-lan"} != set(NAMES)
    ):
        raise ValueError("incomplete owned bridge inventory")
    result = []
    for name in NAMES:
        row = by_name[name]
        if (
            row["link_type"] != "ether"
            or "UP" not in row["flags"]
            or "LOWER_UP" not in row["flags"]
            or row["linkinfo"]["info_slave_kind"] != "bridge"
            or row["linkinfo"]["info_slave_data"]["state"] != "forwarding"
            or (name != "wlan0" and row["linkinfo"]["info_kind"] != "veth")
        ):
            raise ValueError("port is not in the observed forwarding state")
        result.append(
            (
                name,
                integer(row["ifindex"], 1, 2**32 - 1),
                mac(row["address"]),
                integer(row["mtu"], 68),
                integer(row.get("link_index", 0)),
            )
        )
    if len({r[1] for r in result}) != len(NAMES) or len({r[2] for r in result}) != len(NAMES):
        raise ValueError("ambiguous forwarding identities")
    return (integer(bridge["ifindex"], 1), mac(bridge["address"]), tuple(result))


def normalize(observation):
    """Only observed driver values enter OVSDB, never desired configuration."""
    if observation["complete"] is not True:
        raise ValueError("forwarding observation unavailable")
    start = integer(observation["started_ns"], 1)
    end = integer(observation["ended_ns"], start, start + 1_000_000_000)
    boot = str(uuid.UUID(observation["boot_id"]))
    netns = str(integer(observation["netns_inode"], 1))
    identity = link_identity(observation["links"])
    if identity != link_identity(observation["links_after"]):
        raise ValueError("forwarding topology changed during counter read")
    common = {
        "emosa_profile": PROFILE,
        "complete": "true",
        "boot_id": boot,
        "netns_inode": netns,
        "started_ns": str(start),
        "ended_ns": str(end),
    }
    rows = {r["ifname"]: r for r in observation["links"]}
    interfaces = {}
    for name, ifindex, address, mtu, peer in identity[2]:
        stats = rows[name]["stats64"]
        counters = {
            f"{d}_{k}": integer(stats[d][k])
            for d in ("rx", "tx")
            for k in ("packets", "bytes", "errors", "dropped")
        }
        interfaces[name] = {
            "ifindex": ifindex,
            "mac_in_use": address,
            "mtu": mtu,
            "admin_state": "up",
            "link_state": "up",
            "statistics": ["map", sorted(counters.items())],
            "external_ids": ["map", sorted((common | {"peer_ifindex": str(peer)}).items())],
        }
        for key in ("neighbor_observation", "egress_observation", "peer_path_observation"):
            if name == "eth1" and observation.get(key) is not None:
                payload = json.dumps(observation[key], separators=(",", ":"))
                if len(payload) > 262144:
                    raise ValueError("observation exceeds the local budget")
                interfaces[name]["external_ids"][1].append((key, payload))
    return interfaces, [
        "map",
        sorted((common | {"bridge_ifindex": str(identity[0]), "bridge_mac": identity[1]}).items()),
    ]


def graph(snapshot):
    """Follow actual UUID edges. Names alone cannot establish bridge membership."""
    decoded = {
        t: {u: snapshot["schema"].row(t, row) for u, row in snapshot["tables"].get(t, {}).items()}
        for t in TABLES
    }
    if any(
        len(decoded[t]) != n
        for t, n in (("Open_vSwitch", 1), ("Bridge", 1), ("Port", 3), ("Interface", 3))
    ):
        raise ValueError("ambiguous forwarding OVSDB graph")
    (root,) = decoded["Open_vSwitch"].values()
    ((bridge_id, bridge),) = decoded["Bridge"].items()
    if (
        root["bridges"] != [bridge_id]
        or bridge["name"] != "br-lan"
        or set(bridge["ports"]) != set(decoded["Port"])
    ):
        raise ValueError("incomplete forwarding bridge references")
    interfaces = {r["name"]: (u, r) for u, r in decoded["Interface"].items()}
    if set(interfaces) != set(NAMES) or {r["name"] for r in decoded["Port"].values()} != set(NAMES):
        raise ValueError("unexpected forwarding port set")
    if any(p["interfaces"] != [interfaces[p["name"]][0]] for p in decoded["Port"].values()):
        raise ValueError("inconsistent forwarding port references")
    return bridge_id, bridge, interfaces


def updates(snapshot, observation):
    """Guard and atomically publish/withdraw the independently observed graph."""
    bridge_id, _, interfaces = graph(snapshot)
    try:
        values, bridge_metadata = normalize(observation)
        available = True
    except (KeyError, TypeError, ValueError):
        values = {
            name: {
                "external_ids": ["map", [["emosa_profile", PROFILE], ["complete", "false"]]],
                "statistics": ["map", []],
                "mac_in_use": ["set", []],
                "ifindex": ["set", []],
                "admin_state": ["set", []],
                "link_state": ["set", []],
                "mtu": ["set", []],
            }
            for name in NAMES
        }
        bridge_metadata = ["map", [["emosa_profile", PROFILE], ["complete", "false"]]]
        available = False
    ops = []
    for table in TABLES:
        rows = snapshot["tables"][table]
        ops.append(
            {
                "op": "wait",
                "table": table,
                "where": [],
                "columns": ["_uuid", *TABLES[table]],
                "until": "==",
                "timeout": 0,
                "rows": [{"_uuid": ["uuid", u], **r} for u, r in rows.items()],
            }
        )
    for name, (u, _) in interfaces.items():
        ops.append(
            {
                "op": "update",
                "table": "Interface",
                "where": [["_uuid", "==", ["uuid", u]]],
                "row": values[name],
            }
        )
    ops.append(
        {
            "op": "update",
            "table": "Bridge",
            "where": [["_uuid", "==", ["uuid", bridge_id]]],
            "row": {"external_ids": bridge_metadata},
        }
    )
    return ops, {
        "available": available,
        "interfaces": values,
        "bridge_external_ids": bridge_metadata,
    }


@dataclass(frozen=True)
class Sample:
    generation: int
    started_ns: int
    ended_ns: int
    epoch: tuple
    interfaces: dict
    neighbor_observation: dict | None = None
    egress_observation: dict | None = None
    peer_path_observation: dict | None = None


class ForwardingSource:
    """Read-only, generation/epoch-aware raw interval source; never a wire publisher."""

    def __init__(self, clock=time.monotonic_ns):
        self.clock = clock
        self.previous = self.sample = None
        self.window = None
        self.watermark = 0
        self.schema_fingerprint = None
        self.reason = "awaiting_observation"

    def invalidate(self, reason="source_unavailable"):
        self.previous = self.sample = self.window = None
        self.reason = reason

    def refresh(self, snapshot):
        try:
            if not snapshot["ready"]:
                raise ValueError("source unavailable")
            if self.schema_fingerprint != snapshot["schema"].fingerprint:
                snapshot["schema"].qualify_synthetic(tables=TABLES)
                self.schema_fingerprint = snapshot["schema"].fingerprint
            _, bridge, interfaces = graph(snapshot)
            metadata = bridge["external_ids"]
            if metadata["emosa_profile"] != PROFILE or metadata["complete"] != "true":
                raise ValueError("observation unavailable")
            start = integer(int(metadata["started_ns"]), 1)
            end = integer(int(metadata["ended_ns"]), start, start + 1_000_000_000)
            # All processes use the owned VM's shared monotonic clock. Bound the
            # oldest possible reading, not merely the end of the read operation.
            if not end <= self.clock() < start + 2_000_000_000:
                raise ValueError("stale or future observation")
            boot = str(uuid.UUID(metadata["boot_id"]))
            netns = integer(int(metadata["netns_inode"]), 1)
            epoch = [
                snapshot["generation"],
                boot,
                netns,
                integer(int(metadata["bridge_ifindex"]), 1),
                mac(metadata["bridge_mac"]),
            ]
            result = {}
            for name in NAMES:
                row = interfaces[name][1]
                if (
                    row["admin_state"] != "up"
                    or row["link_state"] != "up"
                    or any(
                        row["external_ids"].get(k) != v
                        for k, v in metadata.items()
                        if k not in ("bridge_ifindex", "bridge_mac")
                    )
                ):
                    raise ValueError("incomplete or inconsistent interface observation")
                counters = row["statistics"]
                if set(counters) != set(COUNTERS):
                    raise ValueError("incomplete interface counters")
                counters = {k: integer(v) for k, v in counters.items()}
                ident = (
                    name,
                    integer(row["ifindex"], 1),
                    mac(row["mac_in_use"]),
                    integer(int(row["external_ids"]["peer_ifindex"])),
                    integer(row["mtu"], 68),
                )
                epoch.append(ident)
                result[name] = {
                    "ifindex": ident[1],
                    "mac": ident[2],
                    "peer_ifindex": ident[3],
                    "mtu": ident[4],
                    "counters": counters,
                }
            if (
                len({r["mac"] for r in result.values()}) != 3
                or len({r["ifindex"] for r in result.values()}) != 3
            ):
                raise ValueError("duplicate interface identity")
            payload = interfaces["eth1"][1]["external_ids"].get("neighbor_observation")
            if payload is not None and len(payload) > 262144:
                raise ValueError("neighbor observation exceeds the local budget")
            egress = interfaces["eth1"][1]["external_ids"].get("egress_observation")
            if egress is not None and len(egress) > 262144:
                raise ValueError("egress observation exceeds the local budget")
            path = interfaces["eth1"][1]["external_ids"].get("peer_path_observation")
            if path is not None and len(path) > 262144:
                raise ValueError("peer path observation exceeds the local budget")
            sample = Sample(
                snapshot["generation"],
                start,
                end,
                tuple(epoch),
                result,
                json.loads(payload) if payload is not None else None,
                json.loads(egress) if egress is not None else None,
                json.loads(path) if path is not None else None,
            )
            if self.sample == sample:
                return
            if start <= self.watermark:
                raise ValueError("replayed or reordered observation")
            self.watermark = start
            previous, self.sample, self.window = self.previous, sample, None
            self.previous = sample
            self.reason = "awaiting_second_sample"
            if previous is None or previous.epoch != sample.epoch:
                return
            if start <= previous.ended_ns or start - previous.started_ns >= 2_000_000_000:
                self.reason = "measurement_gap"
                return
            delta = {
                name: {
                    k: sample.interfaces[name]["counters"][k]
                    - previous.interfaces[name]["counters"][k]
                    for k in COUNTERS
                }
                for name in NAMES
            }
            if any(v < 0 for counters in delta.values() for v in counters.values()):
                self.reason = "counter_reset"
                return
            self.window = {
                "first_read_ns": [previous.started_ns, previous.ended_ns],
                "last_read_ns": [start, end],
                "deltas": delta,
            }
            self.reason = "raw_interface_window_available"
        except (EmosaError, OvsError, KeyError, TypeError, ValueError, OverflowError):
            self.invalidate("unavailable_or_invalid_observation")

    def status(self):
        if self.sample and self.clock() >= self.sample.started_ns + 2_000_000_000:
            self.invalidate("observation_expired")
        return {
            "profile": PROFILE,
            "available": self.sample is not None,
            "reason": self.reason,
            "sample": asdict(self.sample) if self.sample else None,
            "window": self.window,
            "counter_scope": "whole-interface-not-per-neighbor",
            "measurement_source_qualified": False,
        }
