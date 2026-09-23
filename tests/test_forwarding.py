import asyncio
import copy
import json
from pathlib import Path

import pytest

from emosa.opensync.schema import Schema, reference_path
from emosa.opensync.session import OvsSession
from emosa.simulation.database import SimDatabase
from emosa.simulation.forwarding import (
    COUNTERS,
    NAMES,
    ForwardingSource,
    normalize,
    seed_entries,
    updates,
)
from emosa.simulation.radio import MONITOR, seed_radio_database


def observation(start=10_000_000_000, count=100):
    rows = [
        {
            "ifname": "br-lan",
            "ifindex": 2,
            "address": "02:00:00:00:00:02",
            "flags": ["UP"],
            "linkinfo": {"info_kind": "bridge"},
        }
    ]
    for index, name in enumerate(NAMES):
        rows.append(
            {
                "ifname": name,
                "ifindex": 8 + index,
                "address": f"00:16:3e:00:00:0{index + 1}",
                "flags": ["UP", "LOWER_UP"],
                "link_type": "ether",
                "master": "br-lan",
                "mtu": 1500,
                "link_index": 19 + index if name != "wlan0" else 0,
                "linkinfo": {
                    "info_kind": "veth",
                    "info_slave_kind": "bridge",
                    "info_slave_data": {"state": "forwarding"},
                },
                "stats64": {
                    d: {"packets": count, "bytes": count * 120, "errors": 3, "dropped": 2}
                    for d in ("rx", "tx")
                },
            }
        )
    return {
        "complete": True,
        "started_ns": start,
        "ended_ns": start + 10_000_000,
        "boot_id": "30e207c8-ed45-444b-a5ac-dcc6a44ef820",
        "netns_inode": 4026533000,
        "links": rows,
        "links_after": copy.deepcopy(rows),
    }


def snapshot(observed=None, generation=1):
    observed = observed or observation()
    values, metadata = normalize(observed)
    entries = seed_entries()
    ids = {
        name: f"00000000-0000-0000-0000-{index:012x}"
        for index, (_, name, _) in enumerate(entries, start=1)
    }

    def resolve(value):
        if isinstance(value, (list, tuple)):
            if len(value) == 2 and value[0] == "named-uuid":
                return ["uuid", ids[value[1]]]
            return [resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        return value

    tables = {}
    for table, name, row in entries:
        if table == "Interface":
            row |= values[row["name"]]
        elif table == "Bridge":
            row["external_ids"] = metadata
        tables.setdefault(table, {})[ids[name]] = resolve(row)
    return {
        "ready": True,
        "generation": generation,
        "tables": tables,
        "schema": Schema(json.loads(reference_path().read_text())),
    }


@pytest.mark.unit
def test_observation_preserves_driver_counts_and_distinguishes_interface_roles():
    rows, metadata = normalize(observation())
    assert rows["eth1"]["mac_in_use"] == "00:16:3e:00:00:01"
    assert rows["eth1"]["mac_in_use"] != "02:00:00:00:30:01"  # Adapter AL/control veth.
    assert dict(rows["eth1"]["statistics"][1]) == {
        "rx_packets": 100,
        "rx_bytes": 12000,
        "rx_errors": 3,
        "rx_dropped": 2,
        "tx_packets": 100,
        "tx_bytes": 12000,
        "tx_errors": 3,
        "tx_dropped": 2,
    }
    assert dict(metadata[1])["bridge_ifindex"] == "2"


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    [
        "extra_port",
        "missing_port",
        "race",
        "blocked",
        "down",
        "multicast_mac",
        "duplicate_mac",
        "negative",
        "overflow",
        "boolean",
        "partial",
        "slow",
    ],
)
def test_incomplete_or_raced_rtnetlink_observation_is_not_published(case):
    value = observation()
    row = value["links"][1]
    if case == "extra_port":
        value["links"].append({"ifname": "eth9", "master": "br-lan"})
    elif case == "missing_port":
        row["master"] = "other"
    elif case == "race":
        value["links_after"][1]["ifindex"] += 90
    elif case == "blocked":
        row["linkinfo"]["info_slave_data"]["state"] = "blocking"
    elif case == "down":
        row["flags"] = ["UP"]
    elif case == "multicast_mac":
        row["address"] = "01:00:5e:00:00:01"
    elif case == "duplicate_mac":
        row["address"] = value["links"][2]["address"]
    elif case in ("negative", "overflow", "boolean"):
        row["stats64"]["rx"]["packets"] = {"negative": -1, "overflow": 2**63, "boolean": True}[case]
    elif case == "partial":
        del row["stats64"]["rx"]["errors"]
    else:
        value["ended_ns"] += 2_000_000_000
    with pytest.raises((ValueError, KeyError)):
        normalize(value)
    ops, event = updates(snapshot(), value)
    assert not event["available"]
    assert all(
        op["row"]["statistics"] == ["map", []]
        for op in ops
        if op["op"] == "update" and op["table"] == "Interface"
    )


