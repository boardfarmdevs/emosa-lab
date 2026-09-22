"""Independently compare the retained native controller counter candidate captures."""

import argparse
import hashlib
import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=ROOT / "doc/evidence/native-controller-counter"
    )
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    check = runpy.run_path(str(ROOT / "scripts/check-controller-probe.py"))["check"]
    reference = read(ROOT / "deploy/peer/prplmesh.reference.json")
    baseline = reference["binaries"]["beerocks_controller"]
    build = read(args.directory / "candidate.json")
    assert build["counter_regression"]["passed"] == 12
    assert build["patch_sha256"] == digest(
        ROOT / "deploy/peer-baseline/patches/0004-controller-counter-capability.patch"
    )
    assert build["candidate_sha256"] != baseline
    before = check(args.tshark, ROOT / "doc/evidence/native-discovery/run-07")
    assert before["controller_flags_hex"] == "40"
    results = []
    for name in ("candidate-01", "candidate-02"):
        directory = args.directory / name
        result = check(args.tshark, directory)
        assert result["controller_flags_hex"] == "c0"
        assert result["selected_response_issues"] == ["security_capability_absent"]
        trial = read(directory / "candidate-trial.json")
        assert trial["status"] == "counter_flag_observed_admission_pending"
        assert trial["candidate_installed"] and trial["baseline_restored"]
        assert not trial["failure_injection"]
        assert trial["candidate_sha256"] == build["candidate_sha256"]
        assert trial["baseline_sha256"] == baseline
        assert trial["probe"] == read(directory / "probe.json")
        selected = read(directory / "reference-candidate.json")
        assert selected["binaries"] == reference["binaries"] | {
            "beerocks_controller": build["candidate_sha256"]
        }
        recorded = read(directory / "result.json")["source_sha256"]["prplmesh.reference.json"]
        assert recorded == digest(directory / "reference-candidate.json")
        assert not trial["controller_onboarding_proven"] and not trial["physical_pod_proven"]
        assert trial["operations_created"] == 0
        results.append(result)
    failure = read(args.directory / "failure-restoration.json")
    assert failure["status"] == "failed" and failure["failure_injection"]
    assert failure["candidate_installed"] and failure["baseline_restored"]
    assert failure["baseline_sha256"] == baseline
    assert failure["candidate_sha256"] == build["candidate_sha256"]
    assert "probe" not in failure and failure["operations_created"] == 0
    after = check(args.tshark, args.directory / "restored-baseline")
    assert after["controller_flags_hex"] == "40"
    recorded = read(args.directory / "restored-baseline/result.json")["source_sha256"]
    assert recorded["prplmesh.reference.json"] == digest(
        ROOT / "deploy/peer/prplmesh.reference.json"
    )
    print(
        json.dumps(
            {
                "passed": True,
                "native_counter_cases": 12,
                "baseline_before": before,
                "candidates": results,
                "restored_baseline": after,
                "post_install_failure_restored": True,
                "controller_onboarding_proven": False,
                "physical_pod_proven": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
