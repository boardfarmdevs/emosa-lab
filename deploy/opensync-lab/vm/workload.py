#!/usr/bin/env python3
"""Recovery workload (proof plan M7): the EasyMesh lab under faults, in one run.

Inside the lab VM (root): python3 workload.py LABEL [--duration 900]

Every client pings the internet once a second for the whole run; a sampler
records every 5 s what EMOSA's agents, the controller's own data model and the
pods report. Scheduled faults:

  client leave/join    wpa_cli disconnect, then reconnect (em-wc2, later em-wc4)
  adapter restart      pod-1's agent process restarted (systemctl)
  transport cut        pod-2's agent port dropped for 20 s (VM firewall, both
                       directions: the session dies and reconnects are refused)
  backhaul loss        pod-3's backhaul STA down for 30 s (GRE, LAN and management)
  controller restart   prplMesh stopped and started; the operator re-enters the
                       policy 20 s later (prplMesh keeps BML credentials in memory)

Healthy = every agent provisioning with a live source, every pod connected to
EMOSA, the controller showing each pod with its expected stations, and every
connected client's ping succeeding. Agents are found from the lab's own files,
per-pod (lab.sh agent) or fleet (lab.sh admit) alike. Recovery time per fault is measured to the
first healthy sample after the fault ends. Writes runs/LABEL/ with timeline,
faults, per-client ping logs and summary.json (verdict per check).
"""

import argparse
import json
import subprocess
import threading
import time
from pathlib import Path

PODS = {"pod-1": "em-wc1 em-wc2", "pod-2": "em-wc3 em-wc4", "pod-3": "em-wc5 em-wc6"}
CLIENTS = [c for v in PODS.values() for c in v.split()]
ROOT = Path("/var/lib/emosa-lab/runs")
POLICY = ("emosa-mesh", "EmosaMesh2026!")  # public lab test values
BAD = {"INDETERMINATE", "FAILED", "OWNERSHIP_CONFLICT", "TIMED_OUT"}
WAN_HOST = "10.101.0.1"  # where pods reach EMOSA (lab.sh WAN_HOST)
AGENT_OF = {}  # pod container -> its agent (discover)
FAULT_NOTES = {}  # fault name -> what it acted on, when that varies


def sh(*args, timeout=60, check=False):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{args}: {r.stderr.strip()}")
    return r.stdout


def cx(container, *args, **kw):
    return sh("lxc", "exec", container, "--", *args, **kw)


AGENTS = r"""
import json, glob, re
out = {}
for f in sorted(glob.glob("/var/lib/emosa/*/status.json")):
    s = json.load(open(f)); pid = s["worker_pid"]
    try:
        rss = int(re.search(r"VmRSS:\s+(\d+)", open(f"/proc/{pid}/status").read()).group(1))
    except OSError:
        rss = None
    out[s["pod_id"]] = {
        "session": (s.get("session") or {}).get("state"),
        "source": s["report_source_available"],
        "ops": [o["state"] for o in s["operations"]],
        "writes": s["writes"], "pid": pid, "rss_kib": rss,
        "stations": len((s.get("pod") or {}).get("stations") or []),
        "updated": s["updated"],
    }
try:
    fleet = json.load(open("/var/lib/emosa/fleet.json"))
    out["_fleet"] = {k: v["handovers"] for k, v in fleet.items()}
except OSError:
    pass
print(json.dumps(out))
"""


def discover():
    """Each pod's agent (per-pod or fleet) from its configuration in the emosa container."""
    for pod in PODS:
        serial = cx(pod, "/usr/opensync/tools/ovsh", "-r", "s", "AWLAN_Node", "serial_number")
        for name in (pod, serial.strip()):
            text = cx("emosa", "cat", f"/etc/emosa/{name}.json")
            if text:
                config = json.loads(text)
                AGENT_OF[pod] = {
                    "pod_id": config["pod_id"],
                    "unit": f"emosa-agent@{config['pod_id']}",
                    "al": config["al_mac"],
                    "port": int(config["ovsdb"].split(":")[1]),
                }
                break
        else:
            raise SystemExit(f"{pod}: no EMOSA agent configured")
    return AGENT_OF


