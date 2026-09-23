import asyncio
import copy
import json
import runpy
import struct
from pathlib import Path

import pytest
from test_forwarding import observation as forwarding_observation

from emosa.opensync.session import OvsSession
from emosa.simulation.database import SimDatabase
from emosa.simulation.egress_accounting import EgressAccountingSource
from emosa.simulation.forwarding import ForwardingSource, updates
from emosa.simulation.radio import MONITOR, seed_radio_database

DATA = Path("doc/evidence/backhaul-accounting/backhaul-loss-02/result.json")
BOOT = "30e207c8-ed45-444b-a5ac-dcc6a44ef820"
COLLECTOR = "30e207c8-ed45-444b-a5ac-dcc6a44ef821"
ADDRESS = "00:16:3e:b2:d2:c4"


def payload(start=10_000_000_000, *, packets=100, drops=0, driver=0, epoch=1):
    raw = json.loads(DATA.read_text())["phases"][1]["after"]
    row = next(v for v in raw["links"] if v["ifname"] == "eth1")
    row["stats64"]["tx"].update(packets=packets, bytes=packets * 98, dropped=driver, errors=0)
    action = raw["filters"][1]["options"]["actions"][0]
    action["stats"].update(packets=drops, drops=drops)
    return {
        "profile": "owned-veth-egress-observation-v1",
        "run_label": "test-egress",
        "running": True,
        "errors": [],
        "collector_epoch": COLLECTOR,
        "configuration_epoch": epoch,
        "heartbeat_ns": start + 20_000_000,
        "boot_id": BOOT,
        "netns_inode": 4026533000,
        "interface": "eth1",
        "ifindex": 8,
        "mac": ADDRESS,
        "observation": {
            "started_ns": start,
            "ended_ns": start + 10_000_000,
            "link": [row],
            "link_after": [copy.deepcopy(row)],
            "qdiscs": raw["qdiscs"],
            "filters": {"root": [], "ingress": [], "egress": raw["filters"]},
        },
    }


def refresh(source, value, generation=1):
    source.refresh(
        value,
        generation=generation,
        ifindex=8,
        address=ADDRESS,
        boot_id=BOOT,
        netns_inode=4026533000,
    )


@pytest.mark.unit
def test_tc_action_and_driver_drops_are_disjoint_without_parent_double_count():
    source = EgressAccountingSource("test-egress", clock=lambda: 10_600_000_000)
    refresh(source, payload())
    refresh(source, payload(10_500_000_000, packets=109, drops=17, driver=2))
    value = source.status()
    assert value["available"] and value["window"]["egress_losses"] == 19
    assert value["window"]["deltas"]["successful_packets"] == 9
    assert not value["measurement_source_qualified"] and not value["rx_loss_qualified"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    [
        "qdisc",
        "xdp",
        "ingress",
        "shared_block",
        "offload",
        "scope",
        "random",
        "shared_action",
        "count_mismatch",
        "error",
        "link_race",
        "future",
        "stale",
        "observer_error",
        "wrong_identity",
        "slow_dump",
    ],
)
def test_unqualified_or_inconsistent_path_withdraws_accounting(case):
    source = EgressAccountingSource("test-egress", clock=lambda: 10_600_000_000)
    refresh(source, payload())
    value = payload(10_500_000_000, drops=17)
    obs = value["observation"]
    config = obs["filters"]["egress"][1]["options"]
    action = config["actions"][0]
    if case == "qdisc":
        obs["qdiscs"][0]["kind"] = "netem"
    elif case == "xdp":
        obs["link"][0]["xdp"] = {"attached": "generic"}
    elif case == "ingress":
        obs["filters"]["ingress"] = [{"kind": "bpf"}]
    elif case == "shared_block":
        obs["qdiscs"][1]["options"] = {"egress_block": 50}
    elif case == "offload":
        config["skip_hw"] = False
    elif case == "scope":
        config["keys"]["dst_ip"] = "192.0.2.99"
    elif case == "random":
        action["prob"]["random_type"] = "netrand"
    elif case == "shared_action":
        action["bind"] = 2
    elif case == "count_mismatch":
        action["stats"]["packets"] = 18
    elif case == "error":
        obs["link"][0]["stats64"]["tx"]["errors"] = 1
    elif case == "link_race":
        obs["link_after"][0]["ifindex"] = 42
    elif case == "future":
        value["heartbeat_ns"] = 11_000_000_000
    elif case == "stale":
        value["heartbeat_ns"] = 8_000_000_000
    elif case == "observer_error":
        value["errors"] = ["ENOBUFS"]
    elif case == "wrong_identity":
        value["netns_inode"] += 1
    elif case == "slow_dump":
        obs["ended_ns"] += 300_000_000
    refresh(source, value)
    assert source.sample is None and source.window is None


