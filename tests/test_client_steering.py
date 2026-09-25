"""Client steering: the Client Steering Request on the wire, and the pod's steering window."""

import asyncio
import struct
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from emosa.agent.pod import MONITOR
from emosa.agent.steering import APPLY, GENTLE, ClientSteering
from emosa.clock import ManualClock
from emosa.easymesh_payloads import AssociatedClient, AssociatedClients, BssClients
from emosa.errors import EmosaError
from emosa.model import State
from emosa.opensync.session import OvsSession
from emosa.opensync.steering import CLIENT_ROW, SteeringBackend
from emosa.secrets import SecretStore
from emosa.store import Store
from emosa.wire.cmdu import Tlv, fragment_message
from emosa.wire.steering import SteeringRequest, decode_request
from emosa_lab.simulation.database import SimDatabase
from test_autoconfiguration import BINDING, assemble
from test_autoconfiguration import fixed_entropy as fixed_entropy
from test_native_onboarding import lifecycle, receive, response
from test_wsc_operation_bridge import rig as rig

STA = bytes.fromhex("020000006c00")
TARGET = bytes.fromhex("02000012752c")


def steering_tlv(source, stations, targets, *, mandate=True, imminent=True, window=5):
    flags = (0x80 if mandate else 0) | (0x40 if imminent else 0) | 0x20
    value = source + bytes([flags]) + struct.pack("!HH", window, 5)
    value += bytes([len(stations)]) + b"".join(stations)
    value += bytes([len(targets)]) + b"".join(b + bytes([c, n]) for b, c, n in targets)
    return Tlv(0x9B, value)


def request_frame(source, stations, targets, mid=900, **flags):
    tlv = steering_tlv(source, stations, targets, **flags)
    return fragment_message(BINDING.local_al, BINDING.controller_al, 0x8014, mid, (tlv,))[0]


# -- the wire ---------------------------------------------------------------------------


def test_the_steering_request_tlv_decodes_completely():
    source = bytes.fromhex("820000006d00")
    request = decode_request((steering_tlv(source, [STA], [(TARGET, 81, 6)], window=50),))
    assert request == SteeringRequest(source, True, True, True, 50, 5, (STA,), ((TARGET, 81, 6),))
    with pytest.raises(EmosaError):
        decode_request((Tlv(0x9B, steering_tlv(source, [STA], [(TARGET, 81, 6)]).value[:-1]),))
    with pytest.raises(EmosaError):  # a multicast station
        decode_request((steering_tlv(source, [b"\x01" + STA[1:]], [(TARGET, 81, 6)]),))


def provisioned(rig, executor):
    """A session past its M2, the station associated with the pod's first BSS."""
    _, _, _, clock = rig
    session, source, sent = lifecycle(rig)
    session.steering_executor = executor
    original = source.current()
    bssid = original.topology.operational.radios[0].bsses[0].ap_mac
    joined = replace(
        original.topology,
        clients=AssociatedClients((BssClients(bssid, (AssociatedClient(STA, 20),)),)),
        inventory_complete=True,
    )
    source.publish((1, 2), original.capabilities, joined, observed_at=clock.monotonic())
    return session, sent, bssid


def test_a_mandate_is_acknowledged_and_handed_to_the_pod(rig):
    async def scenario():
        calls = []
        session, sent, bssid = provisioned(rig, lambda r, mid: calls.append((r, mid)))
        await session.tick()
        await receive(session, response())
        session.state = "provisioning"
        count = len(sent)
        frame = request_frame(bssid, [STA], [(TARGET, 81, 6)])
        assert await receive(session, frame) == "client_steering_started"
        (ack,) = (assemble((f,)) for f in sent[count:])
        assert (ack.message_type, ack.mid, ack.tlvs) == (0x8000, 900, ())
        ((request, mid),) = calls
        assert mid == 900 and request.stations == (STA,) and request.targets[0][0] == TARGET
        # a re-delivered request is acknowledged again, not carried out twice
        assert await receive(session, frame) == "client_steering_repeated_ack_sent"
        assert len(calls) == 1 and assemble((sent[-1],)).message_type == 0x8000
        session.close()

    asyncio.run(scenario())


