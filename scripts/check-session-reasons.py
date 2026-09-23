"""Independent live reason join audit against kernel events and a separate pcap.

No publisher/adapter imports. This proves raw acquisition/correlation, never the
missing EasyMesh counter conversion or final-statistics wire delivery.
"""

import argparse
import hashlib
import json
import runpy
import struct
from pathlib import Path

REMOVAL = runpy.run_path(str(Path(__file__).with_name("check-station-removal.py")))["check"]
HEALTH = runpy.run_path(str(Path(__file__).with_name("check-capture-health.py")))["check"]


def check(directory, tshark="tshark"):
    def read(name):
        return json.loads((directory / name).read_text())

    def lines(name):
        return [json.loads(line) for line in (directory / name).read_text().splitlines()]

    result, observer = read("result.json"), read("reason-observer.json")
    assert result["session_reason_observation_requested"]
    assert observer["profile"] == "owned-hwsim-reason-join-v1"
    assert observer["ready"] and not observer["running"] and not observer["errors"], (
        "live reason observer unhealthy"
    )
    assert result["session_reason_observation"] == observer
    health = HEALTH(directory)
    removal = REMOVAL(directory, tshark)
    assert observer["socket_drops"] == 0 and observer["socket_packets"] > 100
    assert not observer["final_counter_source_qualified"] and not observer["physical_pod_changed"]
    hashes = read("source-hashes.json")
    assert hashes["/opt/emosa-radio-manager/session-reasons.py"] == observer["collector_sha256"]
    kernel = lines("station-events.jsonl")
    assert observer["kernel_collector"] == kernel[0]
    assert observer["kernel_finished"] == kernel[-1]
    if "initial_clock_sample" in observer:
        initial = observer["initial_clock_sample"]
        lower = initial["wall_ns"] - initial["monotonic_after_ns"]
        upper = initial["wall_ns"] - initial["monotonic_ns"]
        samples = [initial, observer["last_clock_sample"], observer["last_kernel_clock_sample"]]
        samples.extend(
            {
                key: row["received_" + key]
                for key in ("monotonic_ns", "wall_ns", "monotonic_after_ns")
            }
            for row in kernel
        )
        for sample in samples:
            before, wall, after = (
                sample[k] for k in ("monotonic_ns", "wall_ns", "monotonic_after_ns")
            )
            assert 0 <= after - before <= 100_000
            assert max(upper, wall - before) - min(lower, wall - after) <= 1_000_000
    events = lines("session-reasons.jsonl")
    assert events[0]["event"] == "ready" and events[-1]["event"] == "finished"
    assert {k: v for k, v in events[-1].items() if k != "event"} == observer
    final = [e for e in events if e["event"] == "joined_raw_final"]
    assert len(final) == observer["joined"] == removal["counts"]["del_station"] >= 6
    assert len({e["association_at_boottime_ns"] for e in final}) == len(final)
    radio = []
    data = (directory / "radio.pcap").read_bytes()
    assert data[:4] == b"\xd4\xc3\xb2\xa1"
    offset, number = 24, 0
    while offset < len(data):
        sec, usec, length, original = struct.unpack_from("<IIII", data, offset)
        offset += 16
        packet = data[offset : offset + length]
        offset += length
        number += 1
        assert length == original and len(packet) == length
        rt = int.from_bytes(packet[2:4], "little")
        mpdu = packet[rt:]
        if mpdu[0] in (0xA0, 0xC0):
            radio.append((number, (sec * 1_000_000 + usec) * 1000, mpdu))
    records, used = [], set()
    for joined in final:
        assert joined["collector_epoch"] == observer["collector_epoch"]
        lifetime = joined["observed_lifetime"]
        creation, deletion = [e for e in kernel if e.get("observed_lifetime") == lifetime]
        assert creation == joined["kernel_new"] and deletion == joined["kernel_final"]
        assert creation["event"] == "new_station" and deletion["event"] == "del_station"
        assert joined["station"] == creation["station"] == deletion["station"]
        assert joined["ifindex"] == creation["ifindex"] == deletion["ifindex"]
        assert all(
            joined["raw_counters"][k] == v
            for k, v in deletion["observed_fields"].items()
            if k in joined["raw_counters"]
        )
        assert set(joined["raw_counters"]) == {
            "tx_bytes64",
            "rx_bytes64",
            "tx_packets",
            "rx_packets",
            "tx_failed",
            "rx_drop_misc",
            "tx_retries",
            "assoc_at_boottime_ns",
        }
        assert (
            joined["association_at_boottime_ns"]
            == deletion["observed_fields"]["assoc_at_boottime_ns"]
        )
        frame = joined["disconnect_frame"]
        matches = [
            (n, t, f)
            for n, t, f in radio
            if f.hex() == frame["frame_hex"] and abs(t - frame["wall_ns"]) <= 1_000_000
        ]
        assert len(matches) == 1
        n, at, body = matches[0]
        assert n not in used
        used.add(n)
        assert int.from_bytes(body[24:26], "little") == frame["reason"]
        independent = next(r for r in removal["correlations"] if r["observed_lifetime"] == lifetime)
        assert independent["radio_frame"] == n and independent["actual_reason"] == frame["reason"]
        assert 0 <= deletion["received_wall_ns"] - at < 1_000_000_000
        latency = joined["joined_monotonic_ns"] - deletion["received_monotonic_ns"]
        assert 250_000_000 <= latency < 1_000_000_000
        assert not joined["final_counter_source_qualified"]
        assert not joined["native_final_statistics_delivery_proven"]
        records.append(
            {
                "observed_lifetime": lifetime,
                "radio_frame": n,
                "reason": frame["reason"],
                "live_join_latency_ms": latency / 1_000_000,
                "leave_notification_frame": independent["leave_notification_frame"],
            }
        )
    assert not result["cleanup_errors"]
    return {
        "live_session_reason_checks_passed": True,
        "scope": "owned sole-client station-initiated unprotected hwsim disconnects",
        "joined_removals": len(final),
        "bracketed_clock_checks_passed": "initial_clock_sample" in observer,
        "records": records,
        "capture_health": health,
        "socket_drops": 0,
        "collector_epoch": observer["collector_epoch"],
        "observation_sha256": hashlib.sha256(
            (directory / "session-reasons.jsonl").read_bytes()
        ).hexdigest(),
        "final_counter_source_qualified": False,
        "native_final_statistics_delivery_proven": False,
        "sustained_operation_proven": False,
        "physical_pod_proven": False,
        "remaining": [
            "Normative counter definitions and conversion",
            "Online reporting-source handoff",
            "Native final-statistics delivery and complete AP/STA reporting",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tshark", default="tshark")
    args = parser.parse_args()
    print(json.dumps(check(args.directory, args.tshark), indent=2))
