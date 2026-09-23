"""Owned AF_PACKET discovery/report lifecycle and synthetic controller peer."""

import asyncio
import json
import time
from pathlib import Path

from emosa.simulation.coordinator import observed_ssid
from emosa.simulation.discovery import response_frames
from emosa.simulation.report_source import AFTER, BEFORE, DatabaseReportFixture
from emosa.simulation.wire_reports import AGENT, CONTROLLER, pcap, query_frames
from emosa.wire.cmdu import MidSequence, Reassembler
from emosa.wire.discovery_session import DiscoveryReportSession
from emosa.wire.ethernet import EthernetEndpoint


async def agent(interface, directory, captures):
    fixture = DatabaseReportFixture(ingress=interface)
    session = None
    try:
        await fixture.start()
        with EthernetEndpoint(interface, AGENT, timeout=0.05) as endpoint:
            session = DiscoveryReportSession(fixture.source, endpoint.send, mids=MidSequence(0))
            (directory / "left.ready").touch()
            stage, rejected, end = 0, None, time.monotonic() + 18
            while time.monotonic() < end:
                await fixture.refresh()
                session.tick()
                frame = await asyncio.to_thread(endpoint.receive)
                if frame is not None:
                    captures.append(frame)
                    session.receive(frame, ingress=interface, generation=1)
                if session.state == "discovery_incompatible":
                    rejected = session.status()
                    if (directory / "retry-discovery").exists():
                        session.restart()
                replies = (
                    session.reports.counts.get("topology_response_sent", 0)
                    if session.reports
                    else 0
                )
                if stage == 0 and replies == 1:
                    await fixture.change_config()
                    await fixture.wait_ready(ssid=BEFORE)
                    (directory / "config-ready").touch()
                    stage = 1
                elif stage == 1 and replies == 2:
                    await fixture.manager.command("apply")
                    await fixture.wait_ready(ssid=AFTER)
                    (directory / "state-ready").touch()
                    stage = 2
                elif stage == 2 and replies == 3:
                    await fixture.database.stop()
                    assert not await fixture.refresh()
                    session.tick()
                    (directory / "disconnected").touch()
                    stage = 3
                elif stage == 3 and (directory / "peer-checked").exists():
                    assert rejected is not None and session.state == "waiting_source"
                    return {
                        "passed": True,
                        "session": session.status(),
                        "incompatible_advertisement": rejected,
                        "monitor_read_only": fixture.session.read_only,
                        "pod_initiated_connection": True,
                    }
            raise AssertionError("discovery wire fixture did not complete; retain logs")
    finally:
        if session is not None:
            session.close()
        await fixture.close()


async def peer(interface, directory, captures):
    with EthernetEndpoint(interface, CONTROLLER, timeout=0.05) as endpoint:
        (directory / "right.ready").touch()
        assembly = Reassembler()
        searches, replies, requested = [], {}, set()
        incompatible_started = disconnected_started = None
        incompatible_observation = None
        end = time.monotonic() + 22
        while time.monotonic() < end:
            frame = await asyncio.to_thread(endpoint.receive)
            if frame is not None:
                captures.append(frame)
                message = assembly.feed(frame)
                if message is not None:
                    assert message.source == AGENT
                    if message.message_type == 7:
                        searches.append(message.mid)
                        assert len(searches) <= 3
                        if len(searches) == 2:
                            endpoint.send(response_frames(message.mid, compatible=False)[0])
                            endpoint.send(query_frames(710)[0])
                            incompatible_started = time.monotonic()
                        elif len(searches) == 3:
                            endpoint.send(response_frames(message.mid)[0])
                            endpoint.send(query_frames(711)[0])
                    elif message.message_type == 3:
                        assert message.mid in (711, 712, 713)
                        replies[message.mid] = observed_ssid((frame,))
                    else:
                        raise AssertionError("unexpected Early Report/M1 or other message")
            if incompatible_started is not None and time.monotonic() - incompatible_started >= 1.1:
                incompatible_observation = time.monotonic() - incompatible_started
                incompatible_started = None
                (directory / "retry-discovery").touch()
            for mid, ready in (
                (712, 711 in replies and (directory / "config-ready").exists()),
                (713, 712 in replies and (directory / "state-ready").exists()),
                (714, 713 in replies and (directory / "disconnected").exists()),
            ):
                if ready and mid not in requested:
                    endpoint.send(query_frames(mid)[0])
                    requested.add(mid)
                    if mid == 714:
                        disconnected_started = time.monotonic()
            if disconnected_started is not None and time.monotonic() - disconnected_started >= 1.1:
                assert searches == [1, 2, 3]
                assert replies == {711: BEFORE, 712: BEFORE, 713: AFTER}
                (directory / "peer-checked").touch()
                return {
                    "passed": True,
                    "search_mids": searches,
                    "response_mids": sorted(replies),
                    "query_mids": [710, 711, 712, 713, 714],
                    "incompatible_observation_seconds": incompatible_observation,
                    "disconnected_observation_seconds": time.monotonic() - disconnected_started,
                    "automatic_early_report_or_m1_observed": False,
                    "independent_state_change_reported": True,
                }
        raise AssertionError("synthetic discovery peer timed out; retain logs")


async def worker(side, interface, directory):
    directory = Path(directory)
    captures = []
    try:
        result = await (agent if side == "left" else peer)(interface, directory, captures)
        result.update(
            scope="restricted_discovery_with_real_ovsdb_over_AF_PACKET",
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
