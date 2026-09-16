import json
import os
import socket
import subprocess
from pathlib import Path

ROOT = Path("/opt/emosa-radio-manager")
AP = "em-baseline-agent"
CLIENTS = ("em-baseline-wired", "em-baseline-wifi")
SERVER = "em-baseline-controller"
NODES = (SERVER, AP, *CLIENTS)
OWNER = "emosa-standard-agent-baseline-v1"


def run(*args, input=None, check=True, timeout=30):
    return subprocess.run(
        args, input=input, capture_output=True, text=True, check=check, timeout=timeout
    ).stdout


def lxc(*args, **kw):
    return run("lxc", "--force-local", "--project", "default", *args, **kw)


def inside(name, *args, **kw):
    return lxc("exec", name, "--", *args, **kw)


def guard():
    if os.geteuid() != 0 or socket.gethostname() != "emosa-lab":
        raise RuntimeError("Run only in the dedicated emosa-lab VM")
    if run("systemd-detect-virt", "--vm").strip() != "kvm":
        raise RuntimeError("Expected the dedicated KVM VM")
    state = json.loads(Path("/opt/emosa-baseline/ownership.json").read_text())
    if state["owner"] != OWNER or not state["setup_complete"]:
        raise RuntimeError("Native baseline ownership evidence absent")
    for name in NODES:
        if lxc("config", "get", name, "user.emosa.baseline").strip() != OWNER:
            raise RuntimeError("Container ownership mismatch")
        inside(name, "test", "!", "-e", "/sys/class/net/eth0")
        active = inside(
            name,
            "systemctl",
            "list-units",
            "--state=active,activating,deactivating",
            "--no-legend",
            "--plain",
            "emosa-baseline-*",
        )
        if active.strip():
            raise RuntimeError("Collect and stop the native baseline first")


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
