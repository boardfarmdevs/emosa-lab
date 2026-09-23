"""Independent selected native-library lifetime and graceful-stop audit.

Checks observed process mappings, unrelaxed systemd stop policy, actual unit
exit results, build probe and byte-exact restoration. Recovery/traffic and
metrics use their separate capture audits; this is not full acceptance.
"""

import argparse
import json
from pathlib import Path


def check(directory):
    def read(name):
        return json.loads((directory / name).read_text())

    candidate = read("candidate.json")
    trial = read("candidate-trial.json")
    result = read("result.json")
    start, stop = read("native-processes-start.json"), read("native-processes-stop.json")
    reference = read("reference-candidate.json")
    lib = "libbpl.so.6.0.0"
    assert set(candidate["runtime_libraries"]) == {lib}
    hashes = candidate["runtime_libraries"][lib]
    assert hashes["baseline_sha256"] != hashes["candidate_sha256"]
    assert reference["runtime_libraries"] == {lib: hashes["candidate_sha256"]}
    assert trial["runtime_libraries"] == candidate["runtime_libraries"]
    assert trial["baseline_restored"] and trial["status"] == "experiment_finished"
    assert trial["runtime_library_restoration"] == {
        lib: {"restored_sha256": hashes["baseline_sha256"], "restored": True}
    }
    restoration = read("post-restoration-observation.json")
    assert restoration["owned_idle_check_passed"]
    files = restoration["actual_restored_files"]
    assert files["/opt/prpl-install-nl80211/lib/" + lib] == hashes["baseline_sha256"]
    assert files["/opt/prpl-install-nl80211/bin/beerocks_controller"] == trial["baseline_sha256"]
    assert (
        files["/opt/prpl-install-nl80211/bin/beerocks_agent"]
        == reference["binaries"]["beerocks_agent"]
    )
    assert (
        files["/opt/emosa-baseline/prplmesh.reference.json"]
        == restoration["reference_before_sha256"]
    )
    assert not result["cleanup_errors"]
    assert result["active_observed_seconds"] >= 900
    assert result["recovery_checks_requested"]
    assert (
        read("native-session.json")["reporting_policy"]["received_policy"][
            "periods_due_without_report"
        ]
        >= 15
    )
    assert candidate["counter_regression"]["passed"] == 12
    probe = candidate["lifetime_regression"]
    assert probe["baseline"]["loaded_library_sha256"] == hashes["baseline_sha256"]
    assert probe["candidate"]["loaded_library_sha256"] == hashes["candidate_sha256"]
    assert probe["baseline"]["observations"]["implicit"] == [
        "main_returning",
        "runtime_destroyed",
        "model_destroyed",
    ]
    assert probe["candidate"]["observations"]["implicit"] == [
        "main_returning",
        "model_destroyed",
        "runtime_destroyed",
    ]
    for row in probe.values():
        assert row["observations"]["explicit_clear"] == [
            "model_destroyed",
            "main_returning",
            "runtime_destroyed",
        ]
    assert start["observed_at"] < stop["observed_at"]
    assert stop["observed_at"] < restoration["observed_at"]
    outcomes = {item["unit"]: item for item in result["native_shutdown"]["em-baseline-controller"]}
    assert {"agent", "controller", "transport", "bus", "hostap"} <= outcomes.keys()
    for item in outcomes.values():
        assert item["after"] == {"Result": "success", "ExecMainStatus": "0"}
    records = {}
    for name in ("controller", "agent"):
        first, last = start["units"][name], stop["units"][name]
        binary = "beerocks_" + name
        assert (
            first["executable_sha256"] == last["executable_sha256"] == reference["binaries"][binary]
        )
        assert first["libraries"] == last["libraries"]
        assert first["libraries"][lib]["sha256"] == hashes["candidate_sha256"]
        for value in (first, last):
            props = value["properties"]
            assert props["ActiveState"] == "active" and int(props["MainPID"]) > 1
            assert props["KillSignal"] == "15" and props["KillMode"] == "control-group"
            assert props["TimeoutStopUSec"] == "20s" and props["SendSIGKILL"] == "yes"
            assert props["SuccessExitStatus"] == "" and props["Restart"] == "no"
        pid = first["properties"]["MainPID"]
        assert pid == last["properties"]["MainPID"] == outcomes[name]["before"]["MainPID"]
        assert outcomes[name]["before"]["ActiveState"] == "active"
        records[name] = {"pid": int(pid), "exit_status": 0, "result": "success"}
    return {
        "selected_native_lifecycle_checks_passed": True,
        "scope": "controller/helper main processes using the candidate Linux BPL library",
        "baseline_lifetime_failure_reproduced": True,
        "candidate_lifetime_order_verified": True,
        "stop_policy_unchanged": True,
        "native_exits": records,
        "runtime_libraries_restored": trial["runtime_library_restoration"],
        "active_seconds": result["active_observed_seconds"],
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
        "remaining": [
            "Qualified AP/STA reports",
            "Final disassociation statistics",
            "Complete integrated acceptance",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), indent=2))
