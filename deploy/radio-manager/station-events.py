"""Read-only nl80211 station-removal observations in the owned hwsim AP.

Only CTRL_CMD_GETFAMILY and multicast membership are used. No radio command,
configuration write, reason inference or EasyMesh counter conversion is made.
Raw field presence and kernel station lifetime are evidence for qualification,
not a qualified final-session publisher. Run before the experiment's clients.
"""

import argparse
import errno
import hashlib
import json
import os
import runpy
import signal
import socket
import struct
import sys
import time
from pathlib import Path

ROOT = Path("/opt/emosa-radio-manager")
# Stable Linux UAPI enum values; checked against linux/nl80211.h. Numeric field
# names are retained as well as the projection so absent fields remain absent.
FIELDS = {
    1: ("inactive_ms", "I"),
    7: ("signal_dbm", "b"),
    9: ("rx_packets", "I"),
    10: ("tx_packets", "I"),
    11: ("tx_retries", "I"),
    12: ("tx_failed", "I"),
    13: ("signal_avg_dbm", "b"),
    16: ("connected_seconds", "I"),
    23: ("rx_bytes64", "Q"),
    24: ("tx_bytes64", "Q"),
    28: ("rx_drop_misc", "Q"),
    32: ("rx_duration_us", "Q"),
    39: ("tx_duration_us", "Q"),
    42: ("assoc_at_boottime_ns", "Q"),
}


def attributes(data):
    result = {}
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            raise ValueError("truncated netlink attribute")
        length, kind = struct.unpack_from("=HH", data, offset)
        if length < 4 or offset + length > len(data):
            raise ValueError("invalid netlink attribute length")
        if kind & 0x4000:
            raise ValueError("unexpected network-byte-order attribute")
        kind &= 0x3FFF
        if kind in result:
            raise ValueError("duplicate netlink attribute")
        result[kind] = data[offset + 4 : offset + length]
        offset += (length + 3) & ~3
        if offset > len(data):
            raise ValueError("truncated netlink alignment")
    return result


def scalar(data, fmt):
    if len(data) != struct.calcsize("=" + fmt):
        raise ValueError("invalid netlink scalar length")
    return struct.unpack("=" + fmt, data)[0]


def messages(data):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 16:
            raise ValueError("truncated netlink message")
        length, kind, flags, seq, pid = struct.unpack_from("=IHHII", data, offset)
        if length < 16 or offset + length > len(data):
            raise ValueError("invalid netlink message length")
        payload = data[offset + 16 : offset + length]
        if kind == 4:  # NLMSG_OVERRUN
            raise ValueError("kernel reports lost netlink messages")
        if kind == 2:  # NLMSG_ERROR, zero is an Ack
            if len(payload) < 4 or scalar(payload[:4], "i") != 0:
                raise ValueError("netlink request error")
        elif kind not in (1, 3):  # NOOP, DONE
            yield kind, flags, seq, pid, payload
        offset += (length + 3) & ~3
        if offset > len(data):
            raise ValueError("truncated netlink message alignment")


def receive(channel):
    data, _ancillary, flags, address = channel.recvmsg(262144)
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or address[0] != 0:
        raise ValueError("truncated or non-kernel netlink delivery")
    return list(messages(data))


def resolve(channel):
    # Generic-netlink controller request for the nl80211 family and MLME group.
    name = b"nl80211\0"
    attribute = struct.pack("=HH", 4 + len(name), 2) + name
    body = struct.pack("=BBH", 3, 1, 0) + attribute  # CTRL_CMD_GETFAMILY
    channel.sendto(struct.pack("=IHHII", 16 + len(body), 16, 1, 1, 0) + body, (0, 0))
    for kind, _flags, seq, _pid, payload in receive(channel):
        if kind != 16 or seq != 1 or len(payload) < 4 or payload[0] != 1:
            raise ValueError("unexpected generic-netlink family reply")
        fields = attributes(payload[4:])
        family = scalar(fields[1], "H")
        groups = [attributes(v) for v in attributes(fields[7]).values()]
        ids = [scalar(g[2], "I") for g in groups if g[1] == b"mlme\0"]
        if len(ids) != 1:
            raise ValueError("missing unique nl80211 MLME multicast group")
        return family, ids[0]
    raise ValueError("no nl80211 family reply")


