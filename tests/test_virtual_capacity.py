import asyncio
import copy
import runpy

import pytest
from test_egress_accounting import ADDRESS, payload, refresh
from test_forwarding import observation as forwarding_observation

from emosa.opensync.session import OvsSession
from emosa.simulation.database import SimDatabase
from emosa.simulation.forwarding import ForwardingSource, updates
from emosa.simulation.radio import MONITOR, seed_radio_database
from emosa.simulation.virtual_capacity import VirtualCapacitySource


@pytest.mark.unit
def test_independent_audit_preserves_observed_two_octet_table_rounding():
    audit = runpy.run_path("scripts/check-virtual-capacity.py")
    assert [audit["charged_size"](size) for size in (42, 58, 61, 62, 905, 1514)] == [
        84,
        84,
        86,
        86,
        930,
        1538,
    ]
    window = {
        "first_read_ns": [10_000_000_000, 10_010_000_000],
        "last_read_ns": [10_500_000_000, 10_510_000_000],
        "deltas": {"service_bytes": 930, "service_packets": 1},
    }
    frames = [(10_250_000_000, 905)]
    result = audit["reconcile"](frames, [window], [0])
    assert result[0]["service_bytes"] == {"minimum": 930, "observed": 930, "maximum": 930}
    window["deltas"]["service_bytes"] = 929
    with pytest.raises(AssertionError):
        audit["reconcile"](frames, [window], [0])


def observed(start=10_000_000_000, charged=1000):
    value = payload(start)
    obs = value["observation"]
    obs["filters"] = {"root": [], "ingress": [], "egress": []}
    obs["qdiscs"] = [
        {
            "kind": "tbf",
            "handle": "4e00:",
            "root": True,
            "options": {
                "rate": 12500000,
                "burst": "64Kb/1",
                "mpu": 0,
                "lat": 36700,
                "linklayer": "ethernet",
            },
            "stab": {
                "linklayer": "ethernet",
                "overhead": 24,
                "mpu": 84,
                "mtu": 2048,
                "tsize": 2048,
            },
            "bytes": charged,
            "packets": 2000,
            "drops": 0,
            "overlimits": 0,
            "requeues": 0,
            "backlog": 0,
            "qlen": 0,
        }
    ]
    return value


@pytest.mark.unit
def test_service_work_and_bounded_availability_do_not_claim_neighbor_metrics():
    now = [10_600_000_000]
    source = VirtualCapacitySource("test-egress", clock=lambda: now[0])
    refresh(source, observed())
    assert source.status()["service_estimate"] is None
    refresh(source, observed(10_500_000_000, 3_126_000))
    value = source.status()
    estimate = value["service_estimate"]
    assert estimate["service_work_ns"] == 250_000_000
    assert estimate["available_percent"] == 50
    assert estimate["available_percent_bounds"][0] < 50 < estimate["available_percent_bounds"][1]
    assert estimate["mtu_payload_capacity_mbps"] == pytest.approx(97.5292587776)
    assert not value["measurement_source_qualified"] and not value["capacity_qualified"]
    assert not value["native_neighbor_metric_delivery_proven"]
    now[0] = 12_500_000_000
    assert source.status()["service_estimate"] is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    [
        "rate",
        "burst",
        "framing",
        "mpu",
        "mtu",
        "filter",
        "child",
        "xdp",
        "requeue",
        "counter_reset",
        "epoch",
        "generation",
        "too_much_work",
        "configuration_gap",
        "missing_framing_dump",
    ],
)
def test_invalid_service_or_lifetime_withdraws_estimate(case):
    source = VirtualCapacitySource("test-egress", clock=lambda: 10_600_000_000)
    refresh(source, observed())
    value = observed(10_500_000_000, 3_126_000)
    obs, generation = value["observation"], 1
    q = obs["qdiscs"][0]
    if case == "rate":
        q["options"]["rate"] *= 10
    elif case == "burst":
        q["options"]["burst"] = "128Kb/1"
    elif case == "framing":
        q["stab"]["overhead"] = 0
    elif case == "mpu":
        q["stab"]["mpu"] = 0
    elif case == "mtu":
        obs["link_after"][0]["mtu"] = 9000
    elif case == "filter":
        obs["filters"]["egress"] = [{"kind": "bpf"}]
    elif case == "child":
        obs["qdiscs"].append(copy.deepcopy(q))
    elif case == "xdp":
        obs["link"][0]["xdp"] = {"attached": "generic"}
    elif case == "requeue":
        q["requeues"] = 1
    elif case == "counter_reset":
        q["bytes"] = 0
    elif case == "epoch":
        value["configuration_epoch"] += 1
    elif case == "generation":
        generation += 1
    elif case == "too_much_work":
        q["bytes"] = 100_000_000
    elif case == "configuration_gap":
        value["observation"] = None
    elif case == "missing_framing_dump":
        q.pop("stab")
    refresh(source, value, generation)
    assert source.status()["service_estimate"] is None


@pytest.mark.unit
def test_token_waits_are_not_drops_and_burst_credit_is_bounded():
    source = VirtualCapacitySource("test-egress", clock=lambda: 10_600_000_000)
    refresh(source, observed())
    value = observed(10_500_000_000, 6_261_000)
    value["observation"]["qdiscs"][0]["overlimits"] = 123
    refresh(source, value)
    status = source.status()
    assert status["window"]["deltas"]["queue_drops"] == 0
    assert status["service_estimate"]["available_percent"] == 0


@pytest.mark.ovsdb
def test_virtual_service_observation_crosses_ovsdb_and_restarts_baseline(tmp_path):
    async def run():
        db = await SimDatabase(tmp_path / "db").start()
        session = OvsSession(db.endpoint, monitor_columns=MONITOR)
        now = [10_100_000_000]
        forwarding = ForwardingSource(lambda: now[0])
        source = VirtualCapacitySource("test-egress", clock=lambda: now[0])
        try:
            await seed_radio_database(session)
            for start, charged in ((10_000_000_000, 1000), (10_500_000_000, 3_126_000)):
                value = forwarding_observation(start)
                value["links"][1]["address"] = value["links_after"][1]["address"] = ADDRESS
                value["egress_observation"] = observed(start, charged)
                ops, _ = updates(await session.snapshot(), value)
                assert all("error" not in v for v in await session.transact(ops))
                now[0] = start + 100_000_000
                forwarding.refresh(await session.snapshot())
                sample = forwarding.sample
                refresh(source, sample.egress_observation, sample.generation)
            assert source.status()["service_estimate"]["available_percent"] == 50
            refresh(source, sample.egress_observation, sample.generation + 1)
            assert source.status()["service_estimate"] is None
        finally:
            await session.close()
            await db.close()

    asyncio.run(run())
