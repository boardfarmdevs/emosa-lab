"""Create the isolated existing-peer baseline inside the owned emosa-lab VM."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path

NODES = ("em-baseline-controller", "em-baseline-agent", "em-baseline-wired", "em-baseline-wifi")
OWNER = "emosa-standard-agent-baseline-v1"
IMAGE = "cc6b6e557372769f5b0d0945d61f200360818a408f9d0dddadbbab584d2fb414"
ARTIFACTS = json.loads(Path(__file__).with_name("reference.json").read_text())["archives"]
ROOT = Path("/opt/emosa-baseline")


def run(*args, input=None, timeout=120):
    return subprocess.run(
        args, input=input, text=True, capture_output=True, check=True, timeout=timeout
    ).stdout


def lxc(*args, **kwargs):
    return run("lxc", "--force-local", "--project", "default", *args, **kwargs)


def inside(node, *args, **kwargs):
    return lxc("exec", node, "--", *args, **kwargs)


def finish_setup(state):
    if state["owner"] != OWNER or state["setup_complete"] or not state["hwsim_loaded"]:
        raise SystemExit("Not an owned pending radio assignment")
    for node in NODES:
        if lxc("config", "get", node, "user.emosa.baseline").strip() != OWNER:
            raise SystemExit("Container ownership mismatch")
        if not (ROOT / f"{node}-packages.txt").is_file():
            raise SystemExit("Package installation is incomplete")
    phys = sorted(Path("/sys/class/ieee80211").glob("phy*"), key=lambda p: int(p.name[3:]))
    if len(phys) != 3:
        raise SystemExit("Expected all three unassigned radios; inspect partial assignment")
    for node, phy in zip((NODES[0], NODES[1], NODES[3]), phys, strict=True):
        entries = json.loads(lxc("list", node, "--format", "json"))
        info = next(item for item in entries if item["name"] == node)
        pid = info["state"]["pid"]
        if not isinstance(pid, int) or pid <= 1:
            raise SystemExit("Container is not running")
        run("iw", "phy", phy.name, "set", "netns", str(pid))
        interfaces = inside(node, "iw", "dev")
        iface = next(
            line.split()[1]
            for line in interfaces.splitlines()
            if line.strip().startswith("Interface ")
        )
        if iface != "wlan0":
            inside(node, "ip", "link", "set", iface, "name", "wlan0")
    for node in NODES:
        lxc("config", "device", "add", node, "eth0", "none")
    state["setup_complete"] = True
    (ROOT / "ownership.json").write_text(json.dumps(state, indent=2) + "\n")
    print("Baseline containers prepared; no onboarding verdict yet", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finish-setup", action="store_true")
    args = parser.parse_args()
    if socket.gethostname() != "emosa-lab" or os.geteuid() != 0:
        raise SystemExit("Run only as root in the dedicated emosa-lab VM")
    if run("systemd-detect-virt", "--vm").strip() != "kvm":
        raise SystemExit("Expected the dedicated KVM guest")
    if args.finish_setup:
        finish_setup(json.loads((ROOT / "ownership.json").read_text()))
        return
    if Path("/sys/module/mac80211_hwsim").exists():
        raise SystemExit("An hwsim owner already exists; inspect before setup")
    for name, expected in ARTIFACTS.items():
        archive = Path("/opt/peer-artifacts") / name
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise SystemExit(f"Archive digest mismatch: {name}")
    existing = {item["name"] for item in json.loads(lxc("list", "--format", "json"))}
    if existing.intersection(NODES) or (ROOT / "ownership.json").exists():
        raise SystemExit("Baseline resources already exist; inspect their state")
    networks = {item["name"] for item in json.loads(lxc("network", "list", "--format", "json"))}
    if networks.intersection({"em-base-bh", "em-base-lan"}):
        raise SystemExit("Baseline network names already exist")
    profiles = {item["name"] for item in json.loads(lxc("profile", "list", "--format", "json"))}
    if "em-baseline" in profiles:
        raise SystemExit("Baseline profile already exists")
    ROOT.mkdir(exist_ok=True)
    state = {
        "owner": OWNER,
        "nodes": [],
        "networks": [],
        "profile": False,
        "image": IMAGE,
        "setup_complete": False,
        "hwsim_loaded": False,
    }

    def save():
        (ROOT / "ownership.json").write_text(json.dumps(state, indent=2) + "\n")

    save()
    for network in ("em-base-bh", "em-base-lan"):
        lxc(
            "network",
            "create",
            network,
            "ipv4.address=none",
            "ipv6.address=none",
            f"user.emosa.baseline={OWNER}",
        )
        state["networks"].append(network)
        save()
    lxc("profile", "create", "em-baseline")
    state["profile"] = True
    save()
    profile = {
        "config": {
            "limits.cpu": "1",
            "limits.memory": "384MiB",
            "security.privileged": "false",
            "user.emosa.baseline": OWNER,
        },
        "devices": {
            "root": {"type": "disk", "path": "/", "pool": "default"},
            "eth0": {"type": "nic", "network": "em-mgmt", "name": "eth0"},
        },
    }
    lxc("profile", "edit", "em-baseline", input=json.dumps(profile))
    for node in NODES:
        lxc(
            "init",
            IMAGE,
            node,
            "--profile",
            "em-baseline",
            "--config",
            f"user.emosa.baseline={OWNER}",
        )
        state["nodes"].append(node)
        save()
        lxc("start", node)
        print(f"Created {node}", flush=True)
    for node in NODES[:2]:
        lxc("config", "device", "add", node, "backhaul", "nic", "network=em-base-bh", "name=eth1")
    lxc("config", "device", "add", NODES[1], "lan", "nic", "network=em-base-lan", "name=eth2")
    lxc("config", "device", "add", NODES[2], "lan", "nic", "network=em-base-lan", "name=eth1")
    packages = (
        "iproute2 iputils-ping iw tcpdump ebtables libcap-ng0 libevent-2.1-7 "
        "libjson-c5 libnl-3-200 libnl-genl-3-200 libnl-route-3-200 "
        "libssl3 liburiparser1 libyajl2 psmisc"
    )
    for node in NODES:
        inside(node, "systemctl", "mask", "--now", "apt-daily.timer", "apt-daily-upgrade.timer")
        log = ROOT / f"{node}-packages.log"
        command = (
            "export DEBIAN_FRONTEND=noninteractive\n"
            "apt-get update\napt-get install -y " + packages + "\n"
        )
        result = inside(node, "sh", "-ec", command, timeout=600)
        log.write_text(result)
        inside(
            node,
            "mkdir",
            "-p",
            "/opt/emosa-baseline",
            "/var/run/hostapd",
            "/var/run/wpa_supplicant",
            "/var/run/ubus",
        )
        for name in ARTIFACTS:
            if node not in NODES[:2] and not name.startswith("hostap-runtime"):
                continue
            lxc("file", "push", "--quiet", f"/opt/peer-artifacts/{name}", f"{node}/opt/")
            destination = (
                "/usr/local"
                if name.startswith("hostap-runtime")
                else ("/opt" if name.startswith("prpl-install") else "/")
            )
            inside(node, "tar", "-C", destination, "-xzf", f"/opt/{name}")
        inside(node, "ldconfig")
        inventory = inside(node, "dpkg-query", "-W", "-f=${Package}\t${Version}\n")
        (ROOT / f"{node}-packages.txt").write_text(inventory)
        print(f"Installed {node}", flush=True)
    run("modprobe", "mac80211_hwsim", "radios=3")
    state["hwsim_loaded"] = True
    save()
    finish_setup(state)


if __name__ == "__main__":
    main()
