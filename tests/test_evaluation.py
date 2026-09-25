import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from emosa.config import load, validate
from emosa.errors import EmosaError
from emosa.store import Store
from emosa_lab.evaluation.cli import main
from emosa_lab.evaluation.evidence import compare, html_report
from emosa_lab.evaluation.runner import run

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("path", sorted(Path("scenarios").glob("*.json")))
def test_scenario_contracts(path):
    load("scenario", path)


def test_unknown_configuration_and_request_fields_rejected():
    with pytest.raises(EmosaError):
        validate(
            "local-api",
            {
                "schema_version": 1,
                "request_id": "r",
                "method": "status",
                "params": {"password": "do-not-echo"},
            },
        )
    with pytest.raises(EmosaError) as exc:
        validate(
            "local-api", {"schema_version": 2, "request_id": "r", "method": "status", "params": {}}
        )
    assert "do-not-echo" not in str(exc.value)
    with pytest.raises(EmosaError):
        validate("local-api", {"schema_version": 1, "request_id": "r", "result": {}, "error": {}})


def test_runner_blocked_wire_has_no_semantic_fallback(tmp_path):
    scenario = load("scenario", "scenarios/provision-one-bss.json")
    result = asyncio.run(run(scenario, backend="model", root=tmp_path))
    assert result["execution_status"] == result["verdict"] == "blocked"
    assert result["coverage"]["passing"] == 0 and result["operations"] == []
    assert any(p["gate"] == "P0" for p in result["prerequisites"])
    assert result["manifest"]["initiating_interface"] == "easymesh-wire"
    validate("run-result", result)


def test_model_repetition_evidence_comparison_and_immutability(tmp_path):
    scenario = load("scenario", "scenarios/component-bss-change.json")
    a = asyncio.run(run(scenario, backend="model", root=tmp_path))
    b = asyncio.run(run(scenario, backend="model", root=tmp_path))
    assert a["verdict"] == b["verdict"] == "pass"
    assert a["run_id"] != b["run_id"]
    phases = []
    for result in (a, b):
        directory = tmp_path / "runs" / result["run_id"]
        events = json.loads((directory / "events.json").read_text())
        phases.append([e["phase"] for e in events])
        manifest = json.loads((directory / "artifact-manifest.json").read_text())
        for item in manifest["artifacts"]:
            assert (
                hashlib.sha256((directory / item["path"]).read_bytes()).hexdigest()
                == item["sha256"]
            )
        store = Store(directory / "state")
        try:
            with pytest.raises(EmosaError, match="immutable"):
                store.save_run(result)
        finally:
            store.close()
    assert phases[0] == phases[1]
    changed = copy.deepcopy(b)
    changed["manifest"]["scenario"]["intent"]["ssid"] = "new-input"
    assert any(d["field"] == "scenario.intent.ssid" for d in compare(a, changed)["differences"])
    malicious = copy.deepcopy(a)
    malicious["scenario_id"] = "<script>alert(1)</script>"
    assert "<script>" not in html_report(malicious, [])
    assert (
        main(
            [
                "--state-dir",
                str(tmp_path),
                "inspect",
                a["run_id"],
                "--operation",
                a["operations"][0]["operation_id"],
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("component-lost-reply", "OBSERVED_APPLIED"),
        ("application-rejection", "TIMED_OUT"),
        ("competing-writer", "OWNERSHIP_CONFLICT"),
        ("multi-pod-isolation", "TIMED_OUT"),
        ("controller-restart", "OBSERVED_APPLIED"),
    ],
)
def test_model_scenarios(tmp_path, filename, expected):
    result = asyncio.run(
        run(load("scenario", f"scenarios/{filename}.json"), backend="model", root=tmp_path)
    )
    assert result["verdict"] == "pass", result
    assert result["operations"][0]["state"] == expected
    assert result["interoperability_verdict"] == "not_evaluated"


def test_late_model_application_does_not_pass_original_deadline(tmp_path):
    scenario = load("scenario", "scenarios/component-lost-reply.json")
    scenario["fault"]["duration_seconds"] = 2
    scenario["deadlines"]["apply_seconds"] = 1
    result = asyncio.run(run(scenario, backend="model", root=tmp_path))
    assert result["execution_status"] == "completed" and result["verdict"] == "fail"
    assert result["operations"][0]["deadline_elapsed"]
    assert result["operations"][0]["late_resolution"] == "applied_after_deadline"


def test_missing_required_evidence_and_wrong_fault_boundary_do_not_pass(tmp_path):
    scenario = load("scenario", "scenarios/component-bss-change.json")
    scenario["evidence_required"].append("independent-client")
    result = asyncio.run(run(scenario, backend="model", root=tmp_path))
    assert result["verdict"] == "blocked" and not result["operations"]
    scenario["fault"]["point"] = "after-submit"
    with pytest.raises(EmosaError, match="boundary"):
        asyncio.run(run(scenario, backend="model", root=tmp_path))


def test_lxd_command_routes_only_named_endpoints():
    from emosa_lab.evaluation.lxd import endpoint_command

    assert endpoint_command("emosa", ["emosa", "status"]) == [
        "lxc",
        "exec",
        "emosa",
        "--",
        "emosa",
        "status",
    ]
    with pytest.raises(EmosaError):
        endpoint_command("some-existing-host-container", ["emosa", "status"])
