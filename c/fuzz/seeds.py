#!/usr/bin/env python3
"""Seed corpora for the C fuzz targets (c/fuzz), taken from the conformance vectors.

    python3 c/fuzz/seeds.py            # writes c/fuzz/corpus/{cmdu,stats,rows,wsc,fleet,gtp}

wsc: each recorded M2 and tampered M2, after a flags byte of 0 (fuzz/wsc.c). cmdu: every
1905 frame in the vectors, and each multi-frame decode case as one
input (frames after their two-byte length, as fuzz/cmdu.c reads them). stats: every
recorded MQTT payload. rows: every recorded set of OVSDB tables. fleet: each pod's
select result as the reply on its connection after an echo, and every registry file the
fleet sessions leave. gtp: each command of the GTP sessions, its first ip output, the lease
file and the event (fuzz/gtp.c's three parts). Files are named by their SHA-1, as
libFuzzer names its own; rerunning replaces the corpora.
"""

import hashlib
import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[2]
VECTORS = ROOT / "spec" / "conformance"
CORPUS = ROOT / "c" / "fuzz" / "corpus"


def load(name):
    return json.loads((VECTORS / name).read_text())


def is_hex(value):
    return (
        isinstance(value, str)
        and len(value) >= 28
        and len(value) % 2 == 0
        and all(c in "0123456789abcdef" for c in value)
    )


def frames_in(node):
    """Every hex frame under a vector's requests and expected frames."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("request", "frames") or key.endswith("_frames"):
                if is_hex(value):
                    yield bytes.fromhex(value)
                elif isinstance(value, list):
                    yield from (bytes.fromhex(v) for v in value if is_hex(v))
            yield from frames_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from frames_in(value)


def framed(frames):
    return b"".join(len(f).to_bytes(2, "big") + f for f in frames)


def tables_in(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "ovsdb_tables" and isinstance(value, dict):
                yield json.dumps(value, sort_keys=True).encode()
            else:
                yield from tables_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from tables_in(value)


def main():
    seeds = {name: set() for name in ("cmdu", "stats", "rows", "wsc", "fleet", "gtp")}
    for case in load("cmdu.json")["decode"]:
        frames = [bytes.fromhex(f) for f in case["frames"]]
        seeds["cmdu"].add(framed(frames))
    for name in (
        "cmdu.json",
        "control.json",
        "backhaul-steering.json",
        "metrics.json",
        "early-report.json",
    ):
        for frame in frames_in(load(name)):
            seeds["cmdu"].add(framed([frame]))
    for case in load("onboarding.json")["cases"]:
        for key in ("m2_frames", "tampered_m2_frames"):
            frames = [bytes.fromhex(f) for f in case.get(key) or []]
            if frames:
                seeds["wsc"].add(b"\x00" + framed(frames))
    for step in load("telemetry.json")["steps"]:
        seeds["stats"].add(bytes.fromhex(step["payload"]))
    for case in load("metrics.json")["cases"]:
        for payload in case.get("publishes", []):
            if is_hex(payload):
                seeds["stats"].add(bytes.fromhex(payload))
    for name in ("translation-northbound.json", "uplink.json"):
        seeds["rows"].update(tables_in(load(name)))
    for session in load("fleet-sessions.json")["sessions"]:
        for step in session["steps"]:
            if "arrival" in step:
                echo = {"method": "echo", "params": [], "id": "echo"}
                reply = {"id": 1, "result": step["arrival"], "error": None}
                seeds["fleet"].add((json.dumps(echo) + json.dumps(reply)).encode())
        seeds["fleet"].add(session["files"]["registry"].encode())
    for session in load("gtp.json")["sessions"]:
        for step in session["steps"]:
            outputs = [call[2] for call in step["calls"] if call[2]] or [""]
            event = " ".join(step["command"][1:4]) if step["command"][0] == "lease" else ""
            seeds["gtp"].add(b"\0".join(s.encode() for s in (outputs[0], session["leases"], event)))
    for target, inputs in seeds.items():
        directory = CORPUS / target
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        for data in inputs:
            (directory / hashlib.sha1(data).hexdigest()).write_bytes(data)
        print(f"{target}: {len(inputs)} seeds")


if __name__ == "__main__":
    main()
