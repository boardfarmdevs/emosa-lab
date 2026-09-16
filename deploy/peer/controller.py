"""Start/stop the pinned independent peer inside its dedicated LXD container.

Does not install dependencies, create a BSS policy or send EMOSA protocol messages.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path

INSTALL = Path("/opt/prpl-install-nl80211")
# Retain failed transient units so an abort cannot appear as a clean stop.
PREFIX = ["systemd-run", "--quiet", "--property=Type=exec"]
UNITS = {
    "bus": "/usr/sbin/ubusd",
    "transport": str(INSTALL / "bin/ieee1905_transport"),
    "controller": str(INSTALL / "bin/beerocks_controller"),
    "agent": str(INSTALL / "bin/beerocks_agent"),
}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def configure(path, values):
    lines = path.read_text().splitlines()
    for key, value in values.items():
        if sum(line.startswith(key + "=") for line in lines) != 1:
            raise SystemExit(f"Expected one {key} in {path}")
        lines = [f"{key}={value}" if line.startswith(key + "=") else line for line in lines]
    path.write_text("\n".join(lines) + "\n")


def guard():
    if os.geteuid() != 0 or socket.gethostname() != "em-controller":
        raise SystemExit("Run only as root in the dedicated em-controller container")
    if run("systemd-detect-virt", "--container").strip() != "lxc":
        raise SystemExit("Expected an LXD system container")
    reference = json.loads(Path(__file__).with_name("prplmesh.reference.json").read_text())
    for binary, expected in reference["binaries"].items():
        if hashlib.sha256((INSTALL / "bin" / binary).read_bytes()).hexdigest() != expected:
            raise SystemExit("Controller binary differs from the pinned candidate")
    return reference


def start(reference):
    for name in UNITS:
        state = run(
            "systemctl", "show", f"emosa-peer-{name}.service", "-p", "LoadState", "--value"
        ).strip()
        if state != "not-found":
            raise SystemExit(
                f"emosa-peer-{name} is still loaded; "
                "inspect/stop it or reset its recorded failure first"
            )
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            executable = str((proc / "exe").resolve(strict=True))
        except OSError:
            continue
        if executable in UNITS.values():
            raise SystemExit("A peer process is already running; inspect or stop it first")
    if "mac80211_hwsim" not in str(Path("/sys/class/net/wlan0/phy80211/device").resolve()):
        raise SystemExit("Attach the dedicated hwsim radio before starting this peer")
    # Reject unrelated bridge configuration before changing anything.
    bridge = Path("/sys/class/net/br-lan")
    if bridge.exists():
        if (bridge / "address").read_text().strip() != reference["al_mac"]:
            raise SystemExit("Existing br-lan identity differs from this lab")
        if {p.name for p in (bridge / "brif").iterdir()} != {"em0"}:
            raise SystemExit("Existing bridge has unexpected members")
    else:
        run("ip", "link", "add", "br-lan", "type", "bridge")
        run("ip", "link", "set", "br-lan", "address", reference["al_mac"])
        run("ip", "link", "set", "em0", "master", "br-lan")
    run("ip", "link", "set", "em0", "up")
    run("ip", "link", "set", "br-lan", "up")
    platform = Path("/tmp/beerocks/prplmesh_platform_db")
    platform.parent.mkdir(mode=0o700, exist_ok=True)
    if not platform.exists():
        platform.write_bytes((INSTALL / "share/prplmesh_platform_db").read_bytes())
    configure(
        platform,
        {
            "management_mode": reference["mode"],
            "certification_mode": "0",
            "backhaul_wire_iface": "em0",
        },
    )
    for name in ("beerocks_controller", "beerocks_agent"):
        configure(INSTALL / f"config/{name}.conf", {"ucc_listener_port": "0"})
    Path("/var/run/ubus").mkdir(exist_ok=True)
    # Same multicast forwarding guard used by the upstream launcher, container-local.
    rule = ["FORWARD", "-d", "01:80:c2:00:00:13", "-j", "DROP"]
    if subprocess.run(["ebtables", "-C", *rule], capture_output=True).returncode:
        run("ebtables", "-A", *rule)
    for name, binary in UNITS.items():
        run(*PREFIX, "--unit", f"emosa-peer-{name}", binary)
        if name == "bus":
            deadline = time.monotonic() + 10
            while not Path("/var/run/ubus/ubus.sock").is_socket():
                if time.monotonic() >= deadline:
                    raise SystemExit("ubus startup timed out; inspect emosa-peer-bus")
                time.sleep(0.1)
    print("Peer started; verify API, interface binding and captured frames separately")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop"])
    args = parser.parse_args()
    reference = guard()
    if args.action == "start":
        start(reference)
    else:
        # Only units created by this launcher; no broad process-name kills.
        failures = []
        for name in reversed(UNITS):
            unit = f"emosa-peer-{name}.service"
            loaded = run("systemctl", "show", unit, "-p", "LoadState", "--value").strip()
            if loaded == "not-found":
                continue
            result = subprocess.run(["systemctl", "stop", unit], capture_output=True, text=True)
            outcome = run("systemctl", "show", unit, "-p", "Result", "--value").strip()
            if result.returncode or outcome not in {"", "success"}:
                failures.append({"unit": unit, "result": outcome or "stop_command_failed"})
        if failures:
            print(json.dumps({"shutdown_failures": failures}))
            raise SystemExit(1)


if __name__ == "__main__":
    main()
