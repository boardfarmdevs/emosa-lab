"""Discovery-to-topology exercise with an owned real database and synthetic peer.

Ethernet is serialized and delivered in memory. No automatic Early Report, M1,
operation engine or physical endpoint is exposed. Use a new output directory.
"""

import argparse
import asyncio
import json
from pathlib import Path

from emosa.simulation.coordinator import observed_ssid
from emosa.simulation.report_source import AFTER, BEFORE, DatabaseReportFixture
from emosa.simulation.wire_reports import AGENT, CONTROLLER, pcap, query_frames
from emosa.wire.cmdu import MidSequence, Reassembler, Tlv, fragment_message
from emosa.wire.discovery_session import DiscoveryReportSession


def response_frames(mid, *, compatible=True):
    tlvs = (
        Tlv(0x0F, b"\0"),
        Tlv(0x10, b"\0"),
        Tlv(0x80, b"\x01\0"),
        Tlv(0xB3, b"\x01"),
        Tlv(0xDD, b"\xc0" if compatible else b"\x40"),
    )
    if compatible:
        tlvs += (Tlv(0xA9, bytes(3)),)
    return fragment_message(AGENT, CONTROLLER, 8, mid, tlvs)


async def run(directory):
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    fixture = DatabaseReportFixture()
    sent, trace, stages = [], [], {}

    def send(frame):
        sent.append(frame)
        trace.append(frame)

    session = DiscoveryReportSession(fixture.source, send, mids=MidSequence(0))

    def receive(frame):
        trace.append(frame)
        return session.receive(frame, ingress="fixture", generation=1)

    def query(mid, expected=None):
        start = len(sent)
        result = receive(query_frames(mid)[0])
        if expected is None:
            assert len(sent) == start and result == "message_not_admitted"
        else:
            assert result == "topology_response_sent" and observed_ssid(sent[start:]) == expected
        return {"outcome": result, "response_frames": len(sent) - start}

    async def start_search():
        before = len(sent)
        end = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < end:
            await fixture.refresh()
            session.tick()
            if len(sent) > before:
                message = Reassembler().feed(sent[-1])
                assert message.message_type == 7
                return message.mid
            await asyncio.sleep(0.05)
        raise AssertionError("fixture did not start discovery")

    try:
        await fixture.start()
        stages["before_discovery"] = query(700)
        first = await start_search()
        assert receive(response_frames(first, compatible=False)[0]) == "discovery_incompatible"
        stages["incompatible_advertisement"] = session.status()
        stages["incompatible_query"] = query(701)
        session.restart()
        second = await start_search()
        assert second != first
        assert receive(response_frames(first)[0]) == "input_rejected"
        assert receive(response_frames(second)[0]) == "controller_correlated"
        stages["correlated_topology"] = query(702, BEFORE)
        await fixture.change_config()
        await fixture.wait_ready(ssid=BEFORE)
        stages["config_only"] = query(703, BEFORE)
        await fixture.manager.command("apply")
        await fixture.wait_ready(ssid=AFTER)
        stages["observed_state_change"] = query(704, AFTER)
        await fixture.database.stop()
        assert not await fixture.refresh()
        stages["disconnected"] = query(705)
        await fixture.database.start()
        await fixture.database.manager_remote(fixture.listener)
        await fixture.wait_ready(ssid=AFTER)
        stages["reconnected_before_discovery"] = query(706)
        third = await start_search()
        assert len({first, second, third}) == 3
        assert receive(response_frames(second)[0]) == "input_rejected"
        assert receive(response_frames(third)[0]) == "controller_correlated"
        stages["rediscovered_topology"] = query(707, AFTER)
        stages["unsolicited_wsc"] = receive(fragment_message(AGENT, CONTROLLER, 9, 900, ())[0])
        assert stages["unsolicited_wsc"] == "wsc_admission_blocked"
        assert all(Reassembler().feed(f).message_type in (7, 3) for f in sent)
        result = {
            "passed": True,
            "scope": "restricted_discovery_with_real_ovsdb_and_in_memory_ethernet",
            "search_mids": [first, second, third],
            "stages": stages,
            "session": session.status(),
            "monitor_read_only": fixture.session.read_only,
            "pod_initiated_connection": True,
            "credential_columns_monitored": False,
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
        session.close()
        await fixture.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(run(args.output)), indent=2))


if __name__ == "__main__":
    main()
