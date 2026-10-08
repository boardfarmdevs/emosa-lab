# SPDX-License-Identifier: Apache-2.0
"""The other agents of the fleet, as their statuses give them (spec 8.5).

A pod on its EasyMesh backhaul may have another pod's backhaul BSS as its upstream. The agents
then describe that hop to the controller: the child names its parent's agent as its 1905
neighbor on the backhaul, the parent names the child on its backhaul BSS, and both answer a
Link Metric Query for the pair from the parent's measurement of the child's station. No 1905
frame tells an agent about another pod, so each reads the others' statuses in the fleet's run
root (read-only), at most once a second, leaving out an agent whose process is gone.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

REFRESH = 1.0  # seconds between two reads of the run root


def mac_bytes(text):
    """A MAC address as six bytes, or None when it is not one."""
    if not isinstance(text, str):
        return None
    try:
        value = bytes.fromhex(text.replace(":", ""))
    except ValueError:
        return None
    return value if len(value) == 6 else None


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class Peer:
    pod_id: str
    al: bytes  # its agent's AL MAC
    backhaul_bsses: frozenset  # its backhaul BSSes' BSSIDs
    station: bytes | None  # its backhaul station's MAC, while on its EasyMesh backhaul
    parent: bytes | None  # that station's parent BSSID
    measured: dict  # its pod's stations' measurements (spec 3.6), by lower-case MAC text


def peer(pod_id, status):
    """The peer a status describes, or None when it names no AL MAC."""
    al = mac_bytes(status.get("agent_al"))
    if al is None:
        return None
    pod = status.get("pod") or {}
    backhaul = pod.get("backhaul") or {}
    listed = [x for x in pod.get("bsses") or [] if isinstance(x, dict)]
    bsses = frozenset(
        b for b in (mac_bytes(x.get("bssid")) for x in listed if x.get("role") == "backhaul") if b
    )
    measured = (status.get("telemetry") or {}).get("stations") or {}
    return Peer(
        str(status.get("pod_id") or pod_id),
        al,
        bsses,
        mac_bytes(backhaul.get("mac")),
        mac_bytes(backhaul.get("parent")),
        measured if isinstance(measured, dict) else {},
    )


class PeerDirectory:
    """The fleet's other agents, read from the statuses beside this agent's run directory."""

    def __init__(self, run_dir, pod_id, *, clock=time.monotonic, alive=process_alive):
        self.root, self.pod_id = Path(run_dir).parent, pod_id
        self.clock, self.alive = clock, alive
        self.read_at, self.cache = None, ()

    def peers(self):
        now = self.clock()
        if self.read_at is not None and now - self.read_at < REFRESH:
            return self.cache
        found = []
        for path in sorted(self.root.glob("*/status.json")):
            if path.parent.name == self.pod_id:
                continue
            try:
                status = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            pid = status.get("worker_pid") if isinstance(status, dict) else None
            if not isinstance(pid, int) or isinstance(pid, bool) or not self.alive(pid):
                continue
            item = peer(path.parent.name, status)
            if item is not None:
                found.append(item)
        self.cache, self.read_at = tuple(found), now
        return self.cache


def upstream(peers, parent):
    """The peer one of whose backhaul BSSes is ``parent``, or None."""
    return next((p for p in peers if parent is not None and parent in p.backhaul_bsses), None)


def children(peers, backhaul_stations):
    """(BSSID, peer) for each peer on one of this pod's backhaul BSSes: its backhaul's parent is
    that BSS and its station is among the BSS's associated clients (``backhaul_stations``: each
    backhaul BSSID to its associated stations' MACs)."""
    return tuple(
        (p.parent, p)
        for p in peers
        if p.parent in backhaul_stations and p.station in backhaul_stations[p.parent]
    )


def loops(peers, own_al, own_bsses, target):
    """True when an upstream ``target`` would put a loop into ``br-home``: one of this pod's own
    BSSes, or a BSS of a pod whose upstream chain reaches this pod (or runs in a circle)."""
    seen, bssid = set(), target
    while bssid is not None:
        if bssid in own_bsses:
            return True
        owner = upstream(peers, bssid)
        if owner is None:
            return False
        if owner.al == own_al or owner.al in seen:
            return True
        seen.add(owner.al)
        bssid = owner.parent
    return False
