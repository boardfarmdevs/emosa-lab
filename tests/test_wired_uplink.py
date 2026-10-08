# SPDX-License-Identifier: Apache-2.0
"""A wired pod's Ethernet uplink bridged into br-home by the agent (spec §8.4)."""

import asyncio
import copy
import json

import pytest

from emosa.agent.wired import DEADLINE, WiredUplink
from emosa.clock import ManualClock
from emosa.errors import EmosaError, Reason
from emosa.model import State
from emosa.opensync.schema import Schema, reference_path
from emosa.opensync.wired import MONITOR, WiredBackend, WiredIntent
from emosa.secrets import SecretStore
from emosa.store import Store

pytestmark = pytest.mark.unit
SERIAL = "MVXPOD02C09EDFC1A2"
NODE = "00000000-0000-4000-8000-000000000001"
ETH1 = "00000000-0000-4000-8000-0000000000e1"


class Pod:
    """The pod's database as the wired uplink scope sees it (pinned OpenSync schema)."""

    def __init__(self, *, if_type="eth", is_used=True, bridge=None):
        self.schema = Schema(json.loads(reference_path().read_text()))
        self.generation, self.sent = 1, []
        self.restart("00000000-0000-4000-8000-0000000000a1", if_type, is_used, bridge)

    def restart(self, start, if_type="eth", is_used=True, bridge=None):
        """OpenSync starts again: new radio rows, and cm's row without a bridge."""
        self.generation += 1
        self.tables = {
            "AWLAN_Node": {NODE: {"serial_number": SERIAL}},
            "Wifi_Radio_Config": {start: {"if_name": "phy0", "freq_band": "2.4G"}},
            "Connection_Manager_Uplink": {
                ETH1: {
                    "if_name": "eth1",
                    "if_type": if_type,
                    "is_used": is_used,
                    "bridge": bridge if bridge is not None else ["set", []],
                }
            },
        }

    async def snapshot(self):
        tables = copy.deepcopy(self.tables)
        tables["Connection_Manager_Uplink"] = {
            u: {k: v for k, v in r.items() if k in MONITOR["Connection_Manager_Uplink"]}
            for u, r in tables["Connection_Manager_Uplink"].items()
        }
        return {
            "tables": tables,
            "generation": self.generation,
            "revision": 1,
            "ready": True,
            "schema": self.schema,
        }

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(operations)
        results = []
        for op in operations:
            table = self.tables[op["table"]]
            uuid = op["where"][0][2][1]
            if op["op"] == "wait":
                row = table[uuid]
                if any(row.get(k) != v for k, v in op["rows"][0].items()):
                    return [{"error": "timed out"}]
                results.append({})
            else:
                table[uuid].update(op["row"])
                results.append({"count": 1})
        return results


def setup(tmp_path, pod, *, port="eth1"):
    intent = WiredIntent("pod-1", port, "br-home")
    backend = WiredBackend("pod-1", pod, serial=SERIAL, port=port)
    clock = ManualClock()
    vault = SecretStore(tmp_path / "secrets")
    return WiredUplink(
        "pod-1", backend, Store(tmp_path / "wired"), vault, intent, run_id="r", clock=clock
    ), clock


def test_one_guarded_write_bridges_the_uplink_port_into_br_home(tmp_path):
    pod = Pod()
    wired, _ = setup(tmp_path, pod)
    asyncio.run(wired.tick())
    (sent,) = pod.sent
    assert [op["op"] for op in sent] == ["wait", "wait", "update"]
    assert sent[0]["rows"][0] == {"serial_number": SERIAL}
    assert sent[1]["table"] == "Connection_Manager_Uplink"
    assert sent[1]["rows"][0] == {
        "if_name": "eth1",
        "if_type": "eth",
        "is_used": True,
        "bridge": ["set", []],
    }
    assert sent[2] == {
        "op": "update",
        "table": "Connection_Manager_Uplink",
        "where": [["_uuid", "==", ["uuid", ETH1]]],
        "row": {"bridge": "br-home"},
    }
    asyncio.run(wired.tick())
    assert wired.latest().state == State.OBSERVED_APPLIED and len(pod.sent) == 1
    status = wired.status()
    assert (status["mode"], status["bridge"], status["in_use"]) == ("ethernet", "br-home", "eth1")


def test_written_again_after_every_opensync_start_and_only_then(tmp_path):
    pod = Pod()
    wired, _ = setup(tmp_path, pod)
    for _ in range(3):
        asyncio.run(wired.tick())
    assert len(pod.sent) == 1
    pod.restart("00000000-0000-4000-8000-0000000000a2")  # OpenSync forgets the bridge
    asyncio.run(wired.tick())
    asyncio.run(wired.tick())
    assert len(pod.sent) == 2 and wired.latest().state == State.OBSERVED_APPLIED
    assert len(wired.store.operations()) == 2


@pytest.mark.parametrize(
    "row", [{"if_type": "vif"}, {"is_used": False}], ids=["a-wifi-uplink", "not-in-use"]
)
def test_nothing_is_written_while_the_port_is_not_the_pods_uplink_in_use(tmp_path, row):
    pod = Pod(**row)
    wired, _ = setup(tmp_path, pod)
    asyncio.run(wired.tick())
    assert not pod.sent and not wired.store.operations()
    assert "not the pod's Ethernet uplink" in wired.status()["waiting"]


def test_a_port_already_in_br_home_is_left_as_it_is(tmp_path):
    pod = Pod(bridge="br-home")
    wired, _ = setup(tmp_path, pod)
    asyncio.run(wired.tick())
    assert not pod.sent and wired.status()["waiting"] is None


def test_another_managers_bridge_is_not_taken_over(tmp_path):
    pod = Pod(bridge="br-wan")
    wired, _ = setup(tmp_path, pod)
    asyncio.run(wired.tick())
    op = wired.latest()
    assert not pod.sent and op.state == State.REJECTED
    assert op.reason == Reason.OWNERSHIP_CONFLICT
    asyncio.run(wired.tick())
    assert not pod.sent and "not bridged on this start" in wired.status()["waiting"]


def test_an_unconfirmed_write_times_out_and_waits_for_the_next_start(tmp_path):
    pod = Pod()
    wired, clock = setup(tmp_path, pod)

    async def lost(*args, **kwargs):
        raise ConnectionError("session gone")

    pod.transact = lost
    asyncio.run(wired.tick())
    assert wired.latest().state == State.INDETERMINATE
    clock.advance(DEADLINE + 1)
    asyncio.run(wired.tick())
    assert wired.latest().state == State.TIMED_OUT
    asyncio.run(wired.tick())
    assert len(wired.store.operations()) == 1  # not retried on this start


@pytest.mark.parametrize("port", ["", "eth 1", "a" * 16])
def test_an_invalid_port_is_refused(port):
    with pytest.raises(EmosaError) as caught:
        WiredIntent("pod-1", port, "br-home").target()
    assert caught.value.code == Reason.INVALID_INPUT
