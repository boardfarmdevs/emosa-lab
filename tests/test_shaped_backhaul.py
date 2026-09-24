import asyncio
import copy

import pytest

from emosa.opensync.session import OvsSession
from emosa_lab.simulation.database import SimDatabase
from emosa_lab.simulation.forwarding import ForwardingSource, updates
from emosa_lab.simulation.radio import MONITOR, seed_radio_database
from emosa_lab.simulation.shaped_backhaul import ShapedBackhaulSource
from test_egress_accounting import ADDRESS, refresh
from test_forwarding import observation as forwarding_observation
from test_receive_accounting import joint
from test_virtual_capacity import observed


def combined(start=10_000_000_000, *, queue=0, tx=0, rx=0):
    value = joint(start, rx_drops=rx, tx_drops=tx)
    root = observed(start)["observation"]["qdiscs"][0]
    root["drops"] = queue
    value["observation"]["qdiscs"][0] = root
    return value


@pytest.mark.unit
def test_disjoint_queue_action_and_driver_losses_share_one_window():
    source = ShapedBackhaulSource("test-egress", clock=lambda: 10_600_000_000)
    refresh(source, combined(queue=10, tx=5, rx=2))
    value = combined(10_500_000_000, queue=27, tx=8, rx=9)
    value["observation"]["link"][0]["stats64"]["tx"]["dropped"] = 2
    value["observation"]["qdiscs"][0]["overlimits"] = 300
    # Parent action bookkeeping is not an additional physical loss.
    value["observation"]["qdiscs"][1]["drops"] = 100
    refresh(source, value)
    status = source.status()
    assert status["window"]["transmit_losses"] == 17 + 3 + 2
    assert status["window"]["receive_losses"] == 7
    assert status["window"]["deltas"]["token_waits"] == 300
    assert status["service_estimate"] is not None
    assert not status["measurement_source_qualified"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "case", ["child", "shared", "root_filter", "rate", "reset", "generation", "stale"]
)
def test_unsupported_path_and_changed_lifetime_never_bridge_samples(case):
    now = [10_600_000_000]
    source = ShapedBackhaulSource("test-egress", clock=lambda: now[0])
    refresh(source, combined(queue=10))
    value = combined(10_500_000_000, queue=20)
    obs = value["observation"]
    generation = 1
    if case == "child":
        obs["qdiscs"].append({"kind": "bfifo", "parent": "4e00:1"})
    elif case == "shared":
        obs["qdiscs"][1]["options"] = {"egress_block": 5}
    elif case == "root_filter":
        obs["filters"]["root"] = [{"kind": "bpf"}]
    elif case == "rate":
        obs["qdiscs"][0]["options"]["rate"] = 125000
    elif case == "reset":
        obs["qdiscs"][0]["drops"] = 0
    elif case == "generation":
        generation += 1
    elif case == "stale":
        now[0] += 3_000_000_000
    refresh(source, value, generation)
    assert source.status()["window"] is None
    assert source.status()["service_estimate"] is None


@pytest.mark.ovsdb
def test_shaped_observations_survive_ovsdb_encoding_and_reset_on_reconnect(tmp_path):
    async def run():
        db = await SimDatabase(tmp_path / "db").start()
        session = OvsSession(db.endpoint, monitor_columns=MONITOR)
        now = [10_100_000_000]
        forwarding = ForwardingSource(lambda: now[0])
        source = ShapedBackhaulSource("test-egress", clock=lambda: now[0])
        try:
            await seed_radio_database(session)
            for start, drops in ((10_000_000_000, 0), (10_500_000_000, 17)):
                value = forwarding_observation(start)
                value["links"][1]["address"] = value["links_after"][1]["address"] = ADDRESS
                value["egress_observation"] = combined(start, queue=drops, rx=drops)
                operations, _ = updates(await session.snapshot(), copy.deepcopy(value))
                assert all("error" not in r for r in await session.transact(operations))
                now[0] = start + 100_000_000
                forwarding.refresh(await session.snapshot())
                sample = forwarding.sample
                refresh(source, sample.egress_observation, sample.generation)
            assert source.status()["window"]["transmit_losses"] == 17
            assert source.status()["window"]["receive_losses"] == 17
            refresh(source, sample.egress_observation, sample.generation + 1)
            assert source.status()["window"] is None
        finally:
            await session.close()
            await db.close()

    asyncio.run(run())
