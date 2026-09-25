import asyncio
import copy
import json
import time
from dataclasses import replace

import pytest

from emosa.errors import EmosaError
from emosa.wire.cmdu import MULTICAST, Tlv, fragment_message
from emosa_lab.simulation.forwarding import ForwardingSource
from emosa_lab.simulation.neighbor_binding import (
    NeighborSource,
    bridge_discovery,
    topology_discovery,
)
from test_forwarding import observation, snapshot

CONTROLLER, LOCAL, PEER = (
    bytes.fromhex(v) for v in ("020000e00001", "020000003001", "00163eb8c4a7")
)


def discovery(controller=CONTROLLER, peer=PEER):
    return fragment_message(MULTICAST, controller, 0, 0, (Tlv(1, controller), Tlv(2, peer)))[0]


def lldp(controller=CONTROLLER, peer=PEER):
    return (
        bytes.fromhex("0180c200000e")
        + controller
        + b"\x88\xcc"
        + b"\x02\x07\x04"
        + controller
        + b"\x04\x07\x03"
        + peer
        + b"\x06\x02\x00\xb4\0\0"
    )


def sample(now=12_100_000_000, frames=None):
    value = observation(start=now - 100_000_000)
    value["neighbor_observation"] = {
        "running": True,
        "errors": [],
        "run_label": "neighbor-unit-01",
        "interface": "eth1",
        "ifindex": 8,
        "mac": "00:16:3e:00:00:01",
        "boot_id": value["boot_id"],
        "netns_inode": value["netns_inode"],
        "capture_epoch": "d840d6dd-6d73-4665-94d2-85719a5144b1",
        "heartbeat_ns": now - 50_000_000,
        "frames": frames
        if frames is not None
        else [{"received_ns": 10_000_000_000, "frame_hex": discovery().hex()}],
    }
    source = ForwardingSource(lambda: now)
    source.refresh(snapshot(value))
    assert source.sample is not None
    return source.sample


def source(now=12_100_000_000):
    return NeighborSource(CONTROLLER, LOCAL, "neighbor-unit-01", clock=lambda: now)


@pytest.mark.unit
def test_passive_binding_uses_pod_port_and_peer_tlv_not_al_or_proxy_mac():
    reader = source()
    reader.refresh(sample())
    bound = reader.current()
    assert bound.local_interface == "00:16:3e:00:00:01"
    assert bound.neighbor_interface == "00:16:3e:b8:c4:a7"
    assert bound.neighbor_al == "02:00:00:e0:00:01"
    assert bound.bridges_present is True
    assert reader.status()["measurement_source_qualified"] is False


@pytest.mark.unit
def test_matching_lldp_distinguishes_direct_link_and_ignores_padding():
    frames = sample().neighbor_observation["frames"] + [
        {"received_ns": 10_100_000_000, "frame_hex": (lldp() + b"\0" * 20).hex()}
    ]
    assert bridge_discovery(lldp()) == (CONTROLLER, PEER)
    reader = source()
    reader.refresh(sample(frames=frames))
    assert reader.current().bridges_present is False
    # A different interface on the same device does not establish a direct link.
    frames[-1]["frame_hex"] = lldp(peer=bytes.fromhex("00163eb8c4a8")).hex()
    reader = source()
    reader.refresh(sample(frames=frames))
    assert reader.current().bridges_present is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "field,value",
    [
        ("running", False),
        ("errors", ["packet_socket_drops"]),
        ("ifindex", 9),
        ("mac", "02:00:00:00:30:01"),
        ("boot_id", "other"),
        ("netns_inode", 42),
        ("interface", "eth2"),
        ("run_label", "different-run"),
        ("heartbeat_ns", 1),
        ("heartbeat_ns", 99_000_000_000),
        ("capture_epoch", "invalid"),
    ],
)
def test_listener_health_freshness_and_identity_must_match_forwarding_sample(field, value):
    reader = source()
    value_sample = sample()
    reader.refresh(value_sample)
    assert reader.current()
    broken = copy.deepcopy(value_sample)
    broken.neighbor_observation[field] = value
    reader.refresh(broken)
    assert reader.current() is None


@pytest.mark.unit
def test_stale_discovery_cannot_be_refreshed_by_healthy_listener_heartbeat():
    now = 76_000_000_000
    reader = source(now)
    reader.refresh(sample(now))
    assert reader.current() is None


