"""Install or stop the owned native baseline; never touch other lab containers."""

import argparse
import hashlib
import json
import re
from pathlib import Path

from node import HOSTAP, INSTALL, NAMES, UNITS
from run import guard, stop_collect
from setup import NODES, ROOT, inside, lxc


def install():
    reference = json.loads((ROOT / "reference.json").read_text())
    runtime = reference["hostap_runtime"]
    archive = Path("/opt/peer-artifacts") / runtime["archive"]
    if hashlib.sha256(archive.read_bytes()).hexdigest() != runtime["sha256"]:
        raise SystemExit("Hostap build differs from the selected reference; qualify new bytes")
    overlay = reference["native_hal_overlay"]
    hal_archive = Path("/opt/peer-artifacts") / overlay["archive"]
    if hashlib.sha256(hal_archive.read_bytes()).hexdigest() != overlay["sha256"]:
        raise SystemExit("HAL build differs from the selected reference")
    for name in NODES:
        for item in (*UNITS, "client", "data", "capture"):
            state = inside(
                name,
                "systemctl",
                "show",
                f"emosa-baseline-{item}.service",
                "-p",
                "ActiveState",
                "--value",
            ).strip()
            if state in {"active", "activating", "deactivating"}:
                raise SystemExit("Stop and collect all owned units before installation")
    for name in NODES:
        lxc("file", "push", "--quiet", str(archive), name + "/opt/")
        inside(name, "mkdir", "-p", str(HOSTAP))
        inside(name, "tar", "-C", str(HOSTAP), "-xzf", "/opt/" + archive.name)
        for filename in ("node.py", "observer.py", "reference.json", "prplmesh.reference.json"):
            lxc("file", "push", "--quiet", str(ROOT / filename), name + str(ROOT) + "/")
        for binary, expected in runtime["binaries"].items():
            observed = inside(name, "sha256sum", str(HOSTAP / binary)).split()[0]
            if observed != expected:
                raise SystemExit("Installed binary digest mismatch")
        if name in NAMES:
            library = str(INSTALL / "lib/libbwl.so.6.0.0")
            observed = inside(name, "sha256sum", library).split()[0]
            if observed not in (overlay["original_library_sha256"], overlay["library_sha256"]):
                raise SystemExit(
                    "Unexpected existing HAL; preserve and investigate before replacing"
                )
            lxc("file", "push", "--quiet", str(hal_archive), name + "/opt/")
            inside(name, "tar", "-C", str(INSTALL), "-xzf", "/opt/" + hal_archive.name)
            if inside(name, "sha256sum", library).split()[0] != overlay["library_sha256"]:
                raise SystemExit("Installed HAL digest mismatch")
    print("Installed pinned native baseline runtime; no test verdict implied")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "stop"))
    parser.add_argument("--label", help="Unique evidence label for stop")
    args = parser.parse_args()
    guard()
    if args.action == "install":
        install()
    else:
        if not args.label or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,70}", args.label):
            raise SystemExit("Stop requires a unique lowercase evidence label")
        result = stop_collect(ROOT / "runs" / args.label / "shutdown")
        abnormal = any(
            unit["after"].get("Result") != "success" for units in result.values() for unit in units
        )
        print("Stopped owned units; shutdown results retained")
        raise SystemExit(1 if abnormal else 0)


if __name__ == "__main__":
    main()
