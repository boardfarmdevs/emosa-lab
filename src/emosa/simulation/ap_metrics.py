"""Synthetic AP report composition/scheduling vectors; no native measurement claim."""

import argparse
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients
from emosa.simulation.wire_reports import BSSID, RUID, fixtures, pcap
from emosa.wire.ap_metrics import (
    APExtendedMetrics,
    APMetricBundle,
    APMetricCoordinator,
    APMetrics,
    APMetricSource,
    RadioMetrics,
    StationLinkMetrics,
    StationMetrics,
)
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.coordinator import ReportSource
from emosa.wire.disassociation import TrafficCounters
from emosa.wire.reporting_policy import ReportingPolicyCoordinator, ReportingPolicyStore

STA = bytes.fromhex("020000004020")


def generate(output):
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    now, revision, frames = [10.0], [0], []
    binding, caps, topology = fixtures()
    topology = replace(
        topology, clients=AssociatedClients((BssClients(BSSID, (AssociatedClient(STA, 4),)),))
    )
    source = ReportSource(binding, "synthetic-pod", "a" * 64, clock=lambda: now[0])
    metrics = APMetricSource(source, clock=lambda: now[0])
    with tempfile.TemporaryDirectory(prefix="emosa-ap-vectors-") as scratch:
        store = ReportingPolicyStore(Path(scratch) / "policy.sqlite", boot_id="synthetic-boot")
        reporter = APMetricCoordinator(
            metrics,
            frames.append,
            MidSequence(100),
            admitted=lambda: True,
            policy=lambda: coordinator.value["policy"],
            clock=lambda: now[0],
        )
        coordinator = ReportingPolicyCoordinator(
            source, frames.append, store, reporter=reporter, clock=lambda: now[0]
        )

        def observe(*, missing_link=False, empty_queues=False):
            revision[0] += 1
            source.publish((1, revision[0]), caps, topology, observed_at=now[0], lifetime=2)
            value = APMetricBundle(
                APMetrics(BSSID, 123, 1, (("VI", b"\x02\x30\x40"), ("BE", b"\x01\x02\x03"))),
                APExtendedMetrics(BSSID, (0x01020304, 2, 3, 4, 5, 0xFFFFFFFF), 0),
                RadioMetrics(RUID, 160, 21, 22, 23),
                (
                    StationMetrics(
                        STA,
                        "synthetic-association-1",
                        TrafficCounters(2**32 + 2049, 1002, 3, 4, 5, 6, 7),
                        None
                        if missing_link
                        else StationLinkMetrics(
                            now[0] - 0.125, 65, 32, 120, 65000, 32000, 101, 202
                        ),
                        () if empty_queues else ((7, 255), (0, 1)),
                    ),
                ),
            )
            metrics.publish(
                context=source.current().context_token,
                counter_epoch="synthetic-counter-1",
                observed_at=now[0],
                bundle=value,
                inventory_complete=True,
            )

        def request(kind, mid, tlvs):
            frame = fragment_message(binding.local_al, binding.controller_al, kind, mid, tlvs)[0]
            frames.append(frame)
            return Reassembler().feed(frame)

        def query(mid, radio=True):
            return reporter.handle(
                request(
                    0x800B, mid, (Tlv(0x93, b"\1" + BSSID),) + ((Tlv(0x82, RUID),) if radio else ())
                ),
                now[0],
                ingress="fixture",
                generation=1,
            )

        try:
            observe()
            coordinator.handle(
                request(0x8003, 11, (Tlv(0x8A, b"\x3c\1" + RUID + b"\0\0\0\xe0"),)), now[0]
            )
            assert query(71) == "ap_metric_query_answered"
            now[0] = 70
            observe()
            coordinator.tick()
            now[0] = 70.25
            observe(missing_link=True)
            assert query(72) == "ap_measurements_unavailable"
            now[0] = 70.5
            observe(empty_queues=True)
            assert query(73, radio=False) == "ap_metric_query_answered"
            now[0] = 130
            observe(missing_link=True)
            coordinator.tick()
            state = store.read()
            assert state["reports_transmitted"] == state["periods_due_without_report"] == 1
            before = len(frames)
            coordinator.close()
            store.close()
            store = ReportingPolicyStore(Path(scratch) / "policy.sqlite", boot_id="synthetic-boot")
            coordinator = ReportingPolicyCoordinator(
                source, frames.append, store, reporter=reporter, clock=lambda: now[0]
            )
            coordinator.tick()
            assert len(frames) == before
            result = {
                "scope": (
                    "synthetic report composition and scheduling; no measured/native AP source"
                ),
                "passed": True,
                "frames": len(frames),
                "query_reports_transmitted": 2,
                "periodic_reports_transmitted": 1,
                "missing_companion_query_mid": 72,
                "restart_extra_frames": 0,
                "schedule": state,
                "measurement_source_qualified": False,
                "native_ap_reporting_proven": False,
                "sustained_operation_proven": False,
                "physical_pod_proven": False,
                "opaque_fields": (
                    "ESP bytes and Data Elements-derived integers are explicit synthetic wire "
                    "values; subfield/measurement conversions remain unqualified"
                ),
            }
            (output / "synthetic-ap-metrics.pcap").write_bytes(pcap(frames))
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        finally:
            reporter.close()
            coordinator.close()
            store.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(json.dumps(generate(parser.parse_args().output), indent=2))
