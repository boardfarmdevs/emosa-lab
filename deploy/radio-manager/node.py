"""Fixed single-AP hostapd actuator/reader inside the owned hwsim container."""

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
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


def observe(neighbor_label=None):
    ctl = (str(HOSTAP / "bin/hostapd_cli"), "-p", str(ROOT / "ctrl"), "-i", "wlan0")
    status = values(command(*ctl, "status"))
    live = values(command(*ctl, "get_config"))
    raw = command("iw", "dev", "wlan0", "info")
    iface = {}
    for line in raw.splitlines():
        parts = line.lstrip().split(maxsplit=1)
        if len(parts) == 2 and parts[0] in {"addr", "type", "ssid", "channel", "txpower"}:
            if parts[0] == "channel":
                iface[parts[0]] = int(parts[1].split()[0])
            elif parts[0] == "txpower":
                # The hwsim profile has zero antenna/cable gain. Whole measured
                # dBm maps directly to its nominal per-20-MHz EIRP on HT20.
                power = float(parts[1].split()[0])
                if power.is_integer() and 1 <= power <= 20:
                    iface["tx_power_dbm"] = int(power)
            else:
                iface[parts[0]] = parts[1]
    # Private pipe to the independent manager; never printed to the public journal.
    print(
        json.dumps(
            {
                "status": status,
                "config": live,
                "interface": iface,
                "observed_monotonic": time.monotonic(),
                "stations": stations(),
                "forwarding": forwarding(neighbor_label),
            }
        )
    )


def forwarding(neighbor_label=None):
    """Read-only rtnetlink observation. Recheck membership/identities after the dump."""
    try:
        started = time.monotonic_ns()
        links = json.loads(command("ip", "-j", "-d", "-s", "link", "show"))
        after = json.loads(command("ip", "-j", "-d", "link", "show"))
        ended = time.monotonic_ns()
        result = {
            "complete": True,
            "started_ns": started,
            "ended_ns": ended,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "netns_inode": Path("/proc/self/ns/net").stat().st_ino,
            "links": links,
            "links_after": after,
        }
        if neighbor_label is not None:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", neighbor_label):
                raise ValueError("invalid owned neighbor observation label")
            for kind in ("neighbor", "egress"):
                path = ROOT / (kind + "-observations") / (neighbor_label + ".json")
                try:
                    if path.stat().st_size > 262144:
                        raise ValueError("observation exceeds the local budget")
                    result[kind + "_observation"] = json.loads(path.read_text())
                except (OSError, ValueError):
                    result[kind + "_observation"] = None
        return result
    except (OSError, RuntimeError, ValueError):
        return {"complete": False}


def stations():
    """Read the complete authorized station list over hostapd's control socket.

    Missing/partial replies are unknown, never an empty positive inventory.
    The association duration comes from hostapd, not from our first sighting.
    """
    started = int(time.time() * 1000)
    try:
        with (
            tempfile.TemporaryDirectory(prefix="emosa-sta-") as directory,
            socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control,
        ):
            control.settimeout(1)
            control.bind(directory + "/ctrl")
            control.connect(str(ROOT / "ctrl/wlan0"))

            def request(command):
                control.send(command.encode())
                answer = control.recv(65536).decode()
                # This pinned hostap build returns an empty datagram at end of
                # station enumeration. STATUS below independently checks zero.
                if answer and not answer.endswith("\n"):
                    raise ValueError("incomplete hostapd response")
                return answer

            def collect():
                result = []
                raw = request("STA-FIRST")
                while raw not in ("FAIL\n", ""):
                    mac = raw.splitlines()[0]
                    fields = values(raw)
                    if (
                        not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac)
                        or mac in {entry["mac"] for entry in result}
                        or len(result) >= 64
                        or "[ASSOC]" not in fields.get("flags", "")
                        or "[AUTHORIZED]" not in fields.get("flags", "")
                    ):
                        raise ValueError("incomplete station inventory")
                    seconds = int(fields["connected_time"])
                    if not 0 <= seconds <= 4294967:
                        raise ValueError("association age outside telemetry representation")
                    result.append({"mac": mac, "connected_seconds": seconds})
                    raw = request("STA-NEXT " + mac)
                return result

            clients = collect()
            # Detect membership changes across the sequential read.
            if {c["mac"] for c in clients} != {c["mac"] for c in collect()}:
                raise ValueError("station membership changed while reading")
            status = values(request("STATUS"))
            if status.get("state") != "ENABLED" or int(status["num_sta[0]"]) != len(clients):
                raise ValueError("station list and AP status disagree")
        return {"complete": True, "timestamp_ms": started, "clients": clients}
    except (OSError, ValueError, KeyError):
        return {"complete": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "observe", "stop"))
    parser.add_argument("--neighbor-label")
    args = parser.parse_args()
    try:
        guard()
        if args.action == "observe":
            observe(args.neighbor_label)
        else:
            {"apply": apply, "stop": stop}[args.action]()
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
