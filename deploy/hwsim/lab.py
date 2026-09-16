#!/usr/bin/env python3
"""Optional standalone Wi-Fi smoke lab. Never connects to a physical pod."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".lab/hwsim"
STATE = WORK / "state.json"
CONTAINERS = ("em-radio-ap", "em-radio-client")
NETWORK = "em-radio-setup"
PROFILE = "emosa-radio"
SSID = "emosa-hwsim"
UTC = timezone.utc  # noqa: UP017 -- read-only host preflight also supports Python 3.10


def command(argv, *, data=None, timeout=60, check=True):
    return subprocess.run(
        argv, input=data, capture_output=True, text=True, timeout=timeout, check=check
    )


def lxc(*args, **kwargs):
    # Ignore the operator's selected remote/project; resources live in this VM only.
    return command(["lxc", "--force-local", "--project", "default", *args], **kwargs)


def inside(name, *args, **kwargs):
    return lxc("exec", name, "--", *args, **kwargs)


def preflight():
    tools = ("lxc", "iw", "ip", "modprobe", "modinfo", "tcpdump", "systemd-detect-virt")
    missing = [tool for tool in tools if not shutil.which(tool)]
    virtual = (
        command(["systemd-detect-virt", "--vm"], check=False)
        if ("systemd-detect-virt" not in missing)
        else None
    )
    dedicated = (
        socket.gethostname() == "emosa-lab" and virtual is not None and (virtual.returncode == 0)
    )
    module = (
        command(["modinfo", "mac80211_hwsim"], check=False) if ("modinfo" not in missing) else None
    )
    module_available = module is not None and module.returncode == 0
    return {
        "scope": "standalone hwsim AP/station smoke; no EMOSA actuation or physical RF",
        "hostname": socket.gethostname(),
        "dedicated_vm": dedicated,
        "kernel": os.uname().release,
        "missing_vm_tools": missing,
        "hwsim_module_available": module_available,
        "hwsim_already_loaded": Path("/sys/module/mac80211_hwsim").exists(),
        "state_exists": STATE.exists(),
        "ready_for_setup_checks": dedicated and not missing and module_available,
        "runtime_qualified": False,
    }


def require_vm():
    facts = preflight()
    if not facts["dedicated_vm"]:
        raise RuntimeError("Restricted to the dedicated emosa-lab virtual machine")
    if os.geteuid() != 0:
        raise RuntimeError("Run mutation commands as root inside the dedicated VM")
    if facts["missing_vm_tools"]:
        raise RuntimeError(f"Install documented VM prerequisites: {facts['missing_vm_tools']}")
    return facts


def save(state):
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(STATE)


def owned_containers(state):
    for name in state["containers"]:
        found = lxc("config", "get", name, "user.emosa.hwsim").stdout.strip()
        if found != state["owner"]:
            raise RuntimeError(f"Ownership mismatch: {name}; inspect manually")


def push_private(name, filename, content):
    inside(name, "mkdir", "-p", "-m", "700", "/run/emosa-radio")
    lxc("file", "push", "--mode", "0600", "-", f"{name}/run/emosa-radio/{filename}", data=content)


def setup():
    facts = require_vm()
    if not facts["hwsim_module_available"]:
        raise RuntimeError("mac80211_hwsim module unavailable for the running guest kernel")
    if facts["hwsim_already_loaded"] or WORK.exists():
        raise RuntimeError("Existing hwsim module or lab directory; inspect/clean up before setup")
    instances = json.loads(lxc("list", "--format", "json").stdout)
    if any(item["name"] in CONTAINERS for item in instances):
        raise RuntimeError("A target container name already exists; refusing to reuse it")
    for kind, name in (("network", NETWORK), ("profile", PROFILE)):
        if any(x["name"] == name for x in json.loads(lxc(kind, "list", "--format", "json").stdout)):
            raise RuntimeError(f"Existing {kind} {name}; refusing to reuse it")
    # Validate infrastructure before the first side effect.
    lxc("storage", "show", "default")
    image = json.loads((ROOT / "deploy/images.lock.json").read_text())["container_fingerprint"]
    lxc("image", "info", f"ubuntu:{image}")
    WORK.mkdir(parents=True, mode=0o700)
    os.chmod(WORK, 0o700)
    state = {
        "owner": secrets.token_hex(16),
        "containers": [],
        "network": False,
        "profile": False,
        "module": False,
        "setup_complete": False,
        "image": image,
    }
    save(state)
    # Save intent before creation so interrupted setup leaves a reviewable ownership record.
    state["network"] = True
    save(state)
    lxc(
        "network",
        "create",
        NETWORK,
        "ipv4.address=auto",
        "ipv4.nat=true",
        "ipv6.address=none",
        f"user.emosa.hwsim={state['owner']}",
    )
    state["profile"] = True
    save(state)
    lxc("profile", "create", PROFILE)
    profile = {
        "config": {
            "security.privileged": "false",
            "limits.cpu": "1",
            "limits.memory": "512MiB",
            "user.emosa.hwsim": state["owner"],
        },
        "devices": {
            "root": {"type": "disk", "path": "/", "pool": "default"},
            "eth0": {"type": "nic", "name": "eth0", "network": NETWORK},
        },
    }
    lxc("profile", "edit", PROFILE, data=json.dumps(profile))
    for name in CONTAINERS:
        state["containers"].append(name)
        save(state)
        lxc(
            "launch",
            f"ubuntu:{image}",
            name,
            "-p",
            PROFILE,
            "-c",
            f"user.emosa.hwsim={state['owner']}",
            timeout=240,
        )
        inside(name, "cloud-init", "status", "--wait", timeout=180)
        inside(name, "apt-get", "update", timeout=180)
        inside(
            name,
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "iw",
            "iproute2",
            "iputils-ping",
            "hostapd",
            "wpasupplicant",
            timeout=240,
        )
        (WORK / f"{name}-packages.txt").write_text(inside(name, "dpkg-query", "-W").stdout)
        # Ubuntu may start its D-Bus supplicant during package installation.
        # This fresh, owned container uses only the harness's explicit daemons.
        inside(name, "systemctl", "stop", "wpa_supplicant.service", "hostapd.service")
    # All radios belong to this fresh module in this VM; no existing radio is moved.
    command(["modprobe", "mac80211_hwsim", "radios=2"])
    state["module"] = True
    save(state)
    phys = sorted(Path("/sys/class/ieee80211").glob("*"))
    phys = [p.name for p in phys if (p / "device/driver").resolve().name == "mac80211_hwsim"]
    if len(phys) != 2:
        raise RuntimeError("Expected exactly two newly created hwsim PHYs; inspect the lab")
    state["phys"] = phys
    save(state)
    for name, phy in zip(CONTAINERS, phys, strict=True):
        info = json.loads(lxc("list", name, "--format", "json").stdout)
        item = next(x for x in info if x["name"] == name)
        pid = item["state"]["pid"]
        if not isinstance(pid, int) or pid <= 1:
            raise RuntimeError(f"Container is not running: {name}")
        command(["iw", "phy", phy, "set", "netns", str(pid)])
        devices = inside(name, "iw", "dev").stdout
        interfaces = [
            line.split()[1]
            for line in devices.splitlines()
            if line.strip().startswith("Interface ")
        ]
        if len(interfaces) != 1:
            raise RuntimeError(f"Unexpected wireless interface inventory in {name}")
        interface = interfaces[0]
        inside(name, "ip", "link", "set", interface, "down")
        if interface != "wlan0":
            inside(name, "ip", "link", "set", interface, "name", "wlan0")
        # Override the setup NIC with 'none'; traffic checks have only the wireless path.
        lxc("config", "device", "add", name, "eth0", "none")
    key = secrets.token_hex(32)
    push_private(
        CONTAINERS[0],
        "hostapd.conf",
        f"interface=wlan0\ndriver=nl80211\nssid={SSID}\n"
        f"hw_mode=g\nchannel=1\nwpa=2\nwpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\n"
        f"wpa_psk={key}\nctrl_interface=/run/emosa-radio/hostapd\n",
    )
    push_private(
        CONTAINERS[1],
        "station.conf",
        "ctrl_interface=/run/emosa-radio/supplicant\n"
        f'network={{\n ssid="{SSID}"\n psk={key}\n key_mgmt=WPA-PSK\n proto=RSN\n'
        " pairwise=CCMP\n}\n",
    )
    state["setup_complete"] = True
    save(state)
    (WORK / "environment.json").write_text(
        json.dumps(
            {
                **facts,
                "lxd": lxc("version").stdout,
                "container_image": image,
                "module": command(["modinfo", "mac80211_hwsim"]).stdout,
                "vm_packages": command(["dpkg-query", "-W"]).stdout,
                "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "setup_complete": True,
            },
            indent=2,
        )
        + "\n"
    )
    print("Radio lab prepared. Run smoke explicitly; no EMOSA/physical acceptance has occurred.")


def smoke():
    require_vm()
    state = json.loads(STATE.read_text())
    owned_containers(state)
    if not state["setup_complete"]:
        raise RuntimeError("Setup is incomplete; inspect .lab/hwsim before proceeding")
    output = WORK / datetime.now(UTC).strftime("smoke-%Y%m%dT%H%M%S.%fZ")
    output.mkdir(mode=0o700)
    result = {
        "scope": "standalone-hwsim-client-smoke",
        "verdict": "fail",
        "emosa_actuation": "not_evaluated",
        "wire_provisioning": "not_evaluated",
        "physical_pod": "not_evaluated",
        "started_at": datetime.now(UTC).isoformat(),
        "checks": {},
    }
    capture = None
    try:
        # Refuse a second process against an existing control socket.
        for name in CONTAINERS:
            existing = inside(
                name,
                "pgrep",
                "-x",
                "hostapd" if name == CONTAINERS[0] else "wpa_supplicant",
                check=False,
            )
            if existing.returncode == 0:
                raise RuntimeError("A radio daemon is already running; clean up/recreate the lab")
            # Confirm the management Ethernet path was removed before data checks.
            ethernet = inside(name, "ip", "link", "show", "eth0", check=False)
            if ethernet.returncode == 0:
                raise RuntimeError(
                    "Setup Ethernet interface remains; refuse ambiguous traffic check"
                )
        command(["ip", "link", "set", "hwsim0", "up"])
        with (output / "capture.log").open("w") as log:
            capture = subprocess.Popen(
                [
                    "tcpdump",
                    "-Z",
                    "root",
                    "-U",
                    "-i",
                    "hwsim0",
                    "-w",
                    str(output / "wireless.pcap"),
                ],
                stdout=log,
                stderr=log,
            )
        capture_deadline = time.monotonic() + 5
        while "listening on" not in (output / "capture.log").read_text():
            if capture.poll() is not None or time.monotonic() > capture_deadline:
                raise RuntimeError("Virtual-medium capture did not start")
            time.sleep(0.1)
        inside(
            CONTAINERS[0],
            "hostapd",
            "-B",
            "-f",
            "/run/emosa-radio/hostapd.log",
            "/run/emosa-radio/hostapd.conf",
        )
        inside(CONTAINERS[0], "ip", "addr", "add", "192.0.2.1/30", "dev", "wlan0")
        inside(
            CONTAINERS[1],
            "wpa_supplicant",
            "-B",
            "-Dnl80211",
            "-iwlan0",
            "-c/run/emosa-radio/station.conf",
            "-f/run/emosa-radio/station.log",
        )
        inside(CONTAINERS[1], "ip", "addr", "add", "192.0.2.2/30", "dev", "wlan0")
        deadline = time.monotonic() + 30
        status = ""
        while time.monotonic() < deadline:
            status = inside(
                CONTAINERS[1],
                "wpa_cli",
                "-p/run/emosa-radio/supplicant",
                "-iwlan0",
                "status",
                check=False,
                timeout=5,
            ).stdout
            if "wpa_state=COMPLETED" in status:
                break
            time.sleep(0.3)
        (output / "station-status.txt").write_text(status)
        values = dict(line.split("=", 1) for line in status.splitlines() if "=" in line)
        associated = values.get("wpa_state") == "COMPLETED" and values.get("ssid") == SSID
        result["checks"]["wpa2_association"] = associated and values.get("key_mgmt") == "WPA2-PSK"
        connected = inside(CONTAINERS[1], "iw", "dev", "wlan0", "link").stdout
        (output / "station-link.txt").write_text(connected)
        ping = inside(
            CONTAINERS[1], "ping", "-I", "wlan0", "-c", "3", "-W", "2", "192.0.2.1", check=False
        )
        (output / "station-ping.txt").write_text(ping.stdout + ping.stderr)
        result["checks"]["wireless_ping"] = ping.returncode == 0
        result["checks"]["capture_running"] = capture.poll() is None
        if all(result["checks"].values()):
            result["verdict"] = "pass"
    except Exception as error:
        # Do not serialize subprocess arguments/input: config content is private.
        result["error_type"] = type(error).__name__
        if isinstance(error, RuntimeError):
            result["reason"] = str(error)
        raise
    finally:
        if capture is not None and capture.poll() is None:
            capture.terminate()
            capture.wait(timeout=10)
        capture_path = output / "wireless.pcap"
        result["checks"]["capture_has_frames"] = (
            capture_path.exists() and capture_path.stat().st_size > 24
        )
        if not result["checks"]["capture_has_frames"]:
            result["verdict"] = "fail"
        for name, logfile in zip(CONTAINERS, ("hostapd.log", "station.log"), strict=True):
            log = inside(name, "cat", f"/run/emosa-radio/{logfile}", check=False)
            (output / logfile).write_text(log.stdout)
        result["finished_at"] = datetime.now(UTC).isoformat()
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        artifacts = [
            {"path": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(output.iterdir())
            if p.is_file()
        ]
        (output / "manifest.json").write_text(json.dumps(artifacts, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == "pass" else 1


def cleanup():
    require_vm()
    state = json.loads(STATE.read_text())
    owned_containers(state)
    # Check every resource before deleting any. Never delete an unexpected owner.
    if state["network"] and (
        lxc("network", "get", NETWORK, "user.emosa.hwsim").stdout.strip() != state["owner"]
    ):
        raise RuntimeError("Network ownership mismatch; inspect manually")
    if state["profile"] and (
        lxc("profile", "get", PROFILE, "user.emosa.hwsim").stdout.strip() != state["owner"]
    ):
        raise RuntimeError("Profile ownership mismatch; inspect manually")
    for name in state["containers"]:
        lxc("delete", name, "--force")
    if state["profile"]:
        lxc("profile", "delete", PROFILE)
    if state["network"]:
        lxc("network", "delete", NETWORK)
    if state["module"]:
        command(["modprobe", "-r", "mac80211_hwsim"])
    state["cleaned_up_at"] = datetime.now(UTC).isoformat()
    state["setup_complete"] = False
    save(state)
    print("Owned radio resources removed. Local evidence is retained in .lab/hwsim.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "setup", "smoke", "cleanup"))
    args = parser.parse_args()
    try:
        if args.action == "check":
            facts = preflight()
            print(json.dumps(facts, indent=2))
            return 0 if facts["ready_for_setup_checks"] else 1
        return {"setup": setup, "smoke": smoke, "cleanup": cleanup}[args.action]() or 0
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        # No secret-bearing command/input is printed on failure.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(
            f"Radio lab stopped: {message}. Inspect private .lab/hwsim evidence.", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
