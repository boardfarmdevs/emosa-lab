"""Fixed single-AP hostapd actuator/reader inside the owned hwsim container."""

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/opt/emosa-radio-manager")
HOSTAP = Path("/opt/emosa-baseline/hostap")
UNIT = "emosa-radio-manager-ap.service"


def command(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise RuntimeError("radio helper command failed")
    return result.stdout


def guard():
    if os.geteuid() != 0 or socket.gethostname() != "em-baseline-agent":
        raise RuntimeError("Expected the owned hwsim AP container")
    if command("systemd-detect-virt", "--container").strip() != "lxc":
        raise RuntimeError("Expected an LXD container")
    driver = Path("/sys/class/net/wlan0/device/driver").resolve().name
    if driver != "mac80211_hwsim" or Path("/sys/class/net/eth0").exists():
        raise RuntimeError("Expected isolated hwsim radio")
    for unit in ("hostap", "supplicant", "agent", "controller", "transport", "bus"):
        state = command(
            "systemctl", "show", f"emosa-baseline-{unit}.service", "-p", "ActiveState", "--value"
        ).strip()
        if state in {"active", "activating", "deactivating"}:
            raise RuntimeError("Native baseline still active")
    reference = json.loads((ROOT / "peer-reference.json").read_text())
    for name in ("sbin/hostapd", "bin/hostapd_cli"):
        actual = hashlib.sha256((HOSTAP / name).read_bytes()).hexdigest()
        if actual != reference["hostap_runtime"]["binaries"][name]:
            raise RuntimeError("Unqualified hostap binary")


def values(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def stop():
    loaded = command("systemctl", "show", UNIT, "-p", "LoadState", "--value").strip()
    if loaded != "not-found":
        command("systemctl", "stop", UNIT)
    command("systemctl", "reset-failed", UNIT, check=False)


def apply():
    desired = json.load(sys.stdin)
    ssid, key = desired["ssid"], desired["passphrase"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}", ssid):
        raise ValueError("unsupported lab SSID")
    if not 8 <= len(key) <= 63 or any(not 32 <= ord(c) <= 126 for c in key):
        raise ValueError("unsupported lab key")
    # Full process reload; no dependence on the native HAL's UPDATE extension.
    stop()
    config = f"""driver=nl80211
interface=wlan0
bridge=br-lan
ctrl_interface={ROOT}/ctrl
bssid=02:00:00:ec:02:00
ssid2={ssid.encode().hex()}
hw_mode=g
channel=6
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase={key}
wps_state=2
eap_server=1
config_methods=push_button
"""
    path = ROOT / "hostapd.conf"
    path.write_text(config)
    path.chmod(0o600)
    command(
        "systemd-run",
        "--quiet",
        "--property=Type=exec",
        "--unit",
        UNIT,
        str(HOSTAP / "sbin/hostapd"),
        "-f",
        str(ROOT / "hostapd.log"),
        str(path),
    )
    print(json.dumps({"action": "started"}))


def observe():
    ctl = (str(HOSTAP / "bin/hostapd_cli"), "-p", str(ROOT / "ctrl"), "-i", "wlan0")
    status = values(command(*ctl, "status"))
    live = values(command(*ctl, "get_config"))
    raw = command("iw", "dev", "wlan0", "info")
    iface = {}
    for line in raw.splitlines():
        parts = line.lstrip().split(maxsplit=1)
        if len(parts) == 2 and parts[0] in {"addr", "type", "ssid", "channel"}:
            iface[parts[0]] = int(parts[1].split()[0]) if parts[0] == "channel" else parts[1]
    # Private pipe to the independent manager; never printed to the public journal.
    print(
        json.dumps(
            {
                "status": status,
                "config": live,
                "interface": iface,
                "observed_monotonic": time.monotonic(),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "observe", "stop"))
    args = parser.parse_args()
    try:
        guard()
        {"apply": apply, "observe": observe, "stop": stop}[args.action]()
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
