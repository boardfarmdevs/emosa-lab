"""Attach one hwsim radio to the candidate peer; run only in the dedicated VM."""

import json
import os
import socket
import subprocess
from pathlib import Path


def main():
    if os.geteuid() != 0 or socket.gethostname() != "emosa-lab":
        raise SystemExit("Run as root inside the dedicated emosa-lab VM")
    if subprocess.check_output(["systemd-detect-virt", "--vm"], text=True).strip() != "kvm":
        raise SystemExit("Expected the qualified KVM guest")
    if Path("/sys/module/mac80211_hwsim").exists():
        raise SystemExit("hwsim already exists; inspect its owner before changing any radios")
    instances = json.loads(
        subprocess.check_output(["lxc", "list", "em-controller", "--format=json"])
    )
    if len(instances) != 1 or instances[0]["state"]["status"] != "Running":
        raise SystemExit("Expected exactly one running em-controller container")
    instance = instances[0]
    pid = instance["state"]["pid"]
    if not isinstance(pid, int) or pid <= 1 or instance["type"] != "container":
        raise SystemExit("Invalid container namespace")
    existing = subprocess.run(
        ["lxc", "exec", "em-controller", "--", "test", "-e", "/sys/class/net/wlan0"]
    )
    if existing.returncode != 1:
        raise SystemExit("Cannot establish that wlan0 is absent; inspect the container")
    subprocess.run(["modprobe", "mac80211_hwsim", "radios=1"], check=True)
    radios = list(Path("/sys/class/ieee80211").iterdir())
    if len(radios) != 1 or "mac80211_hwsim" not in str((radios[0] / "device").resolve()):
        raise SystemExit("Unexpected radio inventory; leave it intact for inspection")
    subprocess.run(["iw", "phy", radios[0].name, "set", "netns", str(pid)], check=True)
    print(f"Attached {radios[0].name} to em-controller; no AP or wireless client started")


if __name__ == "__main__":
    main()