def test_a_station_not_on_the_source_gets_an_error_code_and_no_steering(rig):
    async def scenario():
        calls = []
        session, sent, bssid = provisioned(rig, lambda r, mid: calls.append(r))
        await session.tick()
        await receive(session, response())
        session.state = "provisioning"
        other = bytes.fromhex("020000009999")
        frame = request_frame(bssid, [other], [(TARGET, 81, 6)], mid=901)
        assert await receive(session, frame) == "client_steering_refused_station_not_associated"
        ack = assemble((sent[-1],))
        assert ack.message_type == 0x8000 and ack.tlvs == (Tlv(0xA3, b"\x02" + other),)
        assert not calls
        session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("stations", "targets", "reason"),
    [
        ([STA, bytes.fromhex("020000009999")], [(TARGET, 81, 6)], "not_one_station"),
        ([STA], [(b"\xff" * 6, 0, 0)], "agent_selected_target"),
        ([STA], [(TARGET, 81, 6), (bytes.fromhex("020000127530"), 81, 6)], "not_one_target"),
    ],
)
def test_what_emosa_cannot_carry_out_is_acknowledged_and_counted(rig, stations, targets, reason):
    async def scenario():
        calls = []
        session, sent, bssid = provisioned(rig, lambda r, mid: calls.append(r))
        await session.tick()
        await receive(session, response())
        session.state = "provisioning"
        result = await receive(session, request_frame(bssid, stations, targets, mid=902))
        assert result == f"client_steering_refused_{reason}"
        assert assemble((sent[-1],)).message_type == 0x8000 and not calls
        session.close()

    asyncio.run(scenario())


def test_an_opportunity_is_completed_at_once(rig):
    async def scenario():
        calls = []
        session, sent, bssid = provisioned(rig, lambda r, mid: calls.append(r))
        await session.tick()
        await receive(session, response())
        session.state = "provisioning"
        count = len(sent)
        frame = request_frame(bssid, [STA], [(TARGET, 81, 6)], mid=903, mandate=False)
        assert await receive(session, frame) == "client_steering_opportunity_completed"
        ack, completed = (assemble((f,)) for f in sent[count:])
        assert (ack.message_type, ack.mid) == (0x8000, 903)
        assert completed.message_type == 0x8017 and completed.tlvs == ()
        assert not calls
        session.close()

    asyncio.run(scenario())


def test_a_busy_executor_is_reported(rig):
    async def scenario():
        session, _, bssid = provisioned(rig, lambda r, mid: "busy")
        await session.tick()
        await receive(session, response())
        session.state = "provisioning"
        frame = request_frame(bssid, [STA], [(TARGET, 81, 6)], mid=904)
        assert await receive(session, frame) == "client_steering_refused_busy"
        session.close()

    asyncio.run(scenario())


# -- the pod ----------------------------------------------------------------------------

SERIAL = "EMOSA-STEER-1"
SOURCE = bytes.fromhex("020000001001")  # the seeded AP VIF lab-ap (5G)


@asynccontextmanager
async def pod(tmp_path):
    database = await SimDatabase().start()
    session = OvsSession(database.endpoint, monitor_columns=MONITOR)
    admin = OvsSession(database.endpoint)
    try:
        await database.seed(serial_number=SERIAL, sole_radio=True)
        state = await rows(admin, "Wifi_VIF_State", ["_uuid"])
        await admin.transact(
            [
                {
                    "op": "insert",
                    "table": "Wifi_Associated_Clients",
                    "uuid-name": "sta",
                    "row": {"mac": STA.hex(":"), "state": "active"},
                },
                {
                    "op": "update",
                    "table": "Wifi_VIF_State",
                    "where": [["_uuid", "==", state[0]["_uuid"]]],
                    "row": {"associated_clients": ["set", [["named-uuid", "sta"]]]},
                },
            ]
        )
        clock = ManualClock()
        steering = ClientSteering(
            "pod-1",
            SteeringBackend("pod-1", session, serial=SERIAL),
            Store(tmp_path / "steering"),
            SecretStore(tmp_path / "secrets"),
            run_id="r",
            clock=clock,
        )
        yield steering, admin, clock
    finally:
        await session.close()
        await admin.close()
        await database.close()


