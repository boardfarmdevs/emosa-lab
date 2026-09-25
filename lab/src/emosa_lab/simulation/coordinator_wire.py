"""Two synthetic peers for the ownership-guarded isolated AF_PACKET driver.

Readiness/fault markers synchronize the lab fixture, not an EasyMesh protocol.
The only network messages are standard reports, Topology Queries and 1905 Acks.
"""

import asyncio
import json
import time
from pathlib import Path

from emosa.wire.cmdu import MidSequence, Reassembler, fragment_message
from emosa.wire.coordinator import ReportCoordinator
from emosa.wire.ethernet import EthernetEndpoint
from emosa_lab.simulation.coordinator import observed_ssid
from emosa_lab.simulation.report_source import AFTER, BEFORE, DatabaseReportFixture
from emosa_lab.simulation.wire_reports import AGENT, CONTROLLER, pcap, query_frames


async def agent(interface, directory, captures):
    fixture = DatabaseReportFixture(ingress=interface)
    coordinator = None
    try:
        await fixture.start()
        with EthernetEndpoint(interface, AGENT, timeout=0.05) as endpoint:
            coordinator = ReportCoordinator(fixture.source, endpoint.send, mids=MidSequence(0))
            (directory / "left.ready").touch()
            coordinator.notify_early()
            stage, end = 0, time.monotonic() + 12
            while time.monotonic() < end:
                await fixture.refresh()
                coordinator.tick()
                frame = await asyncio.to_thread(endpoint.receive)
                if frame is not None:
                    captures.append(frame)
                    coordinator.receive(frame, ingress=interface, generation=1)
                counts = coordinator.counts
                responses = counts.get("topology_response_sent", 0)
                if stage == 0 and responses == 1 and counts.get("early_acknowledged") == 1:
                    await fixture.change_config()
                    await fixture.wait_ready(ssid=BEFORE)
                    (directory / "config-ready").touch()
                    stage = 1
                elif stage == 1 and responses == 2:
                    await fixture.manager.command("apply")
                    await fixture.wait_ready(ssid=AFTER)
                    (directory / "state-ready").touch()
                    stage = 2
                elif stage == 2 and responses == 3:
                    await fixture.database.stop()
                    assert not await fixture.refresh()
                    (directory / "disconnected").touch()
                    stage = 3
                elif (
                    stage == 3
                    and counts.get("input_or_source_rejected", 0) >= 1
                    and (directory / "peer-checked").exists()
                ):
                    return {
                        "passed": True,
                        "coordinator": coordinator.status(),
                        "monitor_read_only": fixture.session.read_only,
                        "pod_initiated_connection": True,
                    }
            raise AssertionError("coordinator wire fixture did not complete; retain logs")
    finally:
        if coordinator is not None:
            coordinator.close()
        await fixture.close()


async def peer(interface, directory, captures):
    with EthernetEndpoint(interface, CONTROLLER, timeout=0.05) as endpoint:
        (directory / "right.ready").touch()
        parser = Reassembler()
        early_mids, replies = [], {}
        requested = set()
        silence_started = None
        end = time.monotonic() + 18
        while time.monotonic() < end:
            frame = await asyncio.to_thread(endpoint.receive)
            if frame is not None:
                captures.append(frame)
                message = parser.feed(frame)
                if message is None:
                    continue
                assert message.source == AGENT and message.destination == CONTROLLER
                if message.message_type == 0x8043:
                    early_mids.append(message.mid)
                    if len(early_mids) > 1:
                        for ack in fragment_message(AGENT, CONTROLLER, 0x8000, message.mid, ()):
                            endpoint.send(ack)
                elif message.message_type == 3:
                    assert message.mid in (600, 601, 602)
                    # This fixture's one-BSS response fits in one Ethernet frame.
                    replies[message.mid] = observed_ssid((frame,))
                else:
                    raise AssertionError("unexpected coordinator fixture message")
            stages = (
                (600, bool(early_mids)),
                (601, 600 in replies and (directory / "config-ready").exists()),
                (602, 601 in replies and (directory / "state-ready").exists()),
                (603, 602 in replies and (directory / "disconnected").exists()),
            )
            for mid, ready in stages:
                if ready and mid not in requested:
                    for query in query_frames(mid):
                        endpoint.send(query)
                    requested.add(mid)
                    if mid == 603:
                        silence_started = time.monotonic()
            if silence_started is not None and time.monotonic() - silence_started >= 1.1:
                assert len(early_mids) == 2 and len(set(early_mids)) == 2
                assert replies == {600: BEFORE, 601: BEFORE, 602: AFTER}
                (directory / "peer-checked").touch()
                return {
                    "passed": True,
                    "early_mids": early_mids,
                    "config_only_keeps_observed_ssid": True,
                    "independent_state_update_reported": True,
                    "disconnected_report_suppressed": True,
                    "post_disconnect_observation_seconds": time.monotonic() - silence_started,
                    "query_mids": sorted(requested),
                    "response_mids": sorted(replies),
                }
        raise AssertionError("synthetic coordinator peer timed out; retain logs")


async def worker(side, interface, directory):
    directory = Path(directory)
    captures = []
    try:
        result = await (agent if side == "left" else peer)(interface, directory, captures)
        result.update(
            scope="read_only_ovsdb_report_coordinator_over_AF_PACKET",
            native_controller_used=False,
            adapter_config_writes=0,
            fixture_admin_and_manager_writes=True,
            radio_used=False,
            controller_onboarding_proven=False,
            physical_pod_proven=False,
        )
        (directory / (side + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    finally:
        (directory / (side + ".pcap")).write_bytes(pcap(captures))
