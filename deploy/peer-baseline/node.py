"""Configure/run native peer processes in the two owned baseline containers."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tarfile
import time
from pathlib import Path

INSTALL = Path("/opt/prpl-install-nl80211")
ROOT = Path("/opt/emosa-baseline")
HOSTAP = ROOT / "hostap"
NAMES = {"em-baseline-controller": 1, "em-baseline-agent": 2}
FRONTHAUL = "emosa-baseline-provisioned"
BACKHAUL = "emosa-baseline-backhaul"
# Deliberately public synthetic credentials; never use for an actual pod.
KEY = "EmosaBaseline2026!"
INITIAL_KEY = "UnprovisionedBaseline2026!"
UNITS = ("hostap", "supplicant", "bus", "transport", "controller", "agent")


def run(*args, timeout=30, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)
    return result.stdout


def guard():
    name = socket.gethostname()
    if os.geteuid() != 0 or name not in NAMES:
        raise SystemExit("Run only inside an owned baseline peer container")
    if run("systemd-detect-virt", "--container").strip() != "lxc":
        raise SystemExit("Expected an unprivileged LXD container")
    reference = json.loads((ROOT / "prplmesh.reference.json").read_text())
    for binary, expected in reference["binaries"].items():
        if hashlib.sha256((INSTALL / "bin" / binary).read_bytes()).hexdigest() != expected:
            raise SystemExit("Native binary differs from pinned peer")
    libraries = reference.get("runtime_libraries", {})
    if libraries and set(libraries) != {"libbpl.so.6.0.0"}:
        raise SystemExit("Unknown experimental native runtime library")
    for name, expected in libraries.items():
        if hashlib.sha256((INSTALL / "lib" / name).read_bytes()).hexdigest() != expected:
            raise SystemExit("Native runtime library differs from recorded candidate")
    baseline = json.loads((ROOT / "reference.json").read_text())
    expected = baseline["native_hal_overlay"]["library_sha256"]
    if hashlib.sha256((INSTALL / "lib/libbwl.so.6.0.0").read_bytes()).hexdigest() != expected:
        raise SystemExit("Native HAL differs from the recorded overlay")
    for binary, expected in baseline["hostap_runtime"]["binaries"].items():
        if hashlib.sha256((HOSTAP / binary).read_bytes()).hexdigest() != expected:
            raise SystemExit("Hostap runtime differs from the recorded build")
    return NAMES[name]


def replace_config(path, values):
    lines = path.read_text().splitlines()
    for key, value in values.items():
        if sum(line.startswith(key + "=") for line in lines) != 1:
            raise SystemExit(f"Expected exactly one config field: {key}")
        lines = [f"{key}={value}" if line.startswith(key + "=") else line for line in lines]
    path.write_text("\n".join(lines) + "\n")


def active(unit):
    return run(
        "systemctl", "show", f"emosa-baseline-{unit}.service", "-p", "ActiveState", "--value"
    ).strip() in {"active", "activating", "deactivating"}


def prepare(ordinal, backhaul, policy_name="front-and-backhaul"):
    if any(active(unit) for unit in UNITS):
        raise SystemExit("Stop and collect the previous peer processes before preparing")
    if Path("/tmp/beerocks/logs").exists():
        raise SystemExit("Archive previous native logs before a clean preparation")
    for name in (
        "config/beerocks_agent.conf",
        "config/beerocks_controller.conf",
        "share/prplmesh_platform_db",
    ):
        with tarfile.open("/opt/prpl-install-nl80211-6.0.0.tar.gz") as archive:
            (INSTALL / name).write_bytes(archive.extractfile(f"prpl-install-nl80211/{name}").read())
    for path in (
        Path("/tmp/beerocks"),
        Path("/var/run/hostapd"),
        Path("/var/run/wpa_supplicant"),
        Path("/var/run/ubus"),
    ):
        path.mkdir(parents=True, exist_ok=True)
    platform = Path("/tmp/beerocks/prplmesh_platform_db")
    platform.write_bytes((INSTALL / "share/prplmesh_platform_db").read_bytes())
    mode = "Multi-AP-Controller-and-Agent" if ordinal == 1 else "Multi-AP-Agent"
    replace_config(
        platform,
        {"management_mode": mode, "certification_mode": "0", "backhaul_wire_iface": "eth1"},
    )
    for name in ("beerocks_agent", "beerocks_controller"):
        replace_config(INSTALL / f"config/{name}.conf", {"ucc_listener_port": "0"})
    if Path("/sys/class/net/eth0").exists():
        raise SystemExit("Remove setup Ethernet before starting the baseline")
    if not Path("/sys/class/net/br-lan").exists():
        run("ip", "link", "add", "br-lan", "type", "bridge")
    run("ip", "link", "set", "br-lan", "address", f"02:00:00:e0:00:{ordinal:02x}")
    for iface in ("eth1", "eth2"):
        if Path(f"/sys/class/net/{iface}").exists():
            if ordinal == 2 and iface == "eth1" and backhaul == "wireless":
                raise SystemExit("Wireless agent still has an Ethernet backhaul device")
            run("ip", "address", "flush", "dev", iface)
            run("ip", "link", "set", iface, "master", "br-lan")
            run("ip", "link", "set", iface, "up")
    run("ip", "link", "set", "br-lan", "up")
    run("ip", "address", "replace", f"192.0.2.{ordinal}/24", "dev", "br-lan")
    if "mac80211_hwsim" not in str(Path("/sys/class/net/wlan0/phy80211/device").resolve()):
        raise SystemExit("Expected an owned hwsim PHY")
    run("ip", "link", "set", "wlan0", "down")
    run("ip", "link", "set", "wlan0", "address", f"02:00:00:ec:{ordinal:02x}:00")
    config = f"""driver=nl80211
