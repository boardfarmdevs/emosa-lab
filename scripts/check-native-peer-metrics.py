"""Independent native peer metrics: observed path/counters -> wire -> controller.

No EMOSA imports. Qualifies only the declared isolated software Ethernet profile,
not physical PHY, AP/STA reporting or complete sustained-operation acceptance.
"""

import argparse
import json
import runpy
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = runpy.run_path(str(ROOT / "scripts/check-native-onboarding.py"))
SHAPED = runpy.run_path(str(ROOT / "scripts/check-shaped-backhaul.py"))
read, lines = SHAPED["read"], SHAPED["lines"]


def check(directory):
    accounting = SHAPED["check"](directory, True)
    runtime = read(directory / "result.json")
    assert runtime["neighbor_metrics_requested"] and runtime["peer_path_restoration"]["restored"]
    for state in runtime["peer_path_restoration"]["offloads"]:
        assert state["before"] == state["after"]
    raw, _ = SHAPED["CAP"]["capture"](directory, "forwarding")
    events = lines(directory / "station-events.jsonl")
    offsets = [e["received_wall_ns"] - e["received_monotonic_ns"] for e in events]
    assert max(offsets) - min(offsets) < 1_000_000
    offset = sorted(offsets)[len(offsets) // 2] / 1e9
    manager = {}
    for event in lines(directory / "manager.jsonl"):
        forwarding = event.get("forwarding", {})
        if not forwarding.get("available"):
            continue
        meta = dict(forwarding["bridge_external_ids"][1])
        manager[int(meta["started_ns"])] = dict(forwarding["interfaces"]["eth1"]["external_ids"][1])
    published, contexts, generations, workers = [], set(), set(), set()
    observations = lines(directory / "forwarding-samples.jsonl")
    for row in observations:
        status = row["peer_link_metrics"]
        if not status or not status["available"]:
            continue
        sample, shaped = row["sample"], row["shaped_backhaul"]
        assert sample and shaped["available"]
        payload = sample["peer_path_observation"]
        assert payload == json.loads(manager[sample["started_ns"]]["peer_path_observation"])
        before, after = payload["before"], payload["after"]
        assert (
            before["descriptor"]
            == after["descriptor"]
            == runtime["peer_path_configuration"]["descriptor"]
        )
        assert before["configuration_epoch"] == after["configuration_epoch"]
        assert before["collector_epoch"] == after["collector_epoch"]
        descriptor = after["descriptor"]
        controller, pod = descriptor["controller"], descriptor["pod"]
        bound = row["observed_neighbor"]["binding"]
        assert bound["neighbor_interface"] == controller["address"]
        assert bound["local_interface"] == pod["address"] == sample["interfaces"]["eth1"]["mac"]
        for value in (before, after):
            assert (
                value["started_ns"]
                <= value["ended_ns"]
                <= row["observed_ns"]
                < value["started_ns"] + 2_000_000_000
            )
            assert value["ended_ns"] - value["started_ns"] <= 250_000_000
            assert len(value["ports"]) == 3
            assert {p["ifindex"] for p in value["ports"]} == {
                descriptor[k]["ifindex"] for k in ("pod_peer", "control_peer")
            } | {controller["link_index"]}
            for port in value["ports"]:
                state = port["linkinfo"]["info_slave_data"]
                assert state["isolated"] == (port["ifindex"] != controller["link_index"])
                assert state["state"] == "forwarding" and not state["hairpin"]
                assert value["vlans"][port["ifname"]] == [
                    {
                        "ifname": port["ifname"],
                        "vlans": [{"vlan": 1, "flags": ["PVID", "Egress Untagged"]}],
                    }
                ]
            assert not value["bridge"][0]["addr_info"]
            assert all(v["active"] is False for v in value["offloads"].values())
        feature = sample["egress_observation"]["observation"]["features"][0]
        assert all(
            v["active"] is False
            for k, v in feature.items()
            if any(x in k for x in ("segmentation", "receive-offload", "gso", "gro", "vlan"))
        )
        window, estimate = shaped["window"], shaped["service_estimate"]
        assert status["qualified_since_ns"] <= window["first_read_ns"][0]
        assert not any(
            f[6:12].hex() == BASE["AGENT"]
            and window["first_read_ns"][0] / 1e9 + offset - 0.001
            < at / 1e9
            < window["last_read_ns"][1] / 1e9 + offset + 0.001
            for at, f in raw
        ), "proxy reflected during a published metric interval"
        published_sample = status["sample"]
        assert published_sample["interval_started"] == window["first_read_ns"][0] / 1e9
        assert published_sample["observed_at"] == window["last_read_ns"][1] / 1e9
        assert (
            row["observed_ns"] / 1e9
            < published_sample["valid_until"]
            <= published_sample["observed_at"] + 2
        )
        assert [t["kind"] for t in published_sample["metrics"]] == [9, 10]
        tx, rx = [bytes.fromhex(t["value_hex"]) for t in published_sample["metrics"]]
        assert tx[:12] == rx[:12] == bytes.fromhex(BASE["AGENT"] + BASE["CONTROLLER"])
        local, remote = (bytes.fromhex(x["address"].replace(":", "")) for x in (pod, controller))
        assert struct.unpack("!6s6sHBIIHHH", tx[12:]) == (
            local,
            remote,
            1,
            1,
            window["transmit_losses"],
            window["deltas"]["tx_packets"],
            int(100 * 1500 / 1538),
            int(estimate["available_percent"]),
            65535,
        )
        assert struct.unpack("!6s6sHIIB", rx[12:]) == (
            local,
            remote,
            1,
            window["receive_losses"],
            window["deltas"]["rx_packets"],
            255,
        )
        published.append((row["observed_ns"] / 1e9 + offset, published_sample, window))
        contexts.add(published_sample["context_token"])
        generations.add((row["worker_pid"], sample["generation"]))
        workers.add(row["worker_pid"])
    assert len(published) >= 90 and len(contexts) == len(generations) == 3 and len(workers) == 2
    packets = BASE["packets"](directory / "ethernet.pcap")
    queries = [p for p in packets if p["kind"] == 5 and p["source"] == BASE["CONTROLLER"]]
    responses = [p for p in packets if p["kind"] == 6 and p["source"] == BASE["AGENT"]]
    stop = read(directory / "worker-stop.json")
    assert stop["returncode"] == 0 and stop["requested_at"] <= stop["exited_at"]
    teardown_queries = [p for p in queries if p["time"] > stop["exited_at"] + 0.001]
    live_queries = [p for p in queries if p["time"] < stop["requested_at"] - 0.001]
    assert len(queries) == len(teardown_queries) + len(live_queries), (
        "query on ambiguous exit boundary"
    )
    queries = live_queries
    path_gap = read(directory / "peer-path-gap-check.json")
    unsupported = path_gap["session_withheld"]["forwarding_observation"]["sample"][
        "peer_path_observation"
    ]["after"]
    pod_port = next(
        p
        for p in unsupported["ports"]
        if p["ifindex"] == unsupported["descriptor"]["pod_peer"]["ifindex"]
    )
    assert pod_port["linkinfo"]["info_slave_data"]["isolated"] is False
    unsafe_start = unsupported["ended_ns"] / 1e9 + offset
    assert path_gap["started_at"] <= unsafe_start < path_gap["restored_at"]
    path_fault_queries = [
        q["frame"]
        for q in queries
        if unsafe_start + 0.001 < q["time"] < path_gap["restored_at"] - 0.001
    ]
    connection_fault_queries = runpy.run_path(str(ROOT / "scripts/check-neighbor-metrics.py"))[
        "queries_during_source_loss"
    ](queries, read(directory / "recovery-checks.json"), events)
    # Restoring discovery does not retroactively qualify the interval spanning
    # its loss. Classify only queries before the first whole new interval could
    # have been read, not arbitrary processing delays after measurements exist.
    discovery_gap = read(directory / "neighbor-gap-check.json")
    assert not discovery_gap["session_withheld"]["observed_neighbor"]["available"]
    restored = next(
        row
        for row in observations
        if row["observed_ns"] / 1e9 + offset > discovery_gap["restored_at"]
        and row["observed_neighbor"]["available"]
    )
    since = restored["sample"]["peer_path_observation"]["after"]["ended_ns"]
    assert restored["peer_link_metrics"]["qualified_since_ns"] == since
    assert not restored["peer_link_metrics"]["available"]
    first = next(
        row
        for row in observations
        if row["observed_ns"] >= restored["observed_ns"]
        and row["shaped_backhaul"]["window"] is not None
        and row["shaped_backhaul"]["window"]["first_read_ns"][0] >= since
    )
    assert first["peer_link_metrics"]["available"]
    baseline_start = since / 1e9 + max(offsets) / 1e9 + 0.001
    baseline_end = (
        first["shaped_backhaul"]["window"]["last_read_ns"][0] / 1e9 + min(offsets) / 1e9 - 0.001
    )
    baseline_queries = [q["frame"] for q in queries if baseline_start < q["time"] < baseline_end]
    excluded = set(path_fault_queries + connection_fault_queries + baseline_queries)
    assert not any(
        r["mid"] == q["mid"] and 0 <= r["time"] - q["time"] < 1
        for q in queries
        if q["frame"] in excluded
        for r in responses
    )
    assert len(queries) >= 3 and len(queries) - len(excluded) == len(responses) >= 2
    queries = [q for q in queries if q["frame"] not in excluded]
    records = []
    for query in queries:
        assert query["tlvs"] == [(8, b"\0\2")]
        (response,) = [
            p for p in responses if p["mid"] == query["mid"] and 0 <= p["time"] - query["time"] < 1
        ]
        assert response["destination"] == BASE["CONTROLLER"]
        matching = [
            (at, s, w)
            for at, s, w in published
            if at <= response["time"] + 0.001
            and s["observed_at"] + offset <= response["time"] < s["valid_until"] + offset
            and [(t["kind"], bytes.fromhex(t["value_hex"])) for t in s["metrics"]]
            == response["tlvs"]
        ]
        assert matching, "reply does not match a fresh independently checked source"
        _, measured, window = matching[-1]
        wanted = {
            "PacketsSent": window["deltas"]["tx_packets"],
            "PacketsReceived": window["deltas"]["rx_packets"],
            "ErrorsSent": window["transmit_losses"],
            "ErrorsReceived": window["receive_losses"],
        }
        receipts = []
        # The native unmodified data model exposes per-interface packet/error
        # values. Capacity/availability remain independently checked on the wire.
        for f in sorted(directory.glob("active-inventory-*.json")):
            index = int(f.stem.rsplit("-", 1)[1])
            timing = read(directory / "active-samples.json")[index]["inventory_observation"]
            subsequent = [r["time"] for r in responses if r["time"] > response["time"]]
            if (
                not response["time"] + 0.001
                < timing["wall_started_ns"] / 1e9
                <= timing["wall_ended_ns"] / 1e9
                < min(subsequent, default=stop["requested_at"])
            ):
                continue
            objects = {k: v for group in read(f) for k, v in group.items() if isinstance(v, dict)}
            devices = [
                k
                for k, v in objects.items()
                if v.get("ID") == "02:00:00:00:30:01" and ".Interface." not in k
            ]
            if len(devices) != 1:
                continue
            interfaces = [
                k
                for k, v in objects.items()
                if k.startswith(devices[0]) and v.get("MACAddress") == pod["address"]
            ]
            if len(interfaces) != 1:
                continue
            stats = objects.get(interfaces[0] + "Stats.", {})
            if all(stats.get(k) == v for k, v in wanted.items()):
                receipts.append(f.name)
        assert receipts, "native controller interface statistics never reflect this response"
        records.append(
            {
                "query_frame": query["frame"],
                "response_frame": response["frame"],
                "mid": query["mid"],
                "latency_seconds": response["time"] - query["time"],
                "controller_statistics": wanted,
                "matching_inventory_files": receipts,
                "counter_epoch": measured["counter_epoch"],
            }
        )
    lost = read(directory / "recovery-checks.json")[0]["session_unavailable"]["peer_link_metrics"]
    assert not lost["available"] and lost["sample"] is None
    gap = read(directory / "neighbor-gap-check.json")
    # The native gap record preserves its three source snapshots.
    assert any(
        isinstance(v, dict) and v.get("peer_link_metrics", {}).get("available") is False
        for v in gap.values()
    )
    assert runtime["peer_path_gap_check_completed"]
    assert path_gap["session_before"]["peer_link_metrics"]["available"]
    assert path_gap["session_after"]["peer_link_metrics"]["available"]
    withheld = path_gap["session_withheld"]
    assert withheld["state"] == "provisioning" and withheld["report_source"]["available"]
    assert (
        not withheld["peer_link_metrics"]["available"]
        and not withheld["neighbor_link_metrics"]["measurement_available"]
    )
    assert path_gap["operation_before"] == path_gap["operation_after"]
    assert path_gap["clients_withheld"]["clients"] == "passed"
    assert (
        path_gap["session_before"]["peer_link_metrics"]["path_epoch"]
        != path_gap["session_after"]["peer_link_metrics"]["path_epoch"]
    )
    return {
        "owned_native_peer_metric_checks_passed": True,
        "qualified_profile": "owned-isolated-peer-path-v1",
        "published_observations_checked": len(published),
        "counter_windows": accounting["service_windows"],
        "control_contexts": len(contexts),
        "connection_generations": len(generations),
        "worker_processes": len(workers),
        "proxy_reflected_frames_in_published_intervals": 0,
        "peer_path_fault_withdrawal_checked": True,
        "queries_after_confirmed_worker_exit": [q["frame"] for q in teardown_queries],
        "queries_withheld_during_observed_invalid_path": path_fault_queries,
        "queries_during_confirmed_pod_connection_loss": connection_fault_queries,
        "queries_before_post_discovery_baseline_complete": baseline_queries,
        "post_discovery_baseline_unavailable_bounds": [baseline_start, baseline_end],
        "native_queries_answered": len(records),
        "responses": records,
        "complete_sustained_operation_proven": False,
        "physical_pod_proven": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.directory), indent=2))
