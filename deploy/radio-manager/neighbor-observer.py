"""Read-only discovery capture on the owned simulated pod's actual backhaul.

Only packet sockets, multicast memberships and local evidence files are used.
This collector does not transmit, configure an interface, or interpret authority.
It retains bounded raw topology-discovery/LLDP frames for the OVSDB publisher.
"""

import argparse
import hashlib
import json
import os
import re
import runpy
import select
import signal
import socket
import struct
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

ROOT = Path("/opt/emosa-radio-manager")


def wanted(frame):
    return (len(frame) >= 14 and frame[12:14] == b"\x88\xcc") or (
        len(frame) >= 22 and frame[12:14] == b"\x89\x3a" and frame[16:18] == b"\0\0"
    )


def observe(label, seconds):
    runpy.run_path(str(ROOT / "node.py"))["guard"]()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", label) or not 30 <= seconds <= 86400:
        raise ValueError("invalid owned observation scope")
    os.umask(0o077)
    directory = ROOT / "neighbor-observations"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (label + ".json")
    path.open("x").close()
    index = socket.if_nametoindex("eth1")
    identity = {
        "interface": "eth1",
        "ifindex": index,
        "mac": Path("/sys/class/net/eth1/address").read_text().strip(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "netns_inode": Path("/proc/self/ns/net").stat().st_ino,
        "capture_epoch": str(uuid.uuid4()),
        "run_label": label,
        "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    frames, errors = [], []
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    def publish(running):
        value = {
            **identity,
            "heartbeat_ns": time.monotonic_ns(),
            "running": running,
            "errors": errors,
            "frames": frames,
            "physical_pod_changed": False,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value) + "\n")
        temporary.replace(path)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with ExitStack() as resources:
        channels = []
        # ETH_P_ALL receives at the slave-interface packet tap, before the
        # Linux bridge's RX handler can redirect protocol delivery to br-lan.
        # EtherType-specific sockets bound to the slave miss that traffic.
        for protocol in (3,):
            channel = resources.enter_context(
                socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(protocol))
            )
            channel.bind(("eth1", protocol))
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
            for destination in ("0180c2000013", "0180c200000e"):
                channel.setsockopt(
                    263, 1, struct.pack("=IHH8s", index, 0, 6, bytes.fromhex(destination))
                )
            channels.append(channel)
        publish(True)
        deadline, last_publish = time.monotonic() + seconds, 0.0
        try:
            while not stopping and time.monotonic() < deadline:
                for channel in select.select(channels, [], [], 0.1)[0]:
                    frame, _, flags, address = channel.recvmsg(1515)
                    if address[2] == 4:  # PACKET_OUTGOING
                        continue
                    # Keep no WSC payload, client traffic or other control message.
                    if not wanted(frame):
                        continue
                    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                        raise ValueError("truncated_discovery_packet")
                    now = time.monotonic_ns()
                    frames[:] = [f for f in frames if now - f["received_ns"] < 180_000_000_000]
                    if len(frames) >= 64:
                        raise ValueError("discovery_capture_budget_exhausted")
                    frames.append({"received_ns": now, "frame_hex": frame.hex()})
                if time.monotonic() - last_publish >= 0.25:
                    for channel in channels:
                        _, dropped = struct.unpack("=II", channel.getsockopt(263, 6, 8))
                        if dropped:
                            raise ValueError("packet_socket_drops")
                    if (
                        socket.if_nametoindex("eth1") != index
                        or Path("/sys/class/net/eth1/address").read_text().strip()
                        != identity["mac"]
                    ):
                        raise ValueError("interface_identity_changed")
                    publish(True)
                    last_publish = time.monotonic()
        except (OSError, ValueError) as error:
            errors.append(type(error).__name__ + ":" + str(error))
        finally:
            publish(False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    observe(args.label, args.seconds)
