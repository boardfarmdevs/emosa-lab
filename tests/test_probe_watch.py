"""The pod watches the probe requests of the stations the controller asks about (spec §3.9)."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from emosa.agent.pod import MONITOR
from emosa.agent.probe_watch import MIN_INTERVAL, TTL, ProbeWatch
from emosa.agent.steering import ClientSteering
from emosa.clock import ManualClock
from emosa.model import State
from emosa.opensync.probe_watch import MARKER, WatchBackend, WatchIntent
from emosa.opensync.session import OvsSession
from emosa.opensync.steering import CLIENT_ROW, SteeringBackend
from emosa.secrets import SecretStore
from emosa.store import Store
from emosa_lab.simulation.database import SimDatabase
from test_client_steering import SOURCE, STA, TARGET, mandate, rows

pytestmark = pytest.mark.ovsdb
SERIAL = "EMOSA-STEER-1"
A, B, C = "02:00:00:00:0a:00", "02:00:00:00:0b:00", "02:00:00:00:0c:00"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@asynccontextmanager
async def pod(tmp_path, *, associated=()):
    database = await SimDatabase().start()
    session = OvsSession(database.endpoint, monitor_columns=MONITOR)
    admin = OvsSession(database.endpoint)
    try:
        await database.seed(serial_number=SERIAL, sole_radio=True)
        state = await rows(admin, "Wifi_VIF_State", ["_uuid", "if_name"])
        for mac in associated:
            await admin.transact(
                [
                    {
                        "op": "insert",
                        "table": "Wifi_Associated_Clients",
                        "uuid-name": "sta",
                        "row": {"mac": mac, "state": "active"},
                    },
                    {
                        "op": "mutate",
                        "table": "Wifi_VIF_State",
                        "where": [["_uuid", "==", state[0]["_uuid"]]],
                        "mutations": [
                            ["associated_clients", "insert", ["set", [["named-uuid", "sta"]]]]
                        ],
                    },
                ]
            )
        clock = Clock()
        watch = ProbeWatch(
            "pod-1",
            WatchBackend("pod-1", session, serial=SERIAL),
            Store(tmp_path / "probe-watch"),
            SecretStore(tmp_path / "secrets"),
            if_name=state[0]["if_name"],
            band="5G",
            run_id="r",
            clock=ManualClock(),
            monotonic=clock,
        )
        yield watch, admin, clock, session
    finally:
        await session.close()
        await admin.close()
        await database.close()


async def clients(admin):
    return {
        r["mac"]: r
        for r in await rows(
            admin, "Band_Steering_Clients", ["mac", "cs_mode", "cs_params", "sticky_kick_type"]
        )
    }


async def settle(watch):
    for _ in range(3):
        await watch.tick()


def test_asked_stations_are_watched_with_marked_inert_rows(tmp_path):
    async def scenario():
        async with pod(tmp_path) as (watch, admin, clock, _):
            watch.ask([A, B])
            await settle(watch)
            found = await clients(admin)
            assert set(found) == {A, B}
            row = found[A]
            assert row["cs_mode"] == "off" and row["cs_params"] == ["map", [["emosa", "watch"]]]
            assert row["sticky_kick_type"] == ["set", []]  # left out: "none" makes owm warn
            groups = await rows(admin, "Band_Steering_Config", ["if_name_5g"])
            assert len(groups) == 1
            assert watch.latest().state == State.OBSERVED_APPLIED
            assert watch.status()["watched"] == [A, B]

    asyncio.run(scenario())


def test_the_set_follows_the_queries_and_forgets_after_ttl(tmp_path):
    async def scenario():
        async with pod(tmp_path) as (watch, admin, clock, _):
            watch.ask([A, B])
            await settle(watch)
            clock.now += TTL - 5
            watch.ask([C])
            await settle(watch)
            assert set(await clients(admin)) == {A, B, C}
            clock.now += 10  # A and B not asked for longer than TTL
            await settle(watch)
            assert set(await clients(admin)) == {C}
            # the group stays for the next watch (or a steering window)
            assert len(await rows(admin, "Band_Steering_Config", ["_uuid"])) == 1

    asyncio.run(scenario())


def test_writes_are_spaced(tmp_path):
    async def scenario():
        async with pod(tmp_path) as (watch, admin, clock, _):
            watch.ask([A])
            await settle(watch)
            watch.ask([B])
            await settle(watch)
            assert set(await clients(admin)) == {A}
            assert watch.status()["waiting"] == "next write after the minimum interval"
            clock.now += MIN_INTERVAL
            await settle(watch)
            assert set(await clients(admin)) == {A, B}

    asyncio.run(scenario())


def test_associated_and_foreign_stations_are_not_watched(tmp_path):
    async def scenario():
        async with pod(tmp_path, associated=(A,)) as (watch, admin, clock, _):
            await admin.transact(
                [
                    {
                        "op": "insert",
                        "table": "Band_Steering_Clients",
                        "row": {**CLIENT_ROW, "mac": B, "cs_mode": "home"},
                    }
                ]
            )
            watch.ask([A, B, C])
            await settle(watch)
            found = await clients(admin)
            assert set(found) == {B, C} and found[B]["cs_mode"] == "home"  # B left as it was
            assert watch.status()["watched"] == [C]

    asyncio.run(scenario())


def test_a_steering_window_replaces_the_stations_watch_row(tmp_path):
    async def scenario():
        async with pod(tmp_path, associated=(STA.hex(":"),)) as (watch, admin, clock, session):
            # watched while it was not yet associated: the row is there
            await watch.backend.session.transact(
                [
                    {
                        "op": "insert",
                        "table": "Band_Steering_Clients",
                        "row": {
                            **{k: v for k, v in CLIENT_ROW.items() if k != "sticky_kick_type"},
                            "mac": STA.hex(":"),
                            "cs_mode": "off",
                            "cs_params": ["map", [[k, v] for k, v in MARKER.items()]],
                        },
                    }
                ]
            )
            steering = ClientSteering(
                "pod-1",
                SteeringBackend("pod-1", session, serial=SERIAL),
                Store(tmp_path / "steering"),
                SecretStore(tmp_path / "secrets"),
                run_id="r",
                clock=ManualClock(),
            )
            assert steering.start(mandate(), 900) is None
            for _ in range(3):
                await steering.tick()
            found = await clients(admin)
            assert found[STA.hex(":")]["cs_mode"] == "away"
            # and the watch leaves the associated station alone
            await settle(watch)
            assert (await clients(admin))[STA.hex(":")]["cs_mode"] == "away"

    asyncio.run(scenario())


def test_the_intent_round_trips_through_the_journal():
    intent = WatchIntent("pod-1", "home-ap-24", "2.4G", (B, A))
    assert intent.stations == (A, B)
    assert WatchIntent(**intent.record()) == intent
    assert intent.target() == {"watched": f"{A},{B}"}
