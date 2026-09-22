"""Temporarily test a candidate HAL in owned native peers, then restore the baseline.

Run as root in the dedicated VM. This never runs EMOSA or contacts an OpenSync pod.
The unchanged native executables remain subject to node.py's pinned digest checks.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node import INSTALL  # noqa: E402
from run import AGENT, CONTROLLER, guard, stop_collect, write  # noqa: E402
from setup import NODES, ROOT, inside, lxc  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--policy", choices=("front-and-backhaul", "sole-fronthaul"), default="sole-fronthaul"
    )
    args = parser.parse_args()
    guard()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", args.label):
        parser.error("Use a new lowercase run label")
    if args.build.parent != ROOT or not args.build.name.startswith("candidate-"):
        parser.error("Select an isolated candidate build below /opt/emosa-baseline")
    if (ROOT / "runs" / args.label).exists():
        parser.error("Run label already exists")
    for name in NODES:
        active = inside(
            name,
            "systemctl",
            "list-units",
            "--state=active,activating,deactivating",
            "--no-legend",
            "--plain",
            "emosa-baseline-*",
            "emosa-radio-manager-*",
        )
        if active.strip():
            raise SystemExit("Collect and stop the owned lab services before this experiment")
    library = INSTALL / "lib/libbwl.so.6.0.0"
    candidate = args.build / "stage/lib/libbwl.so.6.0.0"
    expected = (args.build / "library.sha256").read_text().split()[0]
    if digest(candidate) != expected:
        raise SystemExit("Candidate differs from the recorded build")
    state = ROOT / "compatibility-runs" / args.label
    state.mkdir(parents=True, exist_ok=False)
    reference = ROOT / "reference.json"
    original = reference.read_bytes()
    baseline = json.loads(original)
    original_sha = baseline["native_hal_overlay"]["library_sha256"]
    if baseline.get("candidate_experiment"):
        raise SystemExit("A previous candidate reference has not been restored")
    for name in (CONTROLLER, AGENT):
        if inside(name, "sha256sum", str(library)).split()[0] != original_sha:
            raise SystemExit("Existing runtime differs from baseline; refusing replacement")
        lxc("file", "pull", "--quiet", name + str(library), str(state / (name + ".so")))
        lxc("file", "pull", "--quiet", name + str(reference), str(state / (name + ".json")))
        if (state / (name + ".json")).read_bytes() != original:
            raise SystemExit("Peer reference differs from VM reference")
    (state / "reference-before.json").write_bytes(original)
    baseline["native_hal_overlay"]["library_sha256"] = expected
    baseline["native_hal_overlay"]["archive"] = None
    baseline["native_hal_overlay"]["sha256"] = None
    baseline["candidate_experiment"] = {
        "label": args.label,
        "build": str(args.build),
        "baseline_library_sha256": original_sha,
        "scope": "HE length fix only; other native interoperability findings remain pending",
    }
    write(state / "reference-candidate.json", baseline)
    report = {
        "baseline_library_sha256": original_sha,
        "candidate_library_sha256": expected,
        "policy": args.policy,
        "emosa_wire_onboarding": False,
        "physical_pod_accessed": False,
    }
    changed = []
    try:
        reference.write_bytes((state / "reference-candidate.json").read_bytes())
        for name in (CONTROLLER, AGENT):
            changed.append(name)
            lxc("file", "push", "--quiet", str(candidate), name + str(library))
            lxc("file", "push", "--quiet", str(reference), name + str(reference))
        result = subprocess.run(
            [
                "python3",
                str(ROOT / "run.py"),
                "--mode",
                "wired",
                "--label",
                args.label,
                "--policy",
                args.policy,
            ],
            timeout=600,
            check=False,
        )
        report["run_exit_code"] = result.returncode
    finally:
        # Stop before restoring the library. If collection fails, leave evidence and
        # candidate bytes in place for recovery; never swap a potentially live HAL.
        shutdown = stop_collect(state / "shutdown")
        report["abnormal_shutdown"] = any(
            unit["after"].get("Result") != "success"
            for units in shutdown.values()
            for unit in units
        )
        for name in changed:
            lxc("file", "push", "--quiet", str(state / (name + ".so")), name + str(library))
            lxc("file", "push", "--quiet", str(state / (name + ".json")), name + str(reference))
            if inside(name, "sha256sum", str(library)).split()[0] != original_sha:
                raise RuntimeError("Baseline library restoration failed")
        reference.write_bytes(original)
        report["baseline_restored"] = True
        write(state / "result.json", report)
    print(json.dumps(report, indent=2))
    raise SystemExit(report.get("run_exit_code", 1))


if __name__ == "__main__":
    main()
