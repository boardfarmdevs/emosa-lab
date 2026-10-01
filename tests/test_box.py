"""The lab in a box (emosa_lab.box): each implementation's real agent binary against
the recorded pod and a scripted controller, in a private network namespace."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.box

ROOT = Path(__file__).resolve().parents[1]
C_AGENT = Path(os.environ.get("EMOSA_C_AGENT", ROOT / "c/build/emosa-agent-c"))


def box(scenario, agent, directory):
    command = [sys.executable, "-m", "emosa_lab.box", scenario, "--agent", agent]
    command += ["--directory", str(directory)]
    if agent == "c":
        command += ["--binary", str(C_AGENT)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr + _log(directory)
    return json.loads(result.stdout)


def _log(directory):
    log = Path(directory) / "agent.log"
    return log.read_text()[-2000:] if log.exists() else ""


def unavailable(reason):
    # CI sets EMOSA_BOX_REQUIRED: there a missing prerequisite fails instead of skipping
    if os.environ.get("EMOSA_BOX_REQUIRED"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(params=["python", "c"])
def agent(request):
    if not shutil.which("unshare"):
        unavailable("unshare is required")
    if request.param == "c" and not C_AGENT.exists():
        unavailable(f"no C agent at {C_AGENT}: cmake -S c -B c/build && cmake --build c/build")
    return request.param


def test_the_agent_takes_the_pod_and_searches_for_its_controller(agent, tmp_path):
    result = box("boot", agent, tmp_path / "box")
    assert result["passed"]
    assert "0x0007" in result["messages"]  # AP-Autoconfiguration Search
    assert result["exit"] is None  # still running when it was stopped


def test_an_easymesh_61_controller_onboards_the_pod(agent, tmp_path):
    # the Response, a real M2 from hostap's registrar for the agent's own M1, the
    # credentials written to the pod, applied by its managers, observed applied
    result = box("onboard", agent, tmp_path / "box")
    assert result["session"] == "provisioning"
    assert [op["state"] for op in result["operations"]] == ["OBSERVED_APPLIED"]
    assert "0x8043" in result["messages"]  # the Early AP Capability Report, before M1


def test_a_controller_without_controller_capability_is_refused(agent, tmp_path):
    result = box("refuse", agent, tmp_path / "box")
    assert result["session"] == "incompatible"
    assert result["admission_issues"] == ["controller_capability_absent"]
    assert "0x0009" not in result["messages"]  # no M1


def test_the_onboarded_agent_answers_the_controllers_requests(agent, tmp_path):
    # each request its reply, with the request's MID; the statistics-based two withheld,
    # their reason recorded, while the box has no telemetry (spec 3.8)
    result = box("answers", agent, tmp_path / "box")
    assert result["session"] == "provisioning"
    assert all(result["answered"].values()), result["answered"]
    assert set(result["answered"]) == {
        "topology",
        "ap_capability",
        "channel_preference",
        "client_capability",
        "backhaul_sta_capability",
        "unassociated_sta_metrics",
        "policy_config",
    }
    assert result["abstained"] == {
        "link_metric": "neighbor_measurement_unavailable",
        "ap_metrics": "ap_measurements_unavailable",
    }
    # both record the replies alike: the policy's ACK in the session's counts, the
    # Topology Response in the reports' counts
    assert result["recorded"] == {
        "policy_receipt_ack_sent": 1,
        "topology_response_sent": 1,
        "topology_response_in_session_counts": False,
    }