def sample(expected):
    t = time.time()
    try:
        raw = json.loads(cx("emosa", "python3", "-c", AGENTS, timeout=20) or "{}")
    except (ValueError, subprocess.TimeoutExpired):
        raw = {}
    agents = {p: raw[a["pod_id"]] for p, a in AGENT_OF.items() if a["pod_id"] in raw}
    try:
        topo = json.loads(sh("curl", "-s", "-m", "8", "http://127.0.0.1:8092/api/topology"))
        controller = {
            d["id"]: sum(len(b["clients"]) for r in d["radios"] for b in r["bsses"])
            for d in topo.get("devices", [])
        }
    except (ValueError, subprocess.TimeoutExpired):
        controller = {}
    pods = {}
    for pod in PODS:
        out = cx(pod, "/usr/opensync/tools/ovsh", "-r", "s", "Manager", "is_connected", timeout=15)
        pods[pod] = out.strip() == "true"
    healthy = (
        all(
            agents.get(p, {}).get("session") == "provisioning" and agents.get(p, {}).get("source")
            for p in PODS
        )
        and all(pods.values())
        and all(controller.get(AGENT_OF[p]["al"]) == expected[p] for p in PODS)
    )
    return {
        "t": t,
        "agents": agents,
        "fleet_handovers": raw.get("_fleet"),
        "controller_stations": controller,
        "pods": pods,
        "healthy_management": healthy,
    }


def start_pings(label):
    loop = (
        "while :; do if ping -c1 -W1 8.8.8.8 >/dev/null 2>&1; then r=1; else r=0; fi; "
        "s=$(wpa_cli -i wlan0 status 2>/dev/null | grep ^wpa_state= | cut -d= -f2); "
        'echo "$(date +%s) $r $s"; '
        "sleep 1; done"
    )
    for c in CLIENTS:
        cx(
            c,
            "sh",
            "-c",
            f"nohup sh -c '{loop}' > /tmp/wl-{label}.log 2>&1 & echo $! > /tmp/wl-{label}.pid",
        )


def stop_pings(label, directory):
    (directory / "pings").mkdir(exist_ok=True)
    for c in CLIENTS:
        cx(c, "sh", "-c", f"kill $(cat /tmp/wl-{label}.pid) 2>/dev/null")
        (directory / "pings" / f"{c}.log").write_text(cx(c, "cat", f"/tmp/wl-{label}.log"))


def policy():
    cx(
        "em-ctl",
        "em-ctl-node",
        "policy",
        *POLICY,
        *(a["al"] for a in AGENT_OF.values()),
        timeout=60,
    )


# (offset s, name, action) - action returns the time the fault ended
def fault_client(client, gap=40):
    def run():
        cx(client, "wpa_cli", "-i", "wlan0", "disconnect")
        time.sleep(gap)
        cx(client, "wpa_cli", "-i", "wlan0", "reconnect")
        return time.time()

    return run


def fault_adapter():
    cx("emosa", "systemctl", "restart", AGENT_OF["pod-1"]["unit"], check=True)
    return time.time()


def fault_transport():
    port = str(AGENT_OF["pod-2"]["port"])
    rules = (
        ["INPUT", "-d", WAN_HOST, "-p", "tcp", "--dport", port, "-j", "DROP"],
        ["OUTPUT", "-s", WAN_HOST, "-p", "tcp", "--sport", port, "-j", "DROP"],
    )
    for rule in rules:
        sh("iptables", "-I", *rule, check=True)
    try:
        time.sleep(20)
    finally:
        for rule in rules:
            sh("iptables", "-D", *rule, check=True)
    return time.time()


def backhaul_station(pod):
    """The pod's connected backhaul station: bhaul-sta-50 to mv3, or bhaul-sta-24 when
    its uplink was moved (lab.sh uplink, data plane experiments)."""
    for name in ("bhaul-sta-50", "bhaul-sta-24"):
        if "Connected" in cx(pod, "iw", "dev", name, "link"):
            return name
    raise RuntimeError(f"{pod}: no connected backhaul station")


def fault_backhaul():
    station = backhaul_station("pod-3")
    FAULT_NOTES["backhaul loss 30 s (pod-3)"] = station
    cx("pod-3", "ip", "link", "set", station, "down", check=True)
    time.sleep(30)
    cx("pod-3", "ip", "link", "set", station, "up", check=True)
    return time.time()


def fault_controller():
    cx(
        "em-ctl",
        "sh",
        "-c",
        "em-ctl-node stop >/dev/null; em-ctl-node prepare >/dev/null; em-ctl-node start",
        timeout=120,
        check=True,
    )
    time.sleep(20)
    policy()
    return time.time()


SCHEDULE = [
    (60, "client leave/join em-wc2", fault_client("em-wc2"), {"pod-1": 1}),
    (150, "adapter process restart (pod-1)", fault_adapter, {}),
    (240, "OVSDB transport cut 20 s (pod-2)", fault_transport, {}),
    (360, "backhaul loss 30 s (pod-3)", fault_backhaul, {}),
    (510, "controller restart + policy re-entry", fault_controller, {}),
    (690, "client leave/join em-wc4", fault_client("em-wc4"), {"pod-2": 1}),
]


