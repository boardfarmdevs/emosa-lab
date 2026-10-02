#!/usr/bin/env python3
"""storm-watch.py POD [GROWTH_MB] [SECONDS]: run as root inside the lab VM.

Every second: MemAvailable, the fastest-growing slab caches, and the interfaces
with the most packets per second across the VM and every container network
namespace. When the slab total grows more than GROWTH_MB above its start, the
wireless interfaces in POD's namespace are set down (cuts a loop through the
pod) and the watcher exits 3. Log lines go to stdout.

In run m2-unpinned the cut came too late: the loop had taken every vCPU.
(Formatted for the repository's lint after the runs; same behaviour.)
"""

import glob
import os
import subprocess
import sys
import time

POD = sys.argv[1]
GROWTH = int(sys.argv[2] if len(sys.argv) > 2 else 1500) << 20
DURATION = int(sys.argv[3] if len(sys.argv) > 3 else 600)
WIRELESS = "for d in /sys/class/net/*; do [ -e $d/phy80211 ] && basename $d; done"


def read(path):
    with open(path) as f:
        return f.read()


def meminfo():
    for line in read("/proc/meminfo").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) << 10


def slabs():
    out = {}
    for line in read("/proc/slabinfo").splitlines()[2:]:
        f = line.split()
        out[f[0]] = int(f[2]) * int(f[3])
    return out


def namespaces():
    """{netns inode: (label, pid)}: the VM itself plus one pid per container netns."""
    seen = {}
    for p in glob.glob("/proc/[0-9]*"):
        pid = p[6:]
        try:
            ino = os.stat(f"{p}/ns/net").st_ino
            if ino in seen:
                continue
            cg = read(f"{p}/cgroup")
        except OSError:
            continue
        label = "vm"
        for part in cg.replace("/", " ").split():
            if part.startswith("lxc.payload."):
                label = part[len("lxc.payload.") :]
                break
        seen[ino] = (label, pid)
    return seen


def counters(ns):
    out = {}
    for label, pid in ns.values():
        try:
            lines = read(f"/proc/{pid}/net/dev").splitlines()[2:]
        except OSError:
            continue
        for line in lines:
            name, data = line.split(":", 1)
            f = data.split()
            out[f"{label}/{name.strip()}"] = (int(f[1]), int(f[9]), int(f[0]), int(f[8]))
    return out


def pod_pid():
    info = subprocess.run(["/snap/bin/lxc", "info", POD], capture_output=True, text=True).stdout
    for line in info.splitlines():
        if line.startswith("PID:"):
            return line.split()[1]


def cut():
    pid = pod_pid()
    if not pid:
        return "no pid"
    names = subprocess.run(
        ["nsenter", "-t", pid, "-n", "sh", "-c", WIRELESS], capture_output=True, text=True
    ).stdout.split()
    for n in names:
        subprocess.run(["nsenter", "-t", pid, "-n", "ip", "link", "set", n, "down"])
    return " ".join(names)


def stamp():
    return time.strftime("%T")


ns = namespaces()
ns_at = time.time()
s0 = slabs()
base = sum(s0.values())
prev_s, prev_c = s0, counters(ns)
print(f"{stamp()} start avail={meminfo() >> 20}MB slab={base >> 20}MB netns={len(ns)}", flush=True)
end = time.time() + DURATION
while time.time() < end:
    time.sleep(1)
    if time.time() - ns_at > 10:
        ns, ns_at = namespaces(), time.time()
    s, c = slabs(), counters(ns)
    total = sum(s.values())
    grow = sorted(((s[k] - prev_s.get(k, 0), k) for k in s), reverse=True)[:3]
    rates = sorted(
        (
            (sum(v[:2]) - sum(prev_c[k][:2]), k, v[0] - prev_c[k][0], v[1] - prev_c[k][1])
            for k, v in c.items()
            if k in prev_c
        ),
        reverse=True,
    )[:6]
    print(
        f"{stamp()} avail={meminfo() >> 20}MB slab=+{(total - base) >> 20}MB "
        + " ".join(f"{k}:{d >> 10:+d}K" for d, k in grow if d > 0)
        + " | "
        + " ".join(f"{k} rx{rx} tx{tx}" for _, k, rx, tx in rates if rx + tx > 200),
        flush=True,
    )
    prev_s, prev_c = s, c
    if total - base > GROWTH:
        grown = (total - base) >> 20
        print(f"{stamp()} TRIP: slab grew {grown}MB; down in {POD}: {cut()}", flush=True)
        sys.exit(3)
print(f"{stamp()} done, no trip", flush=True)
