import asyncio
import copy
import runpy
import struct

import pytest
from test_egress_accounting import ADDRESS, payload, refresh
from test_forwarding import observation as forwarding_observation

from emosa.opensync.session import OvsSession
from emosa.simulation.backhaul_accounting import BackhaulAccountingSource
from emosa.simulation.database import SimDatabase
from emosa.simulation.forwarding import ForwardingSource, updates
from emosa.simulation.radio import MONITOR, seed_radio_database


def joint(start=10_000_000_000, *, rx_packets=100, rx_drops=0, tx_drops=0):
    value = payload(start, drops=tx_drops)
    obs = value["observation"]
    rx = obs["link"][0]["stats64"]["rx"]
    rx.update(packets=rx_packets, bytes=rx_packets * 98, dropped=0, errors=0)
    obs["filters"]["ingress"] = copy.deepcopy(obs["filters"]["egress"])
    action = obs["filters"]["ingress"][1]["options"]
    action["keys"].update(src_ip="192.0.2.1", dst_ip="192.0.2.20")
    action["actions"][0]["index"] = 2
    action["actions"][0]["stats"].update(drops=rx_drops, packets=rx_drops)
    return value


@pytest.mark.unit
def test_common_interval_counts_arrivals_and_separate_bidirectional_losses():
    source = BackhaulAccountingSource("test-egress", clock=lambda: 10_600_000_000)
    refresh(source, joint())
    refresh(source, joint(10_500_000_000, rx_packets=117, rx_drops=17, tx_drops=3))
    status = source.status()
    window = status["window"]
    assert status["available"] and window["receive_losses"] == 17
    assert window["transmit_losses"] == 3
    assert window["deltas"]["rx_packets"] == 17  # Not reduced to zero client deliveries.
    assert window["deltas"]["rx_bytes"] == 1666
    assert window["deltas"]["rx_interface_drops"] == 0
    assert not status["measurement_source_qualified"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    [
        "wrong_direction",
        "shared",
        "cross_hook_shared",
        "root_filter",
        "rx_error",
        "rx_negative",
        "rx_reset",
        "epoch",
    ],
)
def test_ingress_accounting_refuses_inconsistent_paths_and_counter_lifetimes(case):
    source = BackhaulAccountingSource("test-egress", clock=lambda: 10_600_000_000)
    refresh(source, joint(rx_drops=8))
    value = joint(10_500_000_000, rx_packets=117, rx_drops=17)
    obs = value["observation"]
    config = obs["filters"]["ingress"][1]["options"]
    if case == "wrong_direction":
        config["keys"]["src_ip"] = "192.0.2.20"
    elif case == "shared":
        config["actions"][0]["ref"] = 2
    elif case == "cross_hook_shared":
        config["actions"][0]["index"] = 1
    elif case == "root_filter":
        obs["filters"]["root"] = [{"kind": "bpf"}]
    elif case == "rx_error":
        obs["link"][0]["stats64"]["rx"]["errors"] = 1
    elif case == "rx_negative":
        obs["link"][0]["stats64"]["rx"]["dropped"] = -1
    elif case == "rx_reset":
        obs["link"][0]["stats64"]["rx"]["packets"] = 1
    elif case == "epoch":
        value["configuration_epoch"] += 1
    refresh(source, value)
    assert not source.status()["available"] and source.window is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "case", ["normal", "wrong_interface", "wrong_media", "wrong_direction", "vlan"]
)
def test_independent_cooked_capture_reader_keeps_direction_and_ethernet_size(tmp_path, case):
    audit = runpy.run_path("scripts/check-receive-accounting.py")
    index, kind, media, protocol = 9, 4, 1, 0x800
    if case == "wrong_interface":
        index = 8
    elif case == "wrong_media":
        media = 772
    elif case == "wrong_direction":
        kind = 7
    elif case == "vlan":
        protocol = 0x8100
    frame = struct.pack("!HHIHBB8s", protocol, 0, index, media, kind, 6, b"\0" * 8) + bytes(84)
    pcap = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 276)
    pcap += struct.pack("<IIII", 1, 0, len(frame), len(frame)) + frame
    path = tmp_path / "receive.pcap"
    path.write_bytes(pcap)
    if case == "normal":
        assert audit["cooked_frames"](path, 9) == [(1_000_000_000, "rx", 98)]
    else:
        with pytest.raises(AssertionError):
            audit["cooked_frames"](path, 9)


@pytest.mark.ovsdb
def test_ingress_observation_crosses_real_ovsdb_and_generation_starts_new_baseline(tmp_path):
    async def run():
        db = await SimDatabase(tmp_path / "db").start()
        session = OvsSession(db.endpoint, monitor_columns=MONITOR)
        now = [10_100_000_000]
        forwarding = ForwardingSource(lambda: now[0])
        source = BackhaulAccountingSource("test-egress", clock=lambda: now[0])
        try:
            await seed_radio_database(session)
            for start, drops in ((10_000_000_000, 0), (10_500_000_000, 17)):
                value = forwarding_observation(start)
                value["links"][1]["address"] = value["links_after"][1]["address"] = ADDRESS
                value["egress_observation"] = joint(start, rx_packets=100 + drops, rx_drops=drops)
                ops, _ = updates(await session.snapshot(), value)
                assert all("error" not in v for v in await session.transact(ops))
                now[0] = start + 100_000_000
                forwarding.refresh(await session.snapshot())
                sample = forwarding.sample
                refresh(source, sample.egress_observation, sample.generation)
            assert source.status()["window"]["receive_losses"] == 17
            refresh(source, sample.egress_observation, sample.generation + 1)
            assert source.window is None
        finally:
            await session.close()
            await db.close()

    asyncio.run(run())
