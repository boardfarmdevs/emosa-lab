"""Fixed, bounded UDP traffic endpoints inside the owned wired lab containers."""

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path("/opt/emosa-radio-manager/link-calibration")
PORT = 49191


def main(args):
    hostname = "em-baseline-controller" if args.receive else "em-baseline-wired"
    if (
        os.geteuid() != 0
        or socket.gethostname() != hostname
        or Path("/sys/class/net/eth0").exists()
    ):
        raise RuntimeError("expected the fixed owned isolated traffic endpoint")
    if subprocess.check_output(["systemd-detect-virt", "--container"], text=True).strip() != "lxc":
        raise RuntimeError("expected owned LXD container")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", args.label) or not re.fullmatch(
        r"[0-9a-f]{16}", args.nonce
    ):
        raise ValueError("invalid bounded traffic identity")
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    nonce = bytes.fromhex(args.nonce)
    result = {
        "label": args.label,
        "nonce": args.nonce,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "errors": [],
    }
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        if args.receive:
            path = ROOT / (args.label + ".json")
            path.open("x").close()
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            channel.bind(("192.0.2.1", PORT))
            channel.settimeout(0.1)
            (ROOT / (args.label + ".ready")).write_text(args.nonce)
            records = []
            deadline = time.monotonic() + 90
            while not stopping and time.monotonic() < deadline:
                try:
                    data, peer = channel.recvfrom(2048)
                except TimeoutError:
                    continue
                if len(data) < 16 or data[:12] != b"EMVL" + nonce or peer[0] != "192.0.2.20":
                    result["errors"].append("unexpected_datagram")
                    break
                records.append(
                    [
                        data[12],
                        int.from_bytes(data[13:16], "big"),
                        len(data),
                        time.monotonic_ns(),
                        time.time_ns(),
                    ]
                )
                if len(records) > 150000:
                    result["errors"].append("receive_budget")
                    break
            result.update(
                records=records, stopped_by_signal=stopping, finished_ns=time.monotonic_ns()
            )
            path.write_text(json.dumps(result) + "\n")
        else:
            if (
                args.phase not in (1, 2, 3)
                or args.size not in (16, 1472)
                or args.rate not in (5_000_000, 50_000_000, 160_000_000)
            ):
                raise ValueError("unsupported calibration workload")
            channel.bind(("192.0.2.20", 0))
            start = time.monotonic_ns()
            deadline = start + 4_000_000_000
            count = 0
            charged = max(args.size + 42 + 24, 84)
            interval = charged * 8 * 1_000_000_000 / args.rate
            while not stopping and time.monotonic_ns() < deadline:
                target = start + int(count * interval)
                delay = target - time.monotonic_ns()
                if delay > 0:
                    time.sleep(delay / 1e9)
                if time.monotonic_ns() >= deadline:
                    break
                data = (
                    b"EMVL"
                    + nonce
                    + bytes([args.phase])
                    + count.to_bytes(3, "big")
                    + bytes(args.size - 16)
                )
                channel.sendto(data, ("192.0.2.1", PORT))
                count += 1
                if count > 100000:
                    raise RuntimeError("sender budget")
            result.update(
                phase=args.phase,
                udp_payload_size=args.size,
                target_wire_bps=args.rate,
                started_ns=start,
                ended_ns=time.monotonic_ns(),
                packets_sent=count,
                charged_octets_per_packet=charged,
            )
            print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    parser.add_argument("nonce")
    parser.add_argument("--receive", action="store_true")
    parser.add_argument("--phase", type=int)
    parser.add_argument("--size", type=int)
    parser.add_argument("--rate", type=int)
    main(parser.parse_args())