@pytest.mark.unit
def test_fresh_duplicate_reads_do_not_extend_lifetime_or_reset_a_valid_window():
    now = [10_100_000_000]
    source = ForwardingSource(lambda: now[0])
    first = snapshot()
    source.refresh(first)
    assert source.status()["available"] and source.window is None
    second = snapshot(observation(10_500_000_000, 107))
    now[0] += 500_000_000
    source.refresh(second)
    assert source.window["deltas"]["eth1"]["rx_packets"] == 7
    assert source.window["deltas"]["eth1"]["tx_bytes"] == 840
    window = copy.deepcopy(source.window)
    source.refresh(second)
    assert source.window == window
    now[0] = 12_500_000_000
    assert not source.status()["available"]
    assert source.window is None
    # No resurrection after clock rollback or an explicit invalidation.
    now[0] = 10_600_000_000
    source.refresh(second)
    assert not source.status()["available"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    [
        "stale",
        "future",
        "inconsistent_time",
        "missing_counter",
        "negative_counter",
        "different_profile",
        "disconnected",
        "bridge_graph",
        "port_graph",
        "row_injection",
        "schema_change",
        "mutated_timestamp",
    ],
)
def test_bad_ovsdb_observation_withdraws_window(case):
    now = [10_100_000_000]
    source = ForwardingSource(lambda: now[0])
    source.refresh(snapshot())
    value = snapshot(observation(10_500_000_000, 105))
    now[0] = 10_600_000_000
    row = next(iter(value["tables"]["Interface"].values()))
    if case == "stale":
        now[0] += 3_000_000_000
    elif case == "future":
        now[0] -= 200_000_000
    elif case in ("inconsistent_time", "different_profile"):
        key = "started_ns" if case == "inconsistent_time" else "emosa_profile"
        row["external_ids"] = [
            "map",
            [[k, "1" if k == key else v] for k, v in row["external_ids"][1]],
        ]
    elif case == "missing_counter":
        row["statistics"][1].pop()
    elif case == "negative_counter":
        row["statistics"][1][0] = ["rx_bytes", -1]
    elif case == "disconnected":
        value["ready"] = False
    elif case == "bridge_graph":
        next(iter(value["tables"]["Bridge"].values()))["ports"][1].pop()
    elif case == "port_graph":
        ports = list(value["tables"]["Port"].values())
        ports[0]["interfaces"] = ports[1]["interfaces"]
    elif case == "row_injection":
        value["tables"]["Interface"]["00000000-0000-0000-0000-000000000abc"] = copy.deepcopy(row)
    elif case == "schema_change":
        raw = copy.deepcopy(value["schema"].raw)
        raw["tables"]["Interface"]["columns"]["statistics"]["type"]["value"] = "string"
        value["schema"] = Schema(raw)
    else:
        value = snapshot(observation(count=101))
    source.refresh(value)
    assert not source.status()["available"] and source.window is None


