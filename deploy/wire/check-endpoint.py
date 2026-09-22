"""Exercise actual AF_PACKET endpoints in fresh, isolated VM network namespaces.

Only empty IEEE Topology Queries are sent. This is transport verification, not
a topology responder, EasyMesh controller, virtual agent or provisioning demo.
"""

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

from emosa.wire.cmdu import Reassembler, fragment_message
from emosa.wire.ethernet import EthernetEndpoint

LEFT = bytes.fromhex("020000004001")
RIGHT = bytes.fromhex("020000004002")


def worker(side, interface, directory):
    directory = Path(directory)
    own, peer = (LEFT, RIGHT) if side == "left" else (RIGHT, LEFT)
    captures = []
    with EthernetEndpoint(interface, own, timeout=0.5) as endpoint:
        (directory / (side + ".ready")).touch()
        if side == "left":
            for mid in (65535, 0):
                endpoint.send(fragment_message(peer, own, 2, mid, [])[0])
        end = time.monotonic() + 5
        mids = []
        while len(mids) < 2 and time.monotonic() < end:
            frame = endpoint.receive()
            if frame is None:
                continue
            message = Reassembler().feed(frame)
            assert message.source == peer and message.destination == own
            assert message.message_type == 2 and not message.tlvs
            mids.append(message.mid)
            captures.append(frame)
            if side == "right":
                # A second independent query, explicitly not a Topology Response.
                endpoint.send(fragment_message(peer, own, 2, message.mid, [])[0])
        assert mids == [65535, 0], mids
    pcap = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for index, frame in enumerate(captures):
        pcap += struct.pack("<IIII", 1, index, len(frame), len(frame)) + frame
    (directory / (side + ".pcap")).write_bytes(pcap)
    (directory / (side + ".json")).write_text(
        json.dumps(
            {
                "passed": True,
                "transport": "AF_PACKET",
                "mids_received": mids,
                "message_type": "0x0002",
                "frames_received": len(captures),
                "onboarding_proven": False,
                "config_writes": 0,
            },
            indent=2,
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--worker", choices=("left", "right"))
    parser.add_argument("--interface")
    args = parser.parse_args()
    if socket.gethostname() != "emosa-lab" or os.geteuid() != 0:
        raise SystemExit("Run as root only inside the dedicated emosa-lab VM")
    if args.worker:
        worker(args.worker, args.interface, args.directory)
        return
    args.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    token = uuid.uuid4().hex[:8]
    names = ["emosa-wire-" + token + suffix for suffix in ("-a", "-b")]
    links = ["emw" + token + suffix for suffix in ("a", "b")]
    created, children = [], []

    def run(*command):
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)

    try:
        for name in names:
            run("ip", "netns", "add", name)
            created.append(name)
        run(
            "ip",
            "-n",
            names[0],
            "link",
            "add",
            links[0],
            "type",
            "veth",
            "peer",
            "name",
            links[1],
            "netns",
            names[1],
        )
        for name, link, address in zip(names, links, (LEFT, RIGHT), strict=True):
            run("ip", "-n", name, "link", "set", link, "address", address.hex(":"), "up")
        for side, name, link in zip(
            ("right", "left"), reversed(names), reversed(links), strict=True
        ):
            log = (args.directory / (side + ".log")).open("w")
            process = subprocess.Popen(
                [
                    "ip",
                    "netns",
                    "exec",
                    name,
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--directory",
                    str(args.directory.resolve()),
                    "--worker",
                    side,
                    "--interface",
                    link,
                ],
                stdout=log,
                stderr=log,
            )
            log.close()
            children.append(process)
            end = time.monotonic() + 5
            while not (args.directory / (side + ".ready")).exists():
                if process.poll() is not None or time.monotonic() >= end:
                    raise RuntimeError("packet endpoint did not become ready; inspect its log")
                time.sleep(0.05)
        for process in children:
            assert process.wait(timeout=10) == 0, "endpoint failed; retain worker logs"
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for name in reversed(created):
            run("ip", "netns", "delete", name)
    print(
        json.dumps(
            {
                "passed": True,
                "namespaces_removed": created,
                "physical_interfaces_used": False,
                "onboarding_proven": False,
            }
        )
    )


if __name__ == "__main__":
    main()