def station_event(payload, ifindex):
    if len(payload) < 4:
        raise ValueError("truncated generic-netlink header")
    command = payload[0]
    if command not in (19, 20):  # NL80211_CMD_NEW_STATION / DEL_STATION
        return None
    fields = attributes(payload[4:])
    if scalar(fields[3], "I") != ifindex:
        return None
    mac = fields[6]
    if len(mac) != 6 or not any(mac) or mac[0] & 1:
        raise ValueError("invalid station identity")
    # Empty sinfo is a legitimate kernel event when allocation failed. Preserve
    # it as incomplete, never materialize the absent counters as zero.
    info = attributes(fields[21]) if 21 in fields else {}
    result = {
        "event": "new_station" if command == 19 else "del_station",
        "ifindex": ifindex,
        "station": mac.hex(":"),
        "attribute_ids": sorted(fields),
        "station_info_ids": sorted(info),
        "raw_station_info": {str(k): v.hex() for k, v in sorted(info.items())},
        "observed_fields": {
            name: scalar(info[k], fmt) for k, (name, fmt) in FIELDS.items() if k in info
        },
    }
    if 46 in fields:
        result["kernel_generation"] = scalar(fields[46], "I")
    if 54 in fields:
        result["reason_code"] = scalar(fields[54], "H")
    return result


def observe(seconds, *, emit):
    runpy.run_path(str(ROOT / "node.py"))["guard"]()
    ifindex = socket.if_nametoindex("wlan0")
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    counts = {"new_station": 0, "del_station": 0}
    active = {}
    errors = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16) as channel:
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
        channel.settimeout(2)
        channel.bind((0, 0))
        family, group = resolve(channel)
        channel.setsockopt(270, 1, group)  # SOL_NETLINK, NETLINK_ADD_MEMBERSHIP
        channel.settimeout(0.2)
        emit(
            {
                "event": "ready",
                "ifindex": ifindex,
                "interface": "wlan0",
                "bssid": Path("/sys/class/net/wlan0/address").read_text().strip(),
                "kernel": os.uname().release,
                "machine": os.uname().machine,
                "byteorder": sys.byteorder,
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "network_namespace": os.readlink("/proc/self/ns/net"),
                "family": family,
                "mlme_group": group,
                "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "final_counter_source_qualified": False,
            }
        )
        deadline = time.monotonic() + seconds
        try:
            while not stopping and time.monotonic() < deadline:
                try:
                    batch = receive(channel)
                except TimeoutError:
                    continue
                for kind, _flags, seq, _pid, payload in batch:
                    if kind != family or seq != 0:
                        raise ValueError("unexpected multicast family or sequence")
                    event = station_event(payload, ifindex)
                    if event is None:
                        continue
                    counts[event["event"]] += 1
                    if sum(counts.values()) > 4096:
                        raise ValueError("station event budget exhausted")
                    mac = event["station"]
                    if event["event"] == "new_station":
                        if mac in active or len(active) >= 64:
                            raise ValueError("duplicate new station or inventory budget exhausted")
                        active[mac] = counts["new_station"]
                    event["observed_lifetime"] = active.get(mac)
                    if event["event"] == "del_station":
                        active.pop(mac, None)
                    emit(event)
        except (ValueError, KeyError, OSError) as exc:
            errors.append(
                "netlink_overrun" if getattr(exc, "errno", None) == errno.ENOBUFS else str(exc)
            )
        emit(
            {
                "event": "finished",
                "counts": counts,
                "errors": errors,
                "remaining_observed_stations": sorted(active),
                "final_counter_source_qualified": False,
            }
        )
    if errors:
        raise RuntimeError("station event observation incomplete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 7200:
        parser.error("seconds must be 30–7200")

    def emit(value):
        # Bound read uncertainty separately from actual wall/monotonic drift.
        for _ in range(3):
            before, wall, after = time.monotonic_ns(), time.time_ns(), time.monotonic_ns()
            if 0 <= after - before <= 100_000:
                break
        else:
            raise RuntimeError("station event clock read uncertainty exceeds budget")
        value["received_monotonic_ns"] = before
        value["received_wall_ns"] = wall
        value["received_monotonic_after_ns"] = after
        print(json.dumps(value, sort_keys=True), flush=True)

    observe(args.seconds, emit=emit)