@pytest.mark.unit
@pytest.mark.parametrize("case", ["reset", "generation", "interface", "gap"])
def test_reset_identity_reconnect_and_gap_require_a_new_baseline(case):
    now = [10_100_000_000]
    source = ForwardingSource(lambda: now[0])
    source.refresh(snapshot())
    when = 13_000_000_000 if case == "gap" else 10_500_000_000
    value = observation(when, 7 if case == "reset" else 110)
    if case == "interface":
        for key in ("links", "links_after"):
            value[key][1]["ifindex"] = 101
    generation = 2 if case == "generation" else 1
    now[0] = when + 100_000_000
    source.refresh(snapshot(value, generation))
    assert source.status()["available"] and source.window is None
    value["started_ns"] += 500_000_000
    value["ended_ns"] += 500_000_000
    for row in value["links"][1:]:
        for direction in ("rx", "tx"):
            row["stats64"][direction]["packets"] += 5
    now[0] += 500_000_000
    source.refresh(snapshot(value, generation))
    assert source.window["deltas"]["eth1"]["tx_packets"] == 5


@pytest.mark.ovsdb
def test_forwarding_publication_is_atomic_and_uses_real_pinned_schema(tmp_path):
    async def scenario():
        db = await SimDatabase(tmp_path / "db").start()
        session = OvsSession(db.endpoint, monitor_columns=MONITOR)
        source = ForwardingSource(lambda: 10_100_000_000)
        try:
            await seed_radio_database(session)
            raw = await session.snapshot()
            source.refresh(raw)
            assert not source.status()["available"]  # Seed is not an observation.
            ids = {t: sorted(v) for t, v in raw["tables"].items()}
            ops, event = updates(raw, observation())
            assert event["available"]
            result = await session.transact(ops, generation=raw["generation"])
            assert all("error" not in r for r in result)
            current = await session.snapshot()
            assert {t: sorted(v) for t, v in current["tables"].items()} == ids
            source.refresh(current)
            assert source.status()["sample"]["interfaces"]["eth1"]["counters"]["rx_packets"] == 100
            # A competing writer invalidates the whole guarded publication.
            ops, _ = updates(current, observation(10_500_000_000, 110))
            interface_id = next(iter(current["tables"]["Interface"]))
            await session.transact(
                [
                    {
                        "op": "update",
                        "table": "Interface",
                        "where": [["_uuid", "==", ["uuid", interface_id]]],
                        "row": {"mtu": 1400},
                    }
                ]
            )
            result = await session.transact(ops, generation=raw["generation"])
            assert any("error" in r for r in result if isinstance(r, dict))
            latest = await session.snapshot()
            for row in latest["tables"]["Interface"].values():
                assert dict(row["statistics"][1])["rx_packets"] == 100
            # Withdraw stale positive fields, preserving graph UUID identity.
            ops, event = updates(latest, {"complete": False})
            assert not event["available"]
            await session.transact(ops)
            source.refresh(await session.snapshot())
            assert not source.status()["available"]
        finally:
            await session.close()
            await db.close()

    asyncio.run(scenario())


@pytest.mark.unit
def test_privileged_observer_has_no_configuration_command(monkeypatch):
    import runpy

    module = runpy.run_path("deploy/radio-manager/node.py")
    calls = []
    raw = observation()

    def command(*args):
        calls.append(args)
        return json.dumps(raw["links"])

    module["forwarding"].__globals__["command"] = command
    actual = module["forwarding"]()
    assert actual["complete"]
    assert calls == [("ip", "-j", "-d", "-s", "link", "show"), ("ip", "-j", "-d", "link", "show")]
    assert actual["netns_inode"] == Path("/proc/self/ns/net").stat().st_ino
    assert set(COUNTERS) == {
        d + "_" + k for d in ("rx", "tx") for k in ("packets", "bytes", "errors", "dropped")
    }