async def rows(admin, table, columns):
    (result,) = await admin.transact(
        [{"op": "select", "table": table, "where": [], "columns": columns}]
    )
    return result["rows"]


async def owm(admin, **row):
    """The pod's owm updating the station's client row."""
    await admin.transact(
        [
            {
                "op": "update",
                "table": "Band_Steering_Clients",
                "where": [["mac", "==", STA.hex(":")]],
                "row": row,
            }
        ]
    )


async def leave(admin):
    await admin.transact(
        [
            {
                "op": "update",
                "table": "Wifi_VIF_State",
                "where": [],
                "row": {"associated_clients": ["set", []]},
            },
            {"op": "delete", "table": "Wifi_Associated_Clients", "where": []},
        ]
    )


def mandate(imminent=True, window=5):
    return SteeringRequest(SOURCE, True, imminent, True, window, 5, (STA,), ((TARGET, 81, 6),))


@pytest.mark.ovsdb
def test_a_mandate_opens_a_window_kicks_and_closes_it(tmp_path):
    async def scenario():
        async with pod(tmp_path) as (steering, admin, clock):
            assert steering.start(mandate(), 900) is None
            assert steering.start(mandate(), 901) == "busy"
            await steering.tick()
            op = steering.store.operations()[-1]
            assert op.state == State.CONFIG_COMMITTED
            (client,) = await rows(
                admin,
                "Band_Steering_Clients",
                [
                    "mac",
                    "cs_mode",
                    "cs_params",
                    "sc_kick_type",
                    "sc_btm_params",
                    "force_kick",
                    "hwm",
                    "pref_5g",
                ],
            )
            assert client["mac"] == STA.hex(":") and client["cs_mode"] == "away"
            assert client["cs_params"] == ["map", [["cs_enforce_period", "15"]]]
            assert client["sc_kick_type"] == "btm_deauth"
            assert client["sc_btm_params"] == [
                "map",
                [["bssid", TARGET.hex(":")], ["disassoc_imminent", "1"]],
            ]
            assert client["hwm"] == CLIENT_ROW["hwm"] and client["pref_5g"] == "never"
            (group,) = await rows(admin, "Band_Steering_Config", ["if_name_5g"])
            assert group["if_name_5g"] == "lab-ap"
            (neighbor,) = await rows(
                admin, "Wifi_VIF_Neighbors", ["bssid", "if_name", "channel", "op_class", "ht_mode"]
            )
            assert (neighbor["bssid"], neighbor["if_name"], neighbor["channel"]) == (
                TARGET.hex(":"),
                "lab-ap",
                6,
            )
            await steering.tick()
            assert steering.status()["active"]["phase"] == "opening"  # owm has not taken it
            await owm(admin, cs_state="steering")
            await steering.tick()
            assert steering.store.get(op.operation_id).state == State.OBSERVED_APPLIED
            (client,) = await rows(admin, "Band_Steering_Clients", ["force_kick"])
            assert client["force_kick"] == "directed"
            await leave(admin)
            await owm(admin, cs_state="expired")
            await steering.tick()
            assert steering.job is None
            assert steering.history[-1]["outcome"] == "left_source"
            for table in ("Band_Steering_Clients", "Band_Steering_Config", "Wifi_VIF_Neighbors"):
                assert await rows(admin, table, ["_uuid"]) == []
            assert steering.start(mandate(), 902) is None  # ready for the next one

    asyncio.run(scenario())


@pytest.mark.ovsdb
def test_another_managers_steering_row_is_not_taken_over(tmp_path):
    async def scenario():
        async with pod(tmp_path) as (steering, admin, _):
            await admin.transact(
                [
                    {
                        "op": "insert",
                        "table": "Band_Steering_Clients",
                        "row": {**CLIENT_ROW, "mac": STA.hex(":"), "kick_type": "deauth"},
                    }
                ]
            )
            assert steering.start(mandate(), 900) is None
            await steering.tick()
            assert steering.history[-1]["outcome"] == "refused_ownership_conflict"
            assert len(await rows(admin, "Band_Steering_Clients", ["_uuid"])) == 1
            assert await rows(admin, "Wifi_VIF_Neighbors", ["_uuid"]) == []

    asyncio.run(scenario())