@pytest.mark.unit
def test_initial_lldp_settle_is_bounded_and_periodic_refresh_does_not_reopen_it():
    frames = [{"received_ns": 12_000_000_000, "frame_hex": discovery().hex()}]
    reader = source()
    reader.refresh(sample(frames=frames))
    assert reader.current() is None and reader.reason == "awaiting_bridge_discovery"
    # The previous observation already established the bridged-path inference.
    frames.insert(0, {"received_ns": 10_000_000_000, "frame_hex": discovery().hex()})
    reader = source()
    reader.refresh(sample(frames=frames))
    assert reader.current().bridges_present


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        discovery(peer=bytes.fromhex("00163eb8c4a8")),
        discovery(controller=bytes.fromhex("020000e00002")),
    ],
)
def test_additional_live_peer_or_interface_exceeds_the_selected_sole_backhaul_profile(extra):
    frames = sample().neighbor_observation["frames"] + [
        {"received_ns": 11_000_000_000, "frame_hex": extra.hex()}
    ]
    reader = source()
    reader.refresh(sample(frames=frames))
    assert reader.current() is None and reader.reason == "awaiting_or_ambiguous_discovery"


@pytest.mark.unit
def test_clock_deadlines_replay_and_capture_epoch_are_separate_from_connection_generation():
    now = [12_100_000_000]
    reader = NeighborSource(CONTROLLER, LOCAL, "neighbor-unit-01", clock=lambda: now[0])
    first = sample(now[0])
    reader.refresh(first)
    assert reader.current().generation == 1
    now[0] += 100_000_000
    second = replace(sample(now[0]), generation=2)
    reader.refresh(second)
    assert reader.current().generation == 2
    reader.invalidate()
    reader.refresh(first)
    assert reader.current() is None  # Older heartbeat after invalidation.
    now[0] += 100_000_000
    third = sample(now[0])
    reader.refresh(third)
    assert reader.current()
    altered = copy.deepcopy(third)
    altered.neighbor_observation["frames"] = []
    reader.refresh(altered)
    assert reader.current() is None  # Same heartbeat, changed payload.
    reader.refresh(third)
    assert reader.current()
    now[0] += 2_000_000_000
    assert reader.current() is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutation", ["relay", "fragment", "reserved_version", "destination", "identity", "tlv"]
)
def test_invalid_discovery_does_not_create_an_observed_link(mutation):
    frame = bytearray(discovery())
    if mutation == "relay":
        frame[21] |= 0x40
    elif mutation == "fragment":
        frame[20] = 1
    elif mutation == "reserved_version":
        frame[14] = 1
    elif mutation == "destination":
        frame[0] = 2
    elif mutation == "identity":
        frame[6] = 4
    else:
        frame[23:25] = b"\0\x40"
    with pytest.raises((ValueError, EmosaError)):
        topology_discovery(bytes(frame))
    reader = source()
    reader.refresh(sample(frames=[{"received_ns": 10_000_000_000, "frame_hex": frame.hex()}]))
    assert reader.current() is None


@pytest.mark.unit
def test_reserved_bits_and_unknown_tlvs_do_not_hide_valid_discovery():
    frame = bytearray(discovery())
    frame[15] = 255
    frame[21] |= 15
    frame[23] |= 0xC0
    assert topology_discovery(bytes(frame)) == (CONTROLLER, PEER)


@pytest.mark.unit
def test_collector_guard_runs_before_socket_or_evidence_mutation(monkeypatch):
    import runpy

    module = runpy.run_path("deploy/radio-manager/neighbor-observer.py")

    def refused():
        raise RuntimeError("unowned host")

    monkeypatch.setattr(module["runpy"], "run_path", lambda _: {"guard": refused})
    monkeypatch.setattr(module["socket"], "socket", lambda *_: pytest.fail("socket before guard"))
    with pytest.raises(RuntimeError, match="unowned host"):
        module["observe"]("refused-test", 100)


@pytest.mark.unit
def test_passive_all_protocol_tap_retains_only_discovery_and_lldp():
    import runpy

    wanted = runpy.run_path("deploy/radio-manager/neighbor-observer.py")["wanted"]
    assert wanted(discovery()) and wanted(lldp())
    assert not wanted(b"\0" * 12 + b"\x08\x00" + b"client payload" * 100)
    assert not wanted(fragment_message(MULTICAST, CONTROLLER, 9, 1, (Tlv(17, b"WSC bytes"),))[0])
    assert not wanted(b"\0" * 12 + b"\x89\x3a\0\0\0\0")


