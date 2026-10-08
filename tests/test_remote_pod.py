# SPDX-License-Identifier: Apache-2.0
"""The simulated pod for a remote EMOSA (emosa_lab.remote_pod): its identity, its cm following
manager_addr from the fleet to the agent, its managers applying the agent's writes."""

import asyncio

import pytest

from emosa_lab.box import apply_configuration, dial
from emosa_lab.remote_pod import attach_vifs, follow, rows, served, start_pod, transact, uuids

pytestmark = pytest.mark.ovsdb


def update(table, row, where=()):
    return [{"op": "update", "table": table, "where": list(where), "row": row}]


async def listener():
    """A TCP port on loopback and the connections it accepted."""
    accepted = []

    async def accept(reader, writer):
        accepted.append(writer)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], accepted


async def until(predicate, seconds=10):
    for _ in range(int(seconds * 10)):
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


def test_the_pod_follows_manager_addr_and_applies_the_agents_writes(tmp_path):
    async def scenario():
        db = await start_pod("SIMPODTEST000001", tmp_path / "db")
        fleet, fleet_port, at_fleet = await listener()
        agent, agent_port, at_agent = await listener()
        try:
            node = next(iter((await rows(db, "AWLAN_Node")).values()))
            assert node["serial_number"] == "SIMPODTEST000001"
            remote = f"tcp:127.0.0.1:{fleet_port}"
            await transact(db, update("AWLAN_Node", {"manager_addr": remote}))
            await dial(db, remote)
            assert await until(lambda: at_fleet), "the pod dials the fleet"
            assert await follow(db, remote) == remote, "nothing to follow yet"
            # the fleet hands the pod over: manager_addr is its agent's port
            handed = f"tcp:127.0.0.1:{agent_port}"
            await transact(db, update("AWLAN_Node", {"manager_addr": handed}))
            assert await follow(db, remote) == handed
            assert await until(lambda: at_agent), "the pod dials its agent"
            # the agent writes the fronthaul's SSID; the pod's managers apply it
            vif = next(
                uuid
                for uuid, row in (await rows(db, "Wifi_VIF_Config")).items()
                if row.get("if_name") == "home-ap-24"
            )
            where = [["_uuid", "==", ["uuid", vif]]]
            await transact(db, update("Wifi_VIF_Config", {"ssid": "written"}, where))
            await apply_configuration(db)
            assert "written" in await served(db)
        finally:
            for server in (fleet, agent):
                server.close()
            await db.stop()

    asyncio.run(scenario())


def test_a_vif_the_agent_creates_gets_a_bssid_on_its_radio(tmp_path):
    async def scenario():
        db = await start_pod("SIMPODTEST000002", tmp_path / "db")
        try:
            radio = next(
                uuid
                for uuid, row in (await rows(db, "Wifi_Radio_Config")).items()
                if row.get("freq_band") == "2.4G"
            )
            # as EMOSA writes a slot of the controller's set: a VIF on the 2.4 GHz radio
            await transact(
                db,
                [
                    {
                        "op": "insert",
                        "table": "Wifi_VIF_Config",
                        "uuid-name": "slot",
                        "row": {"if_name": "svc-d-ap-24", "mode": "ap", "ssid": "iot_ssid"},
                    },
                    {
                        "op": "mutate",
                        "table": "Wifi_Radio_Config",
                        "where": [["_uuid", "==", ["uuid", radio]]],
                        "mutations": [["vif_configs", "insert", ["set", [["named-uuid", "slot"]]]]],
                    },
                ],
            )
            await apply_configuration(db)
            assert await attach_vifs(db) == 2  # its BSSID, and its place on the radio
            states = await rows(db, "Wifi_VIF_State")
            slot = next(u for u, r in states.items() if r.get("if_name") == "svc-d-ap-24")
            assert states[slot]["mac"] == "a2:00:00:00:01:00"  # 82:… is the primary's
            radio_state = next(
                r
                for r in (await rows(db, "Wifi_Radio_State")).values()
                if r.get("freq_band") == "2.4G"
            )
            assert slot in uuids(radio_state["vif_states"])
            assert await attach_vifs(db) == 0  # nothing left to do
        finally:
            await db.stop()

    asyncio.run(scenario())
