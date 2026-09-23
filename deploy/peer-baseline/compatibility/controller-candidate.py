"""Temporarily measure a native controller candidate in the owned lab, then restore.

Only the controller executable and its explicit experimental reference change.
The same discovery probe and admission checks run for baseline and candidate.
"""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import runpy
import shutil
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node import INSTALL  # noqa: E402
from run import CONTROLLER, stop_collect, write  # noqa: E402
from setup import ROOT, inside, lxc  # noqa: E402


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def trial(build, label, *, fail_after_install=False, experiment=None):
    helpers = runpy.run_path(str(ROOT / "controller-trial.py"))
    helpers["idle"]()
    candidate = build / "stage/bin/beerocks_controller"
    provenance = json.loads((build / "candidate.json").read_text())
    expected = provenance["candidate_sha256"]
    patch = ROOT / "0004-controller-counter-capability.patch"
    if digest(candidate) != expected or digest(patch) != provenance["patch_sha256"]:
        raise RuntimeError("Candidate/patch differs from the recorded build")
    if provenance["counter_regression"]["passed"] != 12:
        raise RuntimeError("Native counter conversion regression is incomplete")
    for extra in provenance.get("extra_patches", []):
        if (
            extra["name"] != "0005-controller-configuration-scope.patch"
            or digest(ROOT / extra["name"]) != extra["sha256"]
        ):
            raise RuntimeError("Unknown or mismatched additional candidate patch")
    reference = ROOT / "prplmesh.reference.json"
    original = reference.read_bytes()
    baseline = json.loads(original)
    if "candidate_experiment" in baseline:
        raise RuntimeError("Previous candidate reference has not been restored")
    original_sha = baseline["binaries"]["beerocks_controller"]
    binary = INSTALL / "bin/beerocks_controller"
    if inside(CONTROLLER, "sha256sum", str(binary)).split()[0] != original_sha:
        raise RuntimeError("Existing controller differs from the pinned baseline")
    if json.loads(inside(CONTROLLER, "cat", str(reference))) != baseline:
        raise RuntimeError("Container and VM peer references differ")
    if (ROOT / "discovery-trials" / label).exists():
        raise RuntimeError("Discovery label already exists")
    state = ROOT / "controller-candidates" / label
    state.mkdir(parents=True, mode=0o700, exist_ok=False)
    write(state / "candidate.json", provenance)
    (state / "reference-before.json").write_bytes(original)
    temporary = state / "baseline-controller"
    lxc("file", "pull", "--quiet", CONTROLLER + str(binary), str(temporary))
    if digest(temporary) != original_sha:
        raise RuntimeError("Controller backup digest mismatch")
    with (
        temporary.open("rb") as source,
        gzip.open(state / "baseline-controller.gz", "wb") as target,
    ):
        shutil.copyfileobj(source, target)
    temporary.unlink()  # Only this newly created staging copy; compressed backup is retained.
    baseline["binaries"]["beerocks_controller"] = expected
    baseline["candidate_experiment"] = {
        "label": label,
        "baseline_controller_sha256": original_sha,
        "scope": "Isolated controller experiment; see candidate patch provenance",
    }
    write(state / "reference-candidate.json", baseline)
    report = {
        "label": label,
        "candidate_sha256": expected,
        "baseline_sha256": original_sha,
        "baseline_restored": False,
        "status": "preparing",
        "operations_created": 0,
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
        "failure_injection": fail_after_install,
        "runner_sha256": digest(Path(__file__)),
    }
    write(state / "result.json", report)
    try:
        reference.write_bytes((state / "reference-candidate.json").read_bytes())
        lxc("file", "push", "--quiet", str(candidate), CONTROLLER + str(binary))
        inside(CONTROLLER, "chmod", "0755", str(binary))
        lxc("file", "push", "--quiet", str(reference), CONTROLLER + str(reference))
        if inside(CONTROLLER, "sha256sum", str(binary)).split()[0] != expected:
            raise RuntimeError("Installed candidate digest mismatch")
        report["candidate_installed"] = True
        if fail_after_install:
            raise RuntimeError("Deliberate post-install failure to test restoration")
        if experiment is not None:
            result = experiment(label)
            report.update(status="experiment_finished", experiment=result)
            report["operations_created"] = result.get("operation", {}).get("operation_count", 0)
            report["controller_onboarding_proven"] = result["controller_onboarding_proven"]
        else:
            runpy.run_path(str(ROOT / "discovery-trial.py"))["trial"](label)
            probe = json.loads((ROOT / "discovery-trials" / label / "probe.json").read_text())
            if probe["controller_flags_hex"] != "c0" or probe["selected_response_issues"] != [
                "security_capability_absent"
            ]:
                raise RuntimeError("Unexpected candidate response; inspect discovery evidence")
            report.update(status="counter_flag_observed_admission_pending", probe=probe)
    except BaseException as exc:
        report.update(status="failed", error=type(exc).__name__)
        raise
    finally:
        try:
            # If stop/collection fails, retain the candidate/reference and backup for
            # recovery. Never overwrite a potentially running executable.
            report["restoration_shutdown"] = stop_collect(
                state / "restoration-shutdown", names=(CONTROLLER,)
            )
            helpers["idle"]()
            with (
                gzip.open(state / "baseline-controller.gz", "rb") as source,
                temporary.open("wb") as target,
            ):
                shutil.copyfileobj(source, target)
            if digest(temporary) != original_sha:
                raise RuntimeError("Restoration backup digest mismatch")
            lxc("file", "push", "--quiet", str(temporary), CONTROLLER + str(binary))
            inside(CONTROLLER, "chmod", "0755", str(binary))
            lxc(
                "file",
                "push",
                "--quiet",
                str(state / "reference-before.json"),
                CONTROLLER + str(reference),
            )
            reference.write_bytes(original)
            if inside(CONTROLLER, "sha256sum", str(binary)).split()[0] != original_sha:
                raise RuntimeError("Baseline restoration digest mismatch")
            if inside(CONTROLLER, "sha256sum", str(reference)).split()[0] != digest(reference):
                raise RuntimeError("Peer reference restoration digest mismatch")
            report["baseline_restored"] = True
            temporary.unlink()
        except BaseException as exc:
            report.update(status="restoration_failed", restoration_error=type(exc).__name__)
            raise
        finally:
            write(state / "result.json", report)
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--fail-after-install", action="store_true", help="Test restoration; expected failure"
    )
    args = parser.parse_args()
    if args.build.resolve().parent != ROOT or not args.build.name.startswith("candidate-"):
        parser.error("Stage a separate candidate directory directly below /opt/emosa-baseline")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", args.label):
        parser.error("Use a new 1–24 character lowercase run label")
    os.umask(0o077)
    with Path("/opt/emosa-radio-manager/run.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        try:
            trial(args.build, args.label, fail_after_install=args.fail_after_install)
        finally:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
