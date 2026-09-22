"""Restore missing hwsim radios after a reboot of the already owned lab VM."""

import argparse
import fcntl
import json
from pathlib import Path

from common import AP, CLIENTS, NODES, OWNER, ROOT, SERVER, guard, inside, lxc, run, write
from run import require_idle


def preflight():
    guard()
    require_idle()
    if Path("/sys/module/mac80211_hwsim").exists():
        raise RuntimeError("hwsim is already loaded; inspect existing ownership and assignment")
    if list(Path("/sys/class/ieee80211").glob("*")):
        raise RuntimeError("Unexpected VM radio inventory")
    ownership = json.loads(Path("/opt/emosa-baseline/ownership.json").read_text())
    records = {x["name"]: x for x in json.loads(lxc("list", "--format", "json"))}
    for network in ("em-base-bh", "em-base-lan"):
        if lxc("network", "get", network, "user.emosa.baseline").strip() != OWNER:
            raise RuntimeError("Unexpected network owner")
    for node in NODES:
        info = records[node]
        if info["status"] != "Running" or info["state"]["pid"] <= 1:
            raise RuntimeError("Owned containers must already be running")
        if info["expanded_config"].get("volatile.base_image") != ownership["image"]:
            raise RuntimeError("Container image differs from retained setup")
        if inside(node, "iw", "dev").strip():
            raise RuntimeError("Container already has radio interfaces")
        for device in info["expanded_devices"].values():
            if device.get("type") == "nic" and device.get("network") not in {
                "em-base-bh",
                "em-base-lan",
            }:
                raise RuntimeError("Unexpected container network")
    return {
        "owner": OWNER,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "kernel": run("uname", "-r").strip(),
        "module": run("modinfo", "-n", "mac80211_hwsim").strip(),
        "container_pids": {node: records[node]["state"]["pid"] for node in NODES},
        "status": "preflight_passed",
        "historical_ownership_rewritten": False,
    }


def restore():
    report = preflight()
    path = ROOT / ("restore-" + report["boot_id"] + ".json")
    if path.exists():
        raise RuntimeError("A restoration record exists for this boot; inspect it first")
    write(path, report)
    try:
        run("modprobe", "mac80211_hwsim", "radios=3")
        phys = sorted(Path("/sys/class/ieee80211").glob("phy*"), key=lambda p: int(p.name[3:]))
        if len(phys) != 3:
            raise RuntimeError("Expected exactly three new hwsim PHYs")
        report["assignments"] = {}
        for node, phy in zip((SERVER, AP, CLIENTS[1]), phys, strict=True):
            run("iw", "phy", phy.name, "set", "netns", str(report["container_pids"][node]))
            interfaces = [
                line.split()[1]
                for line in inside(node, "iw", "dev").splitlines()
                if line.strip().startswith("Interface ")
            ]
            if len(interfaces) != 1:
                raise RuntimeError("Unexpected assigned interface set; inspect partial restoration")
            if interfaces[0] != "wlan0":
                inside(node, "ip", "link", "set", interfaces[0], "name", "wlan0")
            driver = inside(node, "readlink", "-f", "/sys/class/net/wlan0/device/driver").strip()
            if not driver.endswith("/mac80211_hwsim"):
                raise RuntimeError("Assigned interface is not hwsim")
            report["assignments"][node] = {"phy": phy.name, "interface": "wlan0", "driver": driver}
            write(path, report)
        for node in (SERVER, AP):
            links = json.loads(inside(node, "ip", "-j", "-d", "link"))
            bridge = next((x for x in links if x["ifname"] == "br-lan"), None)
            if bridge is None:
                inside(node, "ip", "link", "add", "br-lan", "type", "bridge")
            elif bridge.get("linkinfo", {}).get("info_kind") != "bridge":
                raise RuntimeError("br-lan exists but is not a bridge")
        report["status"] = "restored"
    except BaseException as exc:
        report.update(status="failed_inspect_partial_state", error=type(exc).__name__)
        raise
    finally:
        write(path, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="restore after successful preflight")
    args = parser.parse_args()
    guard()
    ROOT.mkdir(exist_ok=True, mode=0o700)
    with (ROOT / "run.lock").open("a+") as lock, (ROOT / "manager.lock").open("a+") as manager:
        for handle in (lock, manager):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.apply:
            restore()
        else:
            print(json.dumps(preflight(), indent=2))