@pytest.mark.unit
@pytest.mark.parametrize("case", ["epoch", "generation", "restart", "counter_reset", "event"])
def test_lifetime_changes_never_bridge_counter_baselines(case):
    source = EgressAccountingSource("test-egress", clock=lambda: 10_600_000_000)
    refresh(source, payload(drops=8))
    value = payload(10_500_000_000, drops=17)
    generation = 1
    if case == "epoch":
        value["configuration_epoch"] += 1
    elif case == "generation":
        generation = 2
    elif case == "restart":
        value["collector_epoch"] = "30e207c8-ed45-444b-a5ac-dcc6a44ef822"
    elif case == "counter_reset":
        value["observation"]["filters"]["egress"][1]["options"]["actions"][0]["stats"].update(
            drops=0, packets=0
        )
    elif case == "event":
        invalid = payload(10_100_000_000, epoch=2)
        invalid["observation"] = None
        refresh(source, invalid)
        value["configuration_epoch"] = 2
    refresh(source, value, generation)
    assert source.sample is not None and source.window is None


@pytest.mark.unit
def test_replayed_dump_with_new_heartbeat_is_rejected_and_watermark_survives():
    now = [10_600_000_000]
    source = EgressAccountingSource("test-egress", clock=lambda: now[0])
    refresh(source, payload())
    good = payload(10_500_000_000, drops=3)
    refresh(source, good)
    assert source.status()["available"]
    refresh(source, good)
    assert source.status()["available"]
    old = payload()
    old["heartbeat_ns"] = 10_550_000_000
    refresh(source, old)
    assert not source.status()["available"]
    refresh(source, good)
    assert source.sample is None


@pytest.mark.unit
@pytest.mark.parametrize("case", ["valid_requester_id", "overrun", "error", "truncated", "short"])
def test_route_notification_parser_detects_loss_but_allows_original_requester_id(case):
    parse = runpy.run_path("deploy/radio-manager/egress-observer.py")["configuration_events"]
    data = struct.pack("=IHHII", 16, {"overrun": 4, "error": 2}.get(case, 36), 0, 55, 1024)
    if case == "truncated":
        data += b"x"
    if case == "short":
        data = data[:8]
    if case == "valid_requester_id":
        assert parse(data) == 1
    else:
        with pytest.raises(ValueError):
            parse(data)


@pytest.mark.ovsdb
def test_loss_observations_survive_actual_ovsdb_publication_and_recovery(tmp_path):
    async def run():
        db = await SimDatabase(tmp_path / "db").start()
        session = OvsSession(db.endpoint, monitor_columns=MONITOR)
        now = [10_100_000_000]
        forwarding = ForwardingSource(lambda: now[0])
        egress = EgressAccountingSource("test-egress", clock=lambda: now[0])
        try:
            await seed_radio_database(session)
            for start, drops in ((10_000_000_000, 0), (10_500_000_000, 17)):
                value = forwarding_observation(start)
                value["links"][1]["address"] = value["links_after"][1]["address"] = ADDRESS
                value["egress_observation"] = payload(start, drops=drops)
                raw = await session.snapshot()
                ops, _ = updates(raw, value)
                result = await session.transact(ops)
                assert all("error" not in entry for entry in result)
                now[0] = start + 100_000_000
                forwarding.refresh(await session.snapshot())
                sample = forwarding.sample
                assert sample.egress_observation == value["egress_observation"]
                refresh(egress, sample.egress_observation, sample.generation)
            assert egress.status()["window"]["egress_losses"] == 17
            refresh(egress, sample.egress_observation, sample.generation + 1)
            assert egress.window is None
        finally:
            await session.close()
            await db.close()

    asyncio.run(run())