@pytest.mark.unit
def test_frame_and_heartbeat_budgets_remain_bounded():
    reader = source()
    first = sample()
    first.neighbor_observation["frames"] *= 65
    reader.refresh(first)
    assert reader.current() is None
    for index in range(10):
        item = sample()
        item.neighbor_observation["capture_epoch"] = f"00000000-0000-0000-0000-{index:012x}"
        reader.refresh(item)
    assert len(reader.watermarks) == 8 and reader.current() is None
    assert len(json.dumps(reader.status())) < 1024


@pytest.mark.ovsdb
def test_native_topology_uses_observed_interfaces_and_withdraws_only_stale_topology(tmp_path):
    from emosa.opensync.session import OvsSession
    from emosa.secrets import SecretStore
    from emosa.wire.autoconfiguration import PeerBinding
    from emosa.wire.cmdu import Message, Reassembler
    from emosa.wire.reports import topology_response
    from emosa.wire.topology_values import decode_topology
    from emosa_lab.simulation.database import SimDatabase
    from emosa_lab.simulation.native_onboarding import AGENT, RADIO_BSSID, RadioReportSource
    from emosa_lab.simulation.radio import MONITOR, RadioManager, seed_radio_database
    from emosa_lab.simulation.wsc_provisioning import SERIAL, BoundBackend
    from test_radio_manager import observation as radio_observation

    class Driver:
        stale = False

        async def apply(self, config):
            pass

        async def observe(self):
            now = time.monotonic_ns()
            ports = observation(now - 100_000_000)
            for key in ("links", "links_after"):
                ports[key][3]["address"] = RADIO_BSSID
            ports["neighbor_observation"] = sample(now).neighbor_observation
            ports["neighbor_observation"]["frames"][0]["received_ns"] = now - 2_000_000_000
            if self.stale:
                ports["neighbor_observation"]["heartbeat_ns"] -= 3_000_000_000
            return radio_observation() | {"forwarding": ports}

    async def scenario():
        db = await SimDatabase(tmp_path / "db").start()
        session = OvsSession(db.endpoint, monitor_columns=MONITOR)
        try:
            await seed_radio_database(session)
            await session.transact(
                [{"op": "insert", "table": "AWLAN_Node", "row": {"serial_number": SERIAL}}]
            )
            driver = Driver()
            manager = RadioManager(session, driver)
            backend = BoundBackend(
                session,
                SecretStore(tmp_path / "secrets"),
                radio_mac=RADIO_BSSID,
                bssid=RADIO_BSSID,
                if_name="wlan0",
                radio_name="phy1",
            )
            binding = PeerBinding("probe0", 1, AGENT, CONTROLLER, (CONTROLLER,))
            facts = RadioReportSource(backend, binding, forwarding_run="neighbor-unit-01")
            await manager.cycle()
            assert await facts.refresh()
            first = facts.source.current()
            assert first.topology.inventory_complete and facts.neighbor.current()
            query = Message(AGENT, CONTROLLER, 2, 8, False, (Tlv(0xB3, b"\x02"),), 1)
            reply = topology_response(
                query,
                binding,
                first.topology,
                first.stamp,
                ingress="probe0",
                generation=1,
                received_at=time.monotonic(),
            )
            decoded = Reassembler().feed(reply.frames[0])
            device = decode_topology(3, next(t.value for t in decoded.tlvs if t.kind == 3))
            assert [i.mac.hex(":") for i in device.interfaces] == [
                "00:16:3e:00:00:01",
                "00:16:3e:00:00:02",
                RADIO_BSSID,
            ]
            neighbor = decode_topology(7, next(t.value for t in decoded.tlvs if t.kind == 7))
            assert neighbor.local_interface == device.interfaces[0].mac
            assert (
                neighbor.neighbors[0].al_mac == CONTROLLER and neighbor.neighbors[0].bridges_present
            )
            driver.stale = True
            await manager.cycle()
            assert await facts.refresh()
            stale = facts.source.current()
            assert (
                stale.context_token == first.context_token and not stale.topology.inventory_complete
            )
            assert facts.neighbor.current() is None
            driver.stale = False
            await manager.cycle()
            assert await facts.refresh()
            assert facts.source.current().topology.inventory_complete
            assert facts.source.current().context_token == first.context_token
        finally:
            await session.close()
            await db.close()

    asyncio.run(scenario())
