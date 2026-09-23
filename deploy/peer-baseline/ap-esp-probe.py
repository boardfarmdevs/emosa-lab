"""Owned synthetic controller parser diagnostic; never a measurement publisher.

Run only through native-onboarding.py after its simulated pod has onboarded and
its worker has stopped. Deliberately malformed TLVs test receiver bounds.
"""

import asyncio
import struct
import sys
import time

from setup import run

NAMES = tuple("EstServiceParameters" + ac for ac in ("BE", "BK", "VO", "VI"))
BSSID = bytes.fromhex("020000ec0200")
SENDER = """
import socket, sys
with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x893a)) as s:
    s.bind(('probe0', 0))
    frame = bytes.fromhex(sys.argv[1])
    assert s.send(frame) == len(frame)
"""


def values(inventory):
    rows = {k: v for obj in inventory for k, v in obj.items() if isinstance(v, dict)}
    devices = [k for k, v in rows.items() if v.get("ID") == "02:00:00:00:30:01"]
    if len(devices) != 1:
        raise RuntimeError("diagnostic needs exactly one onboarded virtual agent")
    bsses = [
        (k, v)
        for k, v in rows.items()
        if k.startswith(devices[0]) and v.get("BSSID") == BSSID.hex(":")
    ]
    if len(bsses) != 1:
        raise RuntimeError("diagnostic needs the actual onboarded virtual BSS")
    path, bss = bsses[0]
    radio = rows[path.rsplit("BSS.", 1)[0]]
    return {name: bss[name] for name in NAMES} | {"Utilization": radio["Utilization"]}


async def probe(directory, namespace, inventory, write):
    result = {
        "scope": "synthetic native sparse-ESP parser diagnostic; not measured AP reporting",
        "passed": False,
        "cases": [],
        "measurement_source_qualified": False,
        "native_ap_report_delivery_proven": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
    }
    target = directory / "ap-esp-probe.json"
    write(target, result)
    expected = values(inventory(depth=8))
    cases = [(flags, None) for flags in (0x90, 0xF0, 0x80, 0xC0, 0xA0, 0xB0, 0xD0, 0xE0)]
    cases += [(0x10, 3), (0x90, 5), (0x90, 7), (0x90, 0), (0xF0, 11), (0xF0, 13), (0, 0), (0x80, 4)]
    for index, (flags, malformed_size) in enumerate(cases):
        chunks = {
            name: bytes((0x10 + ac, 0x40 + index, 0x80 + ac))
            for ac, name in enumerate(NAMES)
            if flags & (0x80 >> ac)
        }
        payload = b"".join(chunks.values())
        valid = malformed_size is None
        if not valid:
            payload = (payload + bytes(15))[:malformed_size]
        utilization = 40 + index
        tlv = BSSID + bytes((utilization, 0, 0, flags)) + payload
        frame = bytes.fromhex("020000e00001020000003001893a0000800c")
        frame += struct.pack("!HBB", 0xD000 + index, 0, 0x80)
        frame += b"\x94" + struct.pack("!H", len(tlv)) + tlv + b"\0\0\0"
        if valid:
            expected.update({name: int.from_bytes(data, "little") for name, data in chunks.items()})
            expected["Utilization"] = utilization
        row = {
            "index": index,
            "valid_presence_length": valid,
            "flags": flags,
            "frame_hex": frame.hex(),
            "expected": dict(expected),
            "sent_at": time.time(),
            "observations": [],
        }
        result["cases"].append(row)
        write(target, result)
        run("ip", "netns", "exec", namespace, sys.executable, "-c", SENDER, frame.hex())
        deadline = time.monotonic() + 3
        while True:
            await asyncio.sleep(0.15 if valid else 0.5)
            started = time.time_ns()
            objects = inventory(depth=8)
            ended = time.time_ns()
            actual = values(objects)
            observed = {"wall_started_ns": started, "wall_ended_ns": ended, "actual": actual}
            row["observations"].append(observed)
            write(directory / f"ap-esp-inventory-{index:02}.json", objects)
            write(target, result)
            if actual == expected:
                break
            if not valid or time.monotonic() >= deadline:
                raise RuntimeError("native sparse-ESP diagnostic mismatch")
    result["passed"] = True
    result["finished_at"] = time.time()
    write(target, result)
    return result