hw_mode=g
channel=6
ieee80211n=1
vht_oper_chwidth=0
vht_oper_centr_freq_seg0_idx=6
interface=wlan0
ctrl_interface=/var/run/hostapd
bridge=br-lan
bssid=02:00:00:ec:{ordinal:02x}:00
ssid=emosa-unprovisioned-{ordinal}
wpa=2
wpa_key_mgmt=WPA-PSK
wpa_passphrase={INITIAL_KEY}
rsn_pairwise=CCMP
multi_ap=2
wps_state=2
eap_server=1
config_methods=push_button
device_name=EMOSA-baseline-{ordinal}
device_type=6-0050F204-1
manufacturer=EMOSA-Lab
model_name=prplMesh-hwsim-baseline
model_number=1
serial_number=baseline-{ordinal}
os_version=01020300
ap_setup_locked=0
uuid=00000000-0000-4000-8000-00000000000{ordinal}
bss=wlan0.0
ctrl_interface=/var/run/hostapd
bridge=br-lan
bssid=02:00:00:ec:{ordinal:02x}:01
ssid=emosa-unprovisioned-backhaul-{ordinal}
wpa=2
wpa_key_mgmt=WPA-PSK
wpa_passphrase={INITIAL_KEY}
rsn_pairwise=CCMP
multi_ap=1
"""
    if ordinal == 2 and policy_name == "sole-fronthaul":
        config = config.split("bss=wlan0.0\n", 1)[0]
    Path("/var/run/hostapd-phy0.conf").write_text(config)
    (ROOT / "initial-hostapd.conf").write_text(config)
    if ordinal == 2 and backhaul == "wireless":
        if not Path("/sys/class/net/wlan1").exists():
            run("iw", "dev", "wlan0", "interface", "add", "wlan1", "type", "station")
        run("ip", "link", "set", "wlan1", "down")
        run("ip", "link", "set", "wlan1", "address", "02:00:00:ec:02:10")
        # Empty configuration: no preinstalled network or backhaul credentials.
        (ROOT / "backhaul.conf").write_text(
            "ctrl_interface=/var/run/wpa_supplicant\nupdate_config=1\n"
        )
    elif ordinal == 2 and Path("/sys/class/net/wlan1").exists():
        # A prior wireless test must not leave a usable station shortcut in wired mode.
        run("iw", "dev", "wlan1", "del")
    rule = ["FORWARD", "-d", "01:80:c2:00:00:13", "-j", "DROP"]
    if subprocess.run(["ebtables", "-C", *rule], capture_output=True).returncode:
        run("ebtables", "-A", *rule)
    print(
        json.dumps(
            {
                "prepared": mode,
                "backhaul": backhaul,
                "initial_ssid": f"emosa-unprovisioned-{ordinal}",
            }
        )
    )


def launch(unit, *command):
    if active(unit):
        raise SystemExit(f"Existing process: {unit}")
    loaded = run(
        "systemctl", "show", f"emosa-baseline-{unit}.service", "-p", "LoadState", "--value"
    ).strip()
    if loaded != "not-found":
        raise SystemExit(f"Record/reset previous unit result before launching: {unit}")
    run(
        "systemd-run",
        "--quiet",
        "--property=Type=exec",
        "--property=TimeoutStopSec=20",
        "--unit",
        f"emosa-baseline-{unit}",
        *command,
    )


def start(ordinal, action):
    if action == "hostap":
        launch(
            "hostap",
            str(HOSTAP / "sbin/hostapd"),
            "-g",
            "/var/run/hostapd/global",
            "-f",
            str(ROOT / "hostapd.log"),
            "/var/run/hostapd-phy0.conf",
        )
    elif action == "supplicant":
        if ordinal != 2:
            raise SystemExit("Backhaul supplicant belongs on the agent")
        launch(
            "supplicant",
            str(HOSTAP / "sbin/wpa_supplicant"),
            "-Dnl80211",
            "-i",
            "wlan1",
            "-b",
            "br-lan",
            "-c",
            str(ROOT / "backhaul.conf"),
            "-f",
            str(ROOT / "backhaul.log"),
        )
    elif action == "services":
        launch("bus", "/usr/sbin/ubusd")
        deadline = time.monotonic() + 10
        while not Path("/var/run/ubus/ubus.sock").is_socket():
            if time.monotonic() > deadline:
                raise SystemExit("ubus socket did not appear")
            time.sleep(0.1)
        launch("transport", str(INSTALL / "bin/ieee1905_transport"))
        if ordinal == 1:
            launch("controller", str(INSTALL / "bin/beerocks_controller"))
    elif action == "agent":
        launch("agent", str(INSTALL / "bin/beerocks_agent"))
    elif action == "controller":
        if ordinal != 1:
            raise SystemExit("The controller belongs on the controller node")
        launch("controller", str(INSTALL / "bin/beerocks_controller"))


def policy(ordinal, policy_name="front-and-backhaul"):
    if ordinal != 1:
        raise SystemExit("BSS policy belongs on the controller")

    def bml(*args):
        output = run(str(INSTALL / "bin/beerocks_cli"), "-c", " ".join(args))
        if "return value is: BML_RET_OK, Success status" not in output:
            raise SystemExit(f"Native BML operation failed: {output}")
        return output

    for target in (1, 2):
        al = f"02:00:00:e0:00:{target:02x}"
        bml("bml_clear_wifi_credentials", al)
        bml("bml_set_wifi_credentials", al, FRONTHAUL, KEY, "24g-5g", "fronthaul", "0")
        if target == 1 or policy_name != "sole-fronthaul":
            bml("bml_set_wifi_credentials", al, BACKHAUL, KEY, "24g-5g", "backhaul", "0")
    bml("bml_update_wifi_credentials")
    print("Controller BSS policy submitted; verify actual M2 and agent state independently")


def stop():
    outcomes = []
    for unit in reversed(UNITS):
        name = f"emosa-baseline-{unit}.service"
        if run("systemctl", "show", name, "-p", "LoadState", "--value").strip() == "not-found":
            continue
        run("systemctl", "stop", name, timeout=35)
        result = run("systemctl", "show", name, "-p", "Result", "-p", "ExecMainStatus")
        outcomes.append({"unit": name, "result": result})
    print(json.dumps(outcomes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "hostap",
            "services",
            "policy",
            "agent",
            "controller",
            "supplicant",
            "stop",
        ),
    )
    parser.add_argument("--backhaul", choices=("wired", "wireless"), default="wired")
    parser.add_argument(
        "--policy", choices=("front-and-backhaul", "sole-fronthaul"), default="front-and-backhaul"
    )
    args = parser.parse_args()
    if args.policy == "sole-fronthaul" and args.backhaul != "wired":
        parser.error("The sole-fronthaul experiment requires wired management")
    ordinal = guard()
    if args.action == "prepare":
        prepare(ordinal, args.backhaul, args.policy)
    elif args.action == "policy":
        policy(ordinal, args.policy)
    elif args.action == "stop":
        stop()
    else:
        start(ordinal, args.action)


if __name__ == "__main__":
    main()