@pytest.mark.ovsdb
def test_existing_group_and_neighbor_are_reused_and_left_in_place(tmp_path):
    async def scenario():
        async with pod(tmp_path) as (steering, admin, _):
            await admin.transact(
                [
                    {
                        "op": "insert",
                        "table": "Band_Steering_Config",
                        "row": {"if_name_5g": "lab-ap"},
                    },
                    {
                        "op": "insert",
                        "table": "Wifi_VIF_Neighbors",
                        "row": {"bssid": TARGET.hex(":"), "if_name": "lab-ap", "channel": 6},
                    },
                ]
            )
            steering.start(mandate(), 900)
            await steering.tick()
            await owm(admin, cs_state="steering")
            await steering.tick()
            await owm(admin, cs_state="expired")
            await steering.tick()
            assert steering.history[-1]["outcome"] == "stayed"
            assert await rows(admin, "Band_Steering_Clients", ["_uuid"]) == []
            assert len(await rows(admin, "Band_Steering_Config", ["_uuid"])) == 1
            assert len(await rows(admin, "Wifi_VIF_Neighbors", ["_uuid"])) == 1

    asyncio.run(scenario())


@pytest.mark.ovsdb
def test_a_gentle_request_closes_before_owms_deauthentication(tmp_path):
    async def scenario():
        async with pod(tmp_path) as (steering, admin, clock):
            steering.start(mandate(imminent=False, window=50), 900)
            await steering.tick()
            (client,) = await rows(admin, "Band_Steering_Clients", ["sc_btm_params", "cs_params"])
            assert ["disassoc_imminent", "0"] in client["sc_btm_params"][1]
            assert client["cs_params"] == ["map", [["cs_enforce_period", "50"]]]
            await owm(admin, cs_state="steering")
            await steering.tick()
            clock.advance(GENTLE - 1)
            await steering.tick()
            assert steering.job is not None
            clock.advance(2)
            await steering.tick()
            assert steering.history[-1]["outcome"] == "stayed"
            assert await rows(admin, "Band_Steering_Clients", ["_uuid"]) == []

    asyncio.run(scenario())


@pytest.mark.ovsdb
def test_a_window_owm_never_takes_is_closed_and_the_scope_is_free_again(tmp_path):
    async def scenario():
        async with pod(tmp_path) as (steering, admin, clock):
            steering.start(mandate(), 900)
            await steering.tick()
            clock.advance(APPLY + 1)
            await steering.tick()
            assert steering.history[-1]["outcome"] == "not_applied"
            assert await rows(admin, "Band_Steering_Clients", ["_uuid"]) == []
            assert steering.start(mandate(), 901) is None

    asyncio.run(scenario())


@pytest.mark.ovsdb
def test_a_window_left_open_by_a_previous_process_is_closed(tmp_path):
    async def scenario():
        async with pod(tmp_path) as (steering, admin, clock):
            steering.start(mandate(), 900)
            await steering.tick()
            assert len(await rows(admin, "Band_Steering_Clients", ["_uuid"])) == 1
            again = ClientSteering(
                "pod-1",
                steering.backend,
                steering.store,
                SecretStore(tmp_path / "secrets"),
                run_id="r",
                clock=clock,
            )
            await again.tick()
            for table in ("Band_Steering_Clients", "Band_Steering_Config", "Wifi_VIF_Neighbors"):
                assert await rows(admin, table, ["_uuid"]) == []
            assert again.start(mandate(), 901) is None  # not busy with the old window
            # a later restart leaves the closed window alone, only the new one is open
            third = ClientSteering(
                "pod-1",
                steering.backend,
                steering.store,
                SecretStore(tmp_path / "secrets"),
                run_id="r",
                clock=clock,
            )
            assert [op.operation_id for op in third.leftover] == [again.job["op"]]

    asyncio.run(scenario())
