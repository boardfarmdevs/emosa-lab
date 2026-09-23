import json
import runpy
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
EVIDENCE = Path("doc/evidence/native-lifecycle/native-lifecycle-soak-01")
CHECK = runpy.run_path("scripts/check-native-lifecycle.py")["check"]
CANDIDATE = runpy.run_path("scripts/check-native-onboarding.py")["candidate_inputs"]
FILES = (
    "candidate.json",
    "candidate-trial.json",
    "result.json",
    "native-processes-start.json",
    "native-processes-stop.json",
    "reference-candidate.json",
    "native-session.json",
    "post-restoration-observation.json",
)


def test_recorded_native_lifecycle_and_known_candidate_pass():
    assert CHECK(EVIDENCE)["selected_native_lifecycle_checks_passed"]
    CANDIDATE(
        json.loads((EVIDENCE / "candidate.json").read_text()),
        json.loads((EVIDENCE / "candidate-trial.json").read_text()),
    )


@pytest.mark.parametrize(
    ("filename", "keys", "value"),
    [
        (
            "native-processes-stop.json",
            ("units", "controller", "properties", "SuccessExitStatus"),
            "6",
        ),
        (
            "native-processes-start.json",
            ("units", "agent", "libraries", "libbpl.so.6.0.0", "sha256"),
            "baseline-library-instead-of-candidate",
        ),
        (
            "native-processes-stop.json",
            ("units", "controller", "properties", "MainPID"),
            "99999",
        ),
        (
            "result.json",
            ("native_shutdown", "em-baseline-controller", 0, "after", "ExecMainStatus"),
            "6",
        ),
        (
            "post-restoration-observation.json",
            ("actual_restored_files", "/opt/prpl-install-nl80211/lib/libbpl.so.6.0.0"),
            "wrong-restored-library",
        ),
        ("result.json", ("active_observed_seconds",), 899.9),
    ],
)
def test_lifecycle_rejects_false_success(tmp_path, filename, keys, value):
    for name in FILES:
        shutil.copyfile(EVIDENCE / name, tmp_path / name)
    path = tmp_path / filename
    data = json.loads(path.read_text())
    item = data
    for key in keys[:-1]:
        item = item[key]
    item[keys[-1]] = value
    path.write_text(json.dumps(data))
    with pytest.raises(AssertionError):
        CHECK(tmp_path)


@pytest.mark.parametrize(
    "case", ["unknown_patch", "wrong_digest", "missing_library", "not_restored"]
)
def test_candidate_admission_rejects_unqualified_variants(case):
    build = json.loads((EVIDENCE / "candidate.json").read_text())
    trial = json.loads((EVIDENCE / "candidate-trial.json").read_text())
    if case == "unknown_patch":
        build["extra_patches"][-1]["name"] = "unknown.patch"
    elif case == "wrong_digest":
        build["extra_patches"][-1]["sha256"] = "0" * 64
    elif case == "missing_library":
        build["runtime_libraries"] = {}
    else:
        trial["runtime_library_restoration"]["libbpl.so.6.0.0"]["restored"] = False
    with pytest.raises(AssertionError):
        CANDIDATE(build, trial)