def outages(log, start, end):
    """Consecutive failed-ping seconds per client within [start, end]."""
    spans, current = [], None
    for line in log.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        t, ok = int(parts[0]), parts[1] == "1"
        if not start <= t <= end:
            continue
        if not ok and current is None:
            current = [t, t]
        elif not ok:
            current[1] = t
        elif current is not None:
            spans.append(current)
            current = None
    if current is not None:
        spans.append(current)
    return spans


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label")
    ap.add_argument("--duration", type=int, default=900)
    args = ap.parse_args()
    directory = ROOT / args.label
    directory.mkdir(parents=True, exist_ok=False)
    expected = {p: 2 for p in PODS}
    print(json.dumps(discover()), flush=True)
    first = sample(expected)
    if not first["healthy_management"]:
        raise SystemExit(f"lab not healthy at start: {json.dumps(first)}")
    start_pings(args.label)
    t0 = time.time()
    timeline = (directory / "timeline.jsonl").open("w")
    faults = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            s = sample(current_expected)
            s["offset"] = round(s["t"] - t0, 1)
            timeline.write(json.dumps(s) + "\n")
            timeline.flush()
            samples.append(s)
            stop.wait(5)

    samples, current_expected = [first], dict(expected)
    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    for offset, name, action, degraded in SCHEDULE:
        while time.time() - t0 < offset:
            time.sleep(0.5)
        began = time.time()
        current_expected = {**expected, **degraded}
        print(f"[{began - t0:6.1f}s] fault: {name}", flush=True)
        ended = action()
        current_expected = dict(expected)
        faults.append(
            {
                "name": name,
                "began": began,
                "ended": ended,
                "offset": round(began - t0, 1),
                **({"acted_on": FAULT_NOTES[name]} if name in FAULT_NOTES else {}),
            }
        )
    while time.time() - t0 < args.duration:
        time.sleep(1)
    stop.set()
    thread.join()
    timeline.close()
    t_end = time.time()
    stop_pings(args.label, directory)
    last = sample(expected)

    # recovery per fault: first healthy sample (management and every client ping)
    pings = {c: (directory / "pings" / f"{c}.log").read_text() for c in CLIENTS}
    ping_ok = {
        c: {int(line.split()[0]) for line in v.splitlines() if line.split()[1:2] == ["1"]}
        for c, v in pings.items()
    }
    for f in faults:
        rec = None
        for s in samples:
            if s["t"] < f["ended"] or not s["healthy_management"]:
                continue
            sec = int(s["t"])
            if all(any(t in ping_ok[c] for t in range(sec - 2, sec + 3)) for c in CLIENTS):
                rec = s["t"]
                break
        f["recovered_after_s"] = round(rec - f["ended"], 1) if rec else None
        f["outage_window_s"] = round((rec or t_end) - f["began"], 1)
    per_client = {c: outages(pings[c], int(t0), int(t_end)) for c in CLIENTS}
    rss = {
        p: (first["agents"].get(p, {}).get("rss_kib"), last["agents"].get(p, {}).get("rss_kib"))
        for p in PODS
    }
    ops = {p: last["agents"].get(p, {}).get("ops", []) for p in PODS}
    checks = {
        "healthy_at_end": last["healthy_management"],
        "every_fault_recovered": all(f["recovered_after_s"] is not None for f in faults),
        "no_unresolved_operations": not any(set(v) & BAD for v in ops.values()),
        "every_client_passing_at_end": all(
            any(t in ping_ok[c] for t in range(int(t_end) - 5, int(t_end) + 1)) for c in CLIENTS
        ),
        "agent_memory_bounded": all(a and b and b < 2 * a for a, b in rss.values()),
    }
    summary = {
        "label": args.label,
        "duration_s": round(t_end - t0),
        "faults": faults,
        "client_outages": {
            c: [[a - int(t0), b - int(t0)] for a, b in v] for c, v in per_client.items()
        },
        "operations": ops,
        "writes": {p: last["agents"].get(p, {}).get("writes") for p in PODS},
        "agents": AGENT_OF,
        "fleet_handovers_start_end": [first.get("fleet_handovers"), last.get("fleet_handovers")],
        "agent_rss_kib_start_end": rss,
        "samples": len(samples),
        "checks": checks,
        "passed": all(checks.values()),
        "scope": "hwsim lab; operational recovery (plan M7), not complete sustained reporting",
    }
    (directory / "faults.json").write_text(json.dumps(faults, indent=1))
    (directory / "summary.json").write_text(json.dumps(summary, indent=1))
    print(
        json.dumps(
            {
                "passed": summary["passed"],
                "checks": checks,
                "recovery_s": {f["name"]: f["recovered_after_s"] for f in faults},
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
