"""Optional owned-lab frame loss; never a qualified radio measurement source.

The unmodified pinned wmediumd probability model uses one random choice per
frame, shared by all its retry attempts. A selected frame therefore exhausts
its retry budget. Its time/rate calculations are not qualified for our HT BSS.
"""

import hashlib
import json
import re
import runpy
import socket
import struct
import subprocess
import time
from pathlib import Path

BUILD = Path("/opt/emosa-radio-manager/medium-717e5d7")
COMMIT = "717e5d7fcc23eecbc8e32bd897a8fd4b1e3ba640"
INTERFACE = "em-medium-nl"
PROFILE = """ifaces: {
    ids = ["42:00:00:00:00:00", "02:00:00:ec:02:00", "02:00:00:00:02:00"];
};
model: {
    type = "prob";
    default_prob = 0.0;
    links = ((1, 2, 0.20), (2, 1, 0.0));
};
"""


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=15)


def capture_filter():
    # Resolve the runtime family; generic-netlink IDs are not stable constants.
    decoder = runpy.run_path(str(Path(__file__).with_name("station-events.py")))
    name = b"MAC80211_HWSIM\0"
    attribute = struct.pack("=HH", 4 + len(name), 2) + name
    attribute += b"\0" * (-len(attribute) % 4)
    body = b"\x03\x01\0\0" + attribute
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16) as channel:
        channel.bind((0, 0))
        channel.settimeout(3)
        channel.sendto(struct.pack("=IHHII", 16 + len(body), 16, 1, 1, 0) + body, (0, 0))
        for kind, _flags, seq, _port, reply in decoder["receive"](channel):
            if kind == 16 and seq == 1:
                attrs = decoder["attributes"](reply[4:])
                if attrs.get(2) == name:
                    family = decoder["scalar"](attrs[1], "H")
                    wire = int.from_bytes(struct.pack("=H", family), "big")
                    return (
                        "link[14:2] = 16 and (link[20:2] = "
                        + str(wire)
                        + " or link[20:2] = 4096 or (link[20:2] = 512 and link[40:2] = "
                        + str(wire)
                        + "))"
                    )
    raise RuntimeError("could not resolve hwsim netlink family")


class Medium:
    def __init__(self, directory, seconds):
        self.directory, self.seconds = directory, seconds
        self.unit = "emosa-medium-" + directory.name + ".service"
        self.link = self.started = False

    def prepare(self):
        manifest = json.loads((BUILD / "build.json").read_text())
        binary = BUILD / "wmediumd"
        if (
            manifest["commit"] != COMMIT
            or manifest["patches"] != []
            or hashlib.sha256(binary.read_bytes()).hexdigest() != manifest["binary_sha256"]
        ):
            raise RuntimeError("medium binary does not match the pinned, unmodified build")
        existing = subprocess.run(["pgrep", "-x", "wmediumd"], capture_output=True)
        if existing.returncode != 1:
            raise RuntimeError("another medium process exists, or process enumeration failed")
        # Exclusive creation: never adopt/remove an interface belonging to another run.
        command("ip", "link", "add", INTERFACE, "type", "nlmon")
        self.link = True
        command("ip", "link", "set", INTERFACE, "up")
        (self.directory / "medium-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (self.directory / "medium.cfg").write_text(PROFILE)
        return capture_filter()

    def start(self):
        # Capture is started by the caller before registration. The runtime cap
        # also removes the medium if the outer experiment is killed outright.
        command(
            "systemd-run",
            "--quiet",
            "--unit=" + self.unit,
            "--property=Type=exec",
            "--property=RemainAfterExit=yes",
            "--property=RuntimeMaxSec=" + str(self.seconds + 180),
            "--property=StandardOutput=append:" + str(self.directory / "medium.log"),
            "--property=StandardError=append:" + str(self.directory / "medium.log"),
            str(BUILD / "wmediumd"),
            "-c",
            str(self.directory / "medium.cfg"),
            "-l",
            "7",
        )
        self.started = True
        time.sleep(0.2)
        self.check()

    def check(self):
        if (
            command("systemctl", "show", self.unit, "-p", "SubState", "--value").strip()
            != "running"
        ):
            raise RuntimeError("medium exited; retain its log and netlink capture")
        log = (self.directory / "medium.log").read_text()
        # wmediumd offers broadcast clones to every configured radio, including
        # ones that are idle/off-channel. The kernel can reject those with
        # EINVAL. Retain/count these replies; never count an offered clone as
        # successful reception. Registration/TX-status/other errors are fatal.
        errors = re.findall(r"^nl:.*$", log, re.M)
        if "Unable to find sender" in log or any(
            not re.fullmatch(r"nl: cmd 2, seq \d+: Invalid argument", error) for error in errors
        ):
            raise RuntimeError("medium reported a netlink/source error")

    def stop(self):
        if self.started:
            command("systemctl", "stop", self.unit)
            if command("systemctl", "show", self.unit, "-p", "MainPID", "--value").strip() != "0":
                raise RuntimeError("medium process did not stop")
            self.started = False

    def remove(self):
        if self.link:
            command("ip", "link", "delete", INTERFACE)
            self.link = False
