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


def test_the_backhaul_sta_capability_names_each_backhaul_station_radio(agent, tmp_path):
    result = box("backhaul-capability", agent, tmp_path / "box")
    assert result["passed"], result


def test_the_fleet_serves_a_modules_file_by_its_sha256_with_the_date(agent, tmp_path):
    # spec 4, the pods' modules: each implementation's fleet, its file server and the settings
    result = box("fleet-files", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_an_invalid_fleet_configuration_is_rejected_and_the_last_valid_one_served(agent, tmp_path):
    # spec 4 (15 October): one pod's unknown key kept every pod off EMOSA, the fleet restarting
    result = box("fleet-config-rejected", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_pods_module_gets_its_settings_and_its_report_is_read(agent, tmp_path):
    # spec 4, the pods' modules: Node_Config written once per start, Node_State in the status
    result = box("node-config", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_wired_pods_backhaul_stations_all_off_are_left_alone(agent, tmp_path):
    # spec 8.4, 8.3: the Pis' bootstrap, a station row per band and none on a wired pod
    result = box("wired-stations-off", agent, tmp_path / "box")
    assert result["passed"], result


def test_backhaul_steering_moves_the_station_and_answers_success(agent, tmp_path):
    result = box("backhaul-steering", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_steer_onto_a_parent_and_back_reverts_to_the_bootstrap(agent, tmp_path):
    result = box("revert-by-record", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_pod_restart_during_a_move_is_reapplied_without_a_steer(agent, tmp_path):
    result = box("restart-during-move", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_gre_move_and_its_revert_leave_the_fallback_station_untouched(agent, tmp_path):
    result = box("fallback-during-move", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_gre_childs_move_is_reapplied_after_its_pod_restarts(agent, tmp_path):
    result = box("gre-child-kept", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_gre_childs_kept_parent_gone_is_dropped_without_a_hold(agent, tmp_path):
    result = box("gre-child-kept-gone", agent, tmp_path / "box", timeout=420)
    assert result["passed"], result


def test_a_gre_child_left_up_after_its_kept_parent_failed_is_undone(agent, tmp_path):
    result = box("gre-child-kept-gone-up", agent, tmp_path / "box", timeout=420)
    assert result["passed"], result


def test_a_radio_power_above_its_advertised_maximum_is_taken_once(agent, tmp_path):
    result = box("power-rises", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_renew_on_the_bootstrap_with_a_dropped_station_is_one_session(agent, tmp_path):
    result = box("renew-on-bootstrap", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_write_its_pod_restarted_under_does_not_hold_the_next_start(agent, tmp_path):
    result = box("restart-during-write", agent, tmp_path / "box", timeout=300)
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


def test_backhaul_steering_to_another_band_moves_the_uplink_to_that_bands_station(agent, tmp_path):
    result = box("backhaul-steering-across-bands", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_move_to_another_band_not_applied_returns_to_the_previous_station(agent, tmp_path):
    result = box("backhaul-steering-across-bands-fails", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_backhaul_steering_to_a_band_without_a_station_is_refused_with_0x04(agent, tmp_path):
    result = box("backhaul-steering-no-station-on-band", agent, tmp_path / "box")
    assert result["passed"], result


def test_backhaul_steering_off_the_bss_radios_channel_is_refused_with_0x04(agent, tmp_path):
    result = box("backhaul-steering-channel-not-operable", agent, tmp_path / "box")
    assert result["passed"], result


# The reference workload's faults


def test_a_restarted_agent_takes_the_pod_back_without_writing_again(agent, tmp_path):
    result = box("adapter-restart", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_pod_connection_cut_for_20_s_keeps_its_session(agent, tmp_path):
    # spec 2.5: a backhaul move's reconnection, the session held and kept
    result = box("transport-cut", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_pod_connection_cut_past_the_reconnection_window_is_onboarded_again(agent, tmp_path):
    result = box("transport-cut-long", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_pod_gone_without_a_fin_is_noticed_by_the_echo_probe(agent, tmp_path):
    # spec 3.1: the pod's database frozen, its connection open; the agent's probe drops it
    result = box("pod-vanished", agent, tmp_path / "box", timeout=180)
    assert result["passed"], result


def test_a_move_with_the_pods_link_cut_keeps_the_session_and_reports_the_new_parent(
    agent, tmp_path
):
    # spec 2.5: the Topology Notification, and the next Topology Response names the new parent
    result = box("reconnect-moved-uplink", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_session_not_yet_provisioned_is_not_held_across_a_cut(agent, tmp_path):
    result = box("reconnect-not-provisioned", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_fronthaul_channel_move_is_a_fresh_session_not_a_refusal(agent, tmp_path):
    # spec 2.5: the Pis' 2.4 GHz fallback, the fronthaul following its station's channel
    result = box("fronthaul-channel-moves", agent, tmp_path / "box")
    assert result["passed"], result


def test_a_kept_reconnection_without_its_configuration_onboards_again_at_once(agent, tmp_path):
    # spec 2.5's safety: the configuration gone under the same start, the fresh session's M2
    result = box("reconnect-config-lost", agent, tmp_path / "box")
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


def test_a_pod_under_another_pod_names_it_its_backhaul_neighbor(agent, tmp_path):
    result = box("pod-child", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_pod_with_a_child_pod_names_it_on_its_backhaul_bss(agent, tmp_path):
    result = box("pod-parent", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_backhaul_move_under_a_child_pod_is_refused(agent, tmp_path):
    result = box("pod-parent-loop", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_gre_parents_backhaul_bss_is_written_as_its_parent_ap(agent, tmp_path):
    result = box("gre-parent-create", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_gre_parent_builds_a_tunnel_to_a_child_that_leased_in_its_underlay(agent, tmp_path):
    result = box("gre-parent-child", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_gre_parent_removes_a_tunnel_when_its_child_is_gone(agent, tmp_path):
    result = box("gre-parent-child-gone", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_gre_parent_re_creates_a_childs_tunnel_that_drifted_away(agent, tmp_path):
    result = box("gre-parent-child-redrift", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_withdrawn_backhaul_bss_removes_the_parent_ap_and_every_tunnel(agent, tmp_path):
    result = box("gre-parent-withdrawn", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_parent_ap_on_the_second_radio_comes_from_that_radios_own_m2(agent, tmp_path):
    # one M1 and one M2 per radio; b-ap-50 written by the 5 GHz radio's own scope (spec 9)
    result = box("second-radio-parent", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["m1_ruids"][:2] == ["02:00:00:00:01:00", "02:00:00:00:05:00"]


def test_a_second_radios_set_without_its_backhaul_bss_removes_only_its_parent_ap(agent, tmp_path):
    # withdrawal by band (spec 8.6): the 5 GHz radio's scope, never the 2.4 GHz radio's
    result = box("second-radio-withdrawn", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result
    assert result["fronthaul_unchanged"]


def test_a_refused_close_is_swept_again_on_the_same_source(agent, tmp_path):
    result = box("steering-close-refused", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


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


def test_a_held_pod_switches_again_after_its_backoff(agent, tmp_path):
    result = box("uplink-held-retry", agent, tmp_path / "box", timeout=500)
    assert result["passed"], result


def test_another_managers_change_to_the_switched_station_holds_the_pod(agent, tmp_path):
    result = box("uplink-foreign-change", agent, tmp_path / "box")
    assert result["passed"], result


@pytest.mark.parametrize("parent", ["empty", "other"])
def test_a_switch_to_the_bss_in_use_stays_applied_whatever_the_vif_parent(agent, parent, tmp_path):
    result = box(f"uplink-reswitch-parent-{parent}", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_pod_started_again_mid_switch_holds_it_as_a_failed_switch(agent, tmp_path):
    # rdk-1004, 9 October: lab.sh up stopped on pod-1's uplink APPLY_TIMEOUT (spec 8.3)
    result = box("uplink-instance-change", agent, tmp_path / "box", timeout=400)
    assert result["passed"], result
    assert result["held"]["reason"] == "switch not confirmed within the deadline"


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


def test_a_second_radios_channel_move_keeps_the_session_and_leaves_its_parent_ap(agent, tmp_path):
    # spec 9's channel rule and 2.5, V54's radio B on 149: the station moves the radio, the
    # agent keeps its session and reports the new operable channel (a Channel Preference Report
    # and an Operating Channel Report), and b-ap-50 is the pod's to move, not EMOSA's to rewrite
    result = box("second-radio-moves-channel", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result
    assert result["sessions"] == 0 and all(result["unchanged"].values())


def test_a_move_with_the_pods_link_cut_keeps_the_session_as_its_ap_follows_the_station(
    agent, tmp_path
):
    # spec 2.5: the station's new parent on 149 with the pod's link cut around it (the Pis'
    # radio B: the AP restarts on the station's channel), the session kept, the channel reported
    result = box("reconnect-channel-follows", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result
    assert result["sessions"] == 0 and result["kept_after_cut"]


def test_a_pod_promoted_to_gre_parent_while_onboarded_converts_its_backhaul_bss(agent, tmp_path):
    # finding A (spec 8.6): the same b-ap-24 row becomes the parent AP, then a Renew is a no-op
    result = box("gre-parent-promoted", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result
    assert result["same_row"] and result["renew_noop"]


def test_a_demoted_gre_parent_leaves_no_dhcp_server_or_tunnel_on_its_backhaul(agent, tmp_path):
    # spec 8.6, the mirror of finding A: no underlay address, DHCP server or tunnel is left
    result = box("gre-parent-demoted", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result
    assert result["same_row"] and result["own_rows_unchanged"] and not result["dhcpd"]


def test_a_5ghz_gre_childs_backhaul_credentials_come_from_its_5ghz_radios_set(agent, tmp_path):
    # spec 8.6, 9 (the rdk-1010 proof): the fronthaul's set has no backhaul BSS on the Pis' shape
    result = box("second-radio-child-credentials", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["waiting"].startswith("no upstream")


def test_a_radio_with_no_reported_power_still_answers_the_channel_procedures(agent, tmp_path):
    # spec 9 (rdk-1010): refused, RDK re-sent its query and renewed the pod every minute
    result = box("second-radio-no-power", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["not_ready_refusals"] == 0 and result["bound_counted"]


def test_a_legacy_pods_fronthaul_is_written_in_its_security_map(agent, tmp_path):
    # spec 3.10: OpenSync 2.0's schema, no wpa_* columns or multi_ap
    result = box("legacy-onboard", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_legacy_pods_ssid_and_key_write_ends_in_a_full_auth_reload(agent, tmp_path):
    # spec 3.10 (15 October): on a hooked boot the SSID and key alone are a fast reload of the
    # pods' authenticator; EMOSA's write ends in full ones
    result = box("legacy-auth-reload", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["without_marker"] == "fast" and result["reloads"][-1] == ["full", "home--1"]


def test_a_6_6_pods_write_has_no_auth_reload(agent, tmp_path):
    result = box("no-auth-reload-6-6", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_legacy_ap_the_agent_makes_gets_its_bridge_port(agent, tmp_path):
    # spec 3.10 (15 October): the converted pods' clients associated and got no EAPOL M1 after a
    # hooked boot: their home APs were in no Open vSwitch port of br-home, which the pod never adds
    result = box("legacy-bridge-port", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["port"] == [True, True] and result["served"] is True


def test_a_live_legacy_ap_without_its_port_is_repaired_without_a_restart(agent, tmp_path):
    result = box("legacy-bridge-port-repair", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["reloads"][-2:] == [["full", "home--1~reload"], ["full", "home--1"]]


def test_a_hooked_boots_pod_gets_the_bridge_mode_its_cloud_runs(agent, tmp_path):
    # spec 3.10 (15 October): after a hooked boot br-home's interface was down (its Inet row
    # network false), so the authenticator heard no client, and the pod was not a bridge
    result = box("legacy-bridge-mode", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["bridge_mode"]["dhcp"] is True and result["served"] is True


def test_a_live_pods_bridge_mode_is_repaired_without_a_restart(agent, tmp_path):
    result = box("legacy-bridge-mode-repair", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["reloads"][-2:] == [["full", "home--1~reload"], ["full", "home--1"]]


def test_the_anchor_ap_experiment_writes_its_own_hidden_ap_and_removes_it(agent, tmp_path):
    # spec 3.10 (15 October): the fleet's anchor_ap on, EMOSA's hidden AP on wl0.1; off, gone
    result = box("legacy-anchor-ap", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_an_agent_on_an_older_profile_still_sets_the_home_aps_rekey_and_ft(agent, tmp_path):
    # spec 3.10 (15 October): an agent binary bind-mounted on the router read the image's profile,
    # which had no group_rekey or ft_psk, and set neither on the home APs
    result = box("legacy-bridge-mode-old-profile", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
    assert result["bridge_mode"]["vif"] == [86400, 0]


def test_a_legacy_pods_backhaul_bss_is_its_gre_parent_ap_on_its_underlay(agent, tmp_path):
    # spec 3.10, 8.6: the former manager's 169.254.2.1 on wl1.1 replaced by the pod's underlay
    result = box("legacy-parent", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_legacy_pod_under_a_6_6_profile_is_refused(agent, tmp_path):
    result = box("legacy-pod-6-6-profile", agent, tmp_path / "box", timeout=120)
    assert result["passed"], result


def test_a_6_6_pod_under_a_legacy_profile_is_refused(agent, tmp_path):
    result = box("pod-6-6-legacy-profile", agent, tmp_path / "box", timeout=120)
    assert result["passed"], result


def test_a_wired_legacy_pods_uplink_is_bridged_by_the_patch_pair(agent, tmp_path):
    # spec 3.10, 8.4: no Connection_Manager_Uplink.bridge on a 2.x pod
    result = box("legacy-wired", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_half_a_patch_pair_is_another_managers_and_left_alone(agent, tmp_path):
    result = box("legacy-wired-half-pair", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_legacy_gre_child_is_moved_by_its_stations_parent_pin(agent, tmp_path):
    # spec 3.10, 8.6: no Wifi_Credential_Config.bssid on a 2.x pod
    result = box("legacy-gre-move", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_legacy_pod_losing_its_ethernet_keeps_its_session_on_the_gtp(agent, tmp_path):
    # spec 3.10, 8.2, 2.5: wired, its station already on the GTP's BSS, as first admitted
    result = box("legacy-ethernet-lost", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_legacy_gre_child_losing_its_ethernet_keeps_the_gtp_as_its_bootstrap(agent, tmp_path):
    # the same with uplink mode gre: the station linked beside the agent is not its own move
    result = box("legacy-ethernet-lost-gre", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_seeded_legacy_pods_gtp_station_is_written_and_carries_its_uplink(agent, tmp_path):
    # spec 3.10, the GTP station: no 2.x core makes one; written once, then the Ethernet lost
    result = box("legacy-gtp-station", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_legacy_station_already_there_is_left_alone(agent, tmp_path):
    # spec 3.10: the pod's, an operator's or a start-up hook's row; nothing written
    result = box("legacy-gtp-station-present", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_an_unassociated_gtp_station_is_removed_before_the_healthcheck_reboots_the_pod(
    agent, tmp_path
):
    # spec 3.10, the station's guard: removed unassociated, written again after the back-off
    result = box("legacy-gtp-station-unassociated", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_converted_pods_seed_stations_go_and_only_its_gtp_station_stays(agent, tmp_path):
    # spec 3.10: the seed's onboarding stations deleted while wired, the GTP station written
    result = box("legacy-seed-stations", agent, tmp_path / "box", timeout=360)
    assert result["passed"], result


def test_a_converted_pods_dark_idle_radio_is_turned_on_so_its_aps_beacon(agent, tmp_path):
    # spec 3.10: every enabled AP beacons; wl0, neither bound nor placed, on at 36 when dark
    result = box("legacy-dark-radio", agent, tmp_path / "box", timeout=360)
    assert result["passed"], result


def test_an_unwired_converted_pods_unassociated_seed_station_is_removed(agent, tmp_path):
    # spec 3.10: the station guard over every station row of a converted pod
    result = box("legacy-seed-station-unwired", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_converted_pods_operator_cloud_is_cleared_on_every_start(agent, tmp_path):
    # spec 3.10, a converted pod talks only to EMOSA: qm's broker and the statistics rows gone
    result = box("legacy-cloud-cleared", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_legacy_pods_radio_plan_is_emosas_once_per_start(agent, tmp_path):
    # spec 3.10, radio_plan emosa: wl0.1's Inet off, wl1 on 6; no AP disabled, stations untouched
    result = box("legacy-radio-plan", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_reseeded_legacy_pod_is_claimed_again_without_a_conflict(agent, tmp_path):
    # spec 3.10: its seed's radio rows keep their UUIDs; the agent's fresh session is a new start
    result = box("legacy-reseed-reclaim", agent, tmp_path / "box", timeout=360)
    assert result["passed"], result


def test_a_reseeded_legacy_pod_gets_its_5ghz_fronthaul_back_on_36(agent, tmp_path):
    # the same on bcm-pod-2.4.3-gre-5g-v1: wl0 on 36 at HT80 again, wl0.2 its home AP
    result = box("legacy-reseed-reclaim-5g", agent, tmp_path / "box", timeout=360)
    assert result["passed"], result


def test_a_legacy_pod_reseeded_past_the_window_is_claimed_again(agent, tmp_path):
    # the same with the pod away past the reconnection window, as after a reboot
    result = box("legacy-reseed-reclaim-late", agent, tmp_path / "box", timeout=420)
    assert result["passed"], result


def test_a_legacy_pod_switched_to_the_5ghz_profile_takes_wl0_without_a_conflict(agent, tmp_path):
    # plan A: its entry switched to bcm-pod-2.4.3-gre-5g-v1, its agent restarted on its journal
    result = box("legacy-profile-5g", agent, tmp_path / "box", timeout=300)
    assert result["passed"], result


def test_a_legacy_parent_tunnels_a_child_into_br_home(agent, tmp_path):
    # spec 3.10, 8.6: a legacy pod as another pod's GRE parent, its tunnel in the 2.0 schema
    result = box("legacy-parent-child", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_legacy_parent_removes_a_gone_childs_tunnel(agent, tmp_path):
    # the same, the child gone: its tunnel and br-home port removed
    result = box("legacy-parent-child-gone", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result


def test_a_legacy_pods_5ghz_radio_serves_its_own_fronthaul(agent, tmp_path):
    # spec 3.10, 9: bcm-pod-2.4.3-gre-5g-v1, the backhaul staying on 2.4 GHz
    result = box("legacy-5g-fronthaul", agent, tmp_path / "box", timeout=240)
    assert result["passed"], result
