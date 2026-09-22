"""Exercise read-only report coordination against an owned real OVSDB database.

Messages are serialized Ethernet frames delivered in memory. For AF_PACKET use
this repository's isolated VM driver. Neither is native controller onboarding.
"""

import argparse
import asyncio
import json
from pathlib import Path

from emosa.easymesh_payloads import decode_value
from emosa.opensync.mapping import check_results
from emosa.simulation.report_source import AFTER, BEFORE, DatabaseReportFixture
from emosa.simulation.wire_reports import AGENT, CONTROLLER, pcap, query_frames
from emosa.wire.cmdu import MidSequence, Reassembler, fragment_message
from emosa.wire.coordinator import ReportCoordinator


def observed_ssid(frames):
    assembly = Reassembler()
    message = None
    for frame in frames:
        message = assembly.feed(frame)
    assert message is not None and message.message_type == 3
    value = next(t.value for t in message.tlvs if t.kind == 0x83)
    return decode_value(0x83, value).radios[0].bsses[0].ssid.decode()


async def client_fixture(fixture, *, present):
    """An owned fixture fault, not a report-coordinator operation."""
    if present:
        operations = [
            {
                "op": "insert",
                "table": "Wifi_Associated_Clients",
                "uuid-name": "client",
                "row": {"mac": "02:00:00:00:40:99", "state": "active"},
            },
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [],
                "row": {"associated_clients": ["set", [["named-uuid", "client"]]]},
            },
        ]
        expected = [None, 1]
    else:
        operations = [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [],
                "row": {"associated_clients": ["set", []]},
            },
            {"op": "delete", "table": "Wifi_Associated_Clients", "where": []},
        ]
        expected = [1, None]
    check_results(await fixture.admin.transact(operations), expected)


async def run(directory):
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    fixture = DatabaseReportFixture()
    sent, trace, stages = [], [], {}

    def send(frame):
        sent.append(frame)
        trace.append(frame)

    coordinator = ReportCoordinator(fixture.source, send, mids=MidSequence(65534))

    def receive(frame):
        trace.append(frame)
        return coordinator.receive(frame, ingress="fixture", generation=1)

    def query(mid, expected=None):
        start = len(sent)
        outcome = receive(query_frames(mid)[0])
        if expected is None:
            assert len(sent) == start and outcome == "input_or_source_rejected"
        else:
            assert outcome == "topology_response_sent"
            assert observed_ssid(sent[start:]) == expected
        return {"outcome": outcome, "response_frames": len(sent) - start}

    try:
        await fixture.start()
        coordinator.notify_early()
        first_mid = Reassembler().feed(sent[-1]).mid
        # Deliberately withhold the first receipt; no fabricated controller timer.
        await asyncio.sleep(0.27)
        await fixture.refresh()
        coordinator.tick()
        retry_mid = Reassembler().feed(sent[-1]).mid
        assert first_mid != retry_mid and len(sent) == 2
        ack = fragment_message(AGENT, CONTROLLER, 0x8000, retry_mid, ())[0]
        assert receive(ack) == "early_acknowledged"
        stages["lost_ack"] = {"first_mid": first_mid, "retry_mid": retry_mid, "acknowledged": True}
        stages["initial_state"] = query(200, BEFORE)
        before_token = fixture.source.current().stamp.token
        await fixture.change_config()
        await fixture.wait_ready(ssid=BEFORE)
        assert fixture.source.current().stamp.token != before_token
        stages["config_only_keeps_observed_ssid"] = query(201, BEFORE)
        await fixture.manager.command("apply")
        await fixture.wait_ready(ssid=AFTER)
        stages["independent_manager_state_change"] = query(202, AFTER)
        await client_fixture(fixture, present=True)
        assert not await fixture.refresh()
        stages["unknown_client_age_withdraws_report"] = query(203)
        await client_fixture(fixture, present=False)
        await fixture.wait_ready(ssid=AFTER)
        stages["complete_inventory_restored"] = query(204, AFTER)
        old_generation = fixture.source._revision[0]
        await fixture.database.stop()
        assert not await fixture.refresh()
        stages["database_disconnect_withdraws_report"] = query(205)
        await fixture.database.start()
        await fixture.database.manager_remote(fixture.listener)
        await fixture.wait_ready(ssid=AFTER)
        assert fixture.source._revision[0] > old_generation
        stages["database_reconnect_fresh_inventory"] = query(206, AFTER)
        result = {
            "passed": True,
            "scope": "read_only_report_coordinator_with_real_ovsdb_and_in_memory_ethernet",
            "pod_initiated_connection": True,
            "monitor_read_only": fixture.session.read_only,
            "credential_columns_monitored": False,
            "stages": stages,
            "coordinator": coordinator.status(),
            "socket_io": False,
            "radio_used": False,
            "native_controller_used": False,
            "fixture_admin_and_manager_writes": True,
            "adapter_config_writes": 0,
            "controller_onboarding_proven": False,
            "physical_pod_proven": False,
        }
        (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        (directory / "messages.pcap").write_bytes(pcap(trace))
        coordinator.close()
        await fixture.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(run(args.output)), indent=2))


if __name__ == "__main__":
    main()
