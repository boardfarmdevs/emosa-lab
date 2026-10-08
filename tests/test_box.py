# SPDX-License-Identifier: Apache-2.0
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


def box(scenario, agent, directory, timeout=180):
    command = [sys.executable, "-m", "emosa_lab.box", scenario, "--agent", agent]
    # the C agent's directory also has the C fleet and GTP (a takeover runs both fleets)
    command += ["--directory", str(directory), "--binary", str(C_AGENT)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr + _log(directory)
    return json.loads(result.stdout)


def _log(directory):
    logs = [Path(directory) / name for name in ("agent.log", "fleet.log", "gtp.log")]
    logs += sorted((Path(directory) / "units").glob("*.log"))
    return "".join(f"\n--- {p.name}\n{p.read_text()[-2000:]}" for p in logs if p.exists())


def unavailable(reason):
    # CI sets EMOSA_BOX_REQUIRED: there a missing prerequisite fails instead of skipping
    if os.environ.get("EMOSA_BOX_REQUIRED"):
        pytest.fail(reason)
    pytest.skip(reason)


# CI runs the two agents in parallel jobs: EMOSA_BOX_AGENTS=python or c
AGENTS = os.environ.get("EMOSA_BOX_AGENTS", "python,c").split(",")


@pytest.fixture(params=AGENTS)
def agent(request):
    if not shutil.which("unshare"):
        unavailable("unshare is required")
    if request.param == "c" and not C_AGENT.exists():
        unavailable(f"no C agent at {C_AGENT}: cmake -S c -B c/build && cmake --build c/build")
    return request.param


def test_the_agent_takes_the_pod_and_searches_for_its_controller(agent, tmp_path):
    result = box("boot", agent, tmp_path / "box")
    assert result["passed"]
    assert result["status_linked"]  # the status in the run directory (spec 6)
    assert "0x0007" in result["messages"]  # AP-Autoconfiguration Search
    assert "0x0000" in result["messages"]  # Topology Discovery, on start (spec 2.4)
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
    assert result["ignored"]  # a broadcast query and a runt: no answer, the agent runs on
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


# Onboarding and the session's own rules (spec 2.5); the suite findings among them


def test_the_early_report_is_retried_three_times_and_stops_at_its_ack(agent, tmp_path):
    result = box("early-report", agent, tmp_path / "box")
    # three at most, each with a new MID (a loop busy at start may fit fewer in its 1 s)
    assert 2 <= result["unacknowledged_transmissions"] <= 3, result
    assert result["distinct_mids"] == result["unacknowledged_transmissions"]
    assert result["acknowledged_transmissions"] == 1, result


def test_a_renew_starts_onboarding_again_at_once(agent, tmp_path):
    result = box("renew", agent, tmp_path / "box")
    assert result["passed"], result
    assert result["search_after_s"] < 3


def test_an_m1_left_unanswered_starts_onboarding_again_after_30_s(agent, tmp_path):
    result = box("no-m2", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_silent_controller_starts_onboarding_again_after_130_s(agent, tmp_path):
    result = box("silent-controller", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_controller_that_forgot_the_agent_has_it_onboard_again(agent, tmp_path):
    result = box("forgotten-agent", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["stayed_while_queried"], result


def test_an_agent_with_its_interface_in_a_namespace_of_its_own_onboards(agent, tmp_path):
    result = box("agent-netns", agent, tmp_path / "box")
    assert result["passed"], result
    assert not result["al_mac_in_own_namespace"], result


def test_a_pod_serving_none_of_the_controllers_bsses_asks_again_after_60_s(agent, tmp_path):
    result = box("unserved-pod", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_pod_back_with_a_new_database_is_onboarded_on_it(agent, tmp_path):
    result = box("new-source", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_windows_rows_left_in_the_pod_are_closed_and_its_station_steered_again(agent, tmp_path):
    result = box("steering-leftover", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_pod_recreated_with_another_radio_is_onboarded_on_it(agent, tmp_path):
    result = box("pod-recreated", agent, tmp_path / "box")
    assert result["passed"], result


# Reports (spec 2.4, 2.6)


def test_a_client_joining_and_leaving_is_notified_and_aged_per_response(agent, tmp_path):
    result = box("clients", agent, tmp_path / "box")
    assert result["joined"] and result["left"], result
    assert result["passed"], result


def test_the_controllers_next_topology_query_reannounces_every_client_once(agent, tmp_path):
    result = box("reannounce", agent, tmp_path / "box")
    assert result["passed"], result


def test_channel_selection_is_accepted_or_declined_and_the_channel_reported(agent, tmp_path):
    result = box("channel-selection", agent, tmp_path / "box")
    assert result["codes"] == {"accepted": 0, "declined": 2}, result
    assert result["passed"], result


def test_a_channel_scan_is_acknowledged_and_reported_not_supported(agent, tmp_path):
    result = box("channel-scan", agent, tmp_path / "box")
    assert result["passed"], result


# The pod's statistics through a broker in the box (spec 3.6, 3.8, 3.9)


def test_the_pods_statistics_are_set_up_and_reach_the_agent(agent, tmp_path):
    result = box("telemetry", agent, tmp_path / "box")
    assert result["passed"], result


def test_another_managers_broker_is_not_taken_over(agent, tmp_path):
    result = box("foreign-broker", agent, tmp_path / "box")
    assert result["passed"], result


def test_ap_metrics_come_from_the_pods_statistics_on_query_and_interval(agent, tmp_path):
    result = box("metrics", agent, tmp_path / "box")
    assert result["passed"], result


def test_unassociated_stations_are_watched_and_reported_when_heard(agent, tmp_path):
    result = box("unassociated", agent, tmp_path / "box")
    assert result["passed"], result


# Client steering (spec 3.7)


def test_a_steering_mandate_opens_kicks_and_closes_its_window(agent, tmp_path):
    result = box("steering", agent, tmp_path / "box")
    assert result["passed"], result


def test_steering_requests_the_agent_does_not_carry_out(agent, tmp_path):
    result = box("steering-refusals", agent, tmp_path / "box")
    assert result["passed"], result


# The pod's Wi-Fi backhaul (spec 8.3)


def test_the_backhaul_sta_capability_names_the_pods_backhaul_station(agent, tmp_path):
    result = box("backhaul-capability", agent, tmp_path / "box")
    assert result["passed"], result


def test_backhaul_steering_moves_the_station_and_answers_success(agent, tmp_path):
    result = box("backhaul-steering", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_backhaul_steering_answer_is_kept_across_a_renewal(agent, tmp_path):
    result = box("backhaul-steering-renewal", agent, tmp_path / "box")
    assert result["passed"], result


def test_backhaul_steering_onto_the_pods_own_bss_is_refused(agent, tmp_path):
    result = box("backhaul-steering-own-bss", agent, tmp_path / "box")
    assert result["passed"], result


def test_backhaul_steering_without_option_1_is_refused_at_once(agent, tmp_path):
    result = box("backhaul-steering-refused", agent, tmp_path / "box")
    assert result["passed"], result


# The reference workload's faults


def test_a_restarted_agent_takes_the_pod_back_without_writing_again(agent, tmp_path):
    result = box("adapter-restart", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_cut_pod_connection_is_onboarded_again_when_the_pod_returns(agent, tmp_path):
    result = box("transport-cut", agent, tmp_path / "box")
    assert result["passed"], result


# -- the adapter around the agents (plan 8.3): the fleet and the GTP, either implementation


def adapter(agent, *programs):
    """The C programs next to the C agent (built with it), for the C runs; and for a
    takeover, also when the other implementation is the C."""
    for program in programs:
        if not (C_AGENT.parent / program).exists():
            unavailable(f"no {program} next to {C_AGENT}: cmake --build c/build")


def test_a_pod_handed_to_the_fleet_gets_its_agent_and_is_onboarded(agent, tmp_path):
    if agent == "c":
        adapter(agent, "emosa-fleet-c")
    result = box("fleet-handover", agent, tmp_path / "box")
    assert result["passed"], result
    assert result["units"][:2] == ["enable", "start"]
    assert result["entry"]["interface"] == "em1"


def test_a_returning_pod_gets_the_same_agent_never_restarted(agent, tmp_path):
    if agent == "c":
        adapter(agent, "emosa-fleet-c")
    result = box("fleet-return", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert "restart" not in result["units"] and result["same_process"]


def test_a_started_fleet_starts_its_registrys_agents_before_any_pod_returns(agent, tmp_path):
    if agent == "c":
        adapter(agent, "emosa-fleet-c")
    result = box("fleet-upgrade", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["units_after_start"] == ["enable", "start"], result


def test_pods_the_fleet_must_not_take_are_left_unchanged(agent, tmp_path):
    if agent == "c":
        adapter(agent, "emosa-fleet-c")
    result = box("fleet-refusals", agent, tmp_path / "box")
    assert result["passed"], result
    assert result["units"] == [] and f"{result['serial']}.json" not in result["configs"]


def test_either_fleet_takes_over_from_the_other(agent, tmp_path):
    adapter(agent, "emosa-fleet-c")  # the C fleet runs first or second
    result = box("fleet-takeover", agent, tmp_path / "box")
    assert result["passed"], result
    assert result["listed_alike"] and result["same_process"]


def test_a_released_pod_is_archived_and_handed_over_anew(agent, tmp_path):
    if agent == "c":
        adapter(agent, "emosa-fleet-c")
    result = box("fleet-forget", agent, tmp_path / "box")
    assert result["passed"], result
    assert result["archived"].startswith(result["released"] + ".released-")


def test_a_fleet_without_systemd_supervises_its_agents(agent, tmp_path):
    adapter(agent, "emosa-fleet-c")  # the C fleet supervises either agent
    result = box("fleet-supervised", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_the_gtp_keeps_one_gretap_per_lease_in_the_lan_bridge(agent, tmp_path):
    if agent == "c":
        adapter(agent, "emosa-gtp-c")
    result = box("gtp", agent, tmp_path / "box")
    assert result["passed"], result


# -- the features' faults and refusals (plan 8.4)


def test_an_m2_set_writes_both_bsses_and_its_backhaul_serves_the_uplink(agent, tmp_path):
    result = box("multi-bss", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["backhaul_bss"] == [["b-ap-24", "backhaul_bss"]]


def test_a_set_beyond_the_radios_slots_is_applied_in_part(agent, tmp_path):
    result = box("set-beyond-slots", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["access_points"] == [["home-ap-24", "EMOSA-WSC-component"]]


def test_a_lost_write_the_pod_never_applies_times_out_and_frees_the_pod(agent, tmp_path):
    result = box("write-lost-unapplied", agent, tmp_path / "box", timeout=420)
    assert result["passed"], result
    assert result["reply_cut"]


def test_each_bss_written_carries_its_m2_role_in_multi_ap(agent, tmp_path):
    result = box("fronthaul-role", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_fronthaul_created_on_a_cold_pod_carries_its_m2_role(agent, tmp_path):
    result = box("fronthaul-role-cold", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert [r[0] for r in result["roles"]] == ["home-ap-24"]


def test_a_wired_pods_ethernet_uplink_is_bridged_into_br_home(agent, tmp_path):
    result = box("wired-uplink", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_kept_backhaul_target_that_is_gone_falls_back_to_the_configured_one(agent, tmp_path):
    result = box("backhaul-kept-gone", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_an_uplink_switch_never_confirmed_holds_the_pod(agent, tmp_path):
    result = box("uplink-held", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result
    assert result["held"]["reason"] == "switch not confirmed within the deadline"


def test_another_managers_change_to_the_switched_station_holds_the_pod(agent, tmp_path):
    result = box("uplink-foreign-change", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_moved_upstream_is_kept_when_the_agent_starts_again(agent, tmp_path):
    result = box("backhaul-steering-kept", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_the_agent_subscribes_again_when_the_broker_returns(agent, tmp_path):
    result = box("telemetry-broker-restart", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_another_managers_steering_rows_are_left_alone(agent, tmp_path):
    result = box("steering-conflict", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_steering_window_owm_never_takes_closes_at_its_deadline(agent, tmp_path):
    result = box("steering-window-expired", agent, tmp_path / "box")
    assert result["passed"], result
    assert result["outcome"] == "not_applied"


def test_a_steering_window_left_open_is_closed_after_the_agent_restarts(agent, tmp_path):
    result = box("steering-restart", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
