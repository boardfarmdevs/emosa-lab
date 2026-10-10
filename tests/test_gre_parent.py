# SPDX-License-Identifier: Apache-2.0
"""Spec 8.6: a GRE parent's tunnels to its children (emosa.opensync.gre_parent), offline."""

import asyncio
import copy
import json

import pytest

from emosa.errors import EmosaError, Reason
from emosa.opensync.gre_parent import GreParentBackend, TunnelIntent, tunnel_name
from emosa.opensync.schema import Schema, reference_path

pytestmark = pytest.mark.unit


def uid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


NODE, RADIO, AP_STATE, AP_INET, BRIDGE, HOME_PORT = (uid(n) for n in range(1, 7))
SERIAL = "MVXPOD0000000002"
CHILD = "02:00:00:00:2a:01"


def tables(*, leases=((CHILD, "169.254.2.10"),), associated=(CHILD,), tunnels=(), address=True):
    clients = {uid(100 + i): {"mac": m, "state": "active"} for i, m in enumerate(associated)}
    raw = {
        "AWLAN_Node": {NODE: {"serial_number": SERIAL}},
        "Wifi_Radio_Config": {RADIO: {"if_name": "wlan0"}},
        "Wifi_VIF_State": {
            AP_STATE: {
                "if_name": "b-ap-24",
                "mode": "ap",
                "associated_clients": ["set", [["uuid", u] for u in clients]],
            }
        },
        "Wifi_Associated_Clients": clients,
        "DHCP_leased_IP": {
            uid(200 + i): {"hwaddr": m, "inet_addr": a} for i, (m, a) in enumerate(leases)
        },
        "Wifi_Inet_State": (
            {AP_INET: {"if_name": "b-ap-24", "inet_addr": "169.254.2.1"}} if address else {}
        ),
        "Wifi_Inet_Config": {},
        "Bridge": {BRIDGE: {"name": "br-home", "ports": ["set", [["uuid", HOME_PORT]]]}},
        "Port": {HOME_PORT: {"name": "br-home", "interfaces": ["uuid", uid(7)]}},
        "Interface": {uid(7): {"name": "br-home"}},
    }
    for i, (name, remote) in enumerate(tunnels):
        port = uid(300 + i)
        raw["Wifi_Inet_Config"][uid(400 + i)] = {
            "if_name": name,
            "if_type": "gre",
            "gre_ifname": "b-ap-24",
            "gre_local_inet_addr": "169.254.2.1",
            "gre_remote_inet_addr": remote,
            "enabled": True,
            "network": True,
            "mtu": 1562,
            "ip_assign_scheme": "none",
        }
        raw["Port"][port] = {"name": name, "interfaces": ["uuid", uid(600 + i)]}
        raw["Interface"][uid(600 + i)] = {"name": name}
        raw["Bridge"][BRIDGE]["ports"][1].append(["uuid", port])
        raw["Wifi_Inet_State"][uid(500 + i)] = {"if_name": name, "inet_addr": "0.0.0.0"}
    return raw


class Session:
    def __init__(self, raw_tables):
        self.schema = Schema(json.loads(reference_path().read_text()))
        self.tables, self.sent = raw_tables, []

    async def snapshot(self):
        return {
            "tables": copy.deepcopy(self.tables),
            "generation": 1,
            "revision": 7,
            "ready": True,
            "schema": self.schema,
        }

    async def transact(self, operations, transaction_id=None, generation=None, **_):
        self.sent.append(operations)
        replies = {"wait": {}, "insert": {"uuid": ["uuid", uid(999)]}}
        return [replies.get(op["op"], {"count": 1}) for op in operations]


def backend(raw):
    return GreParentBackend(
        "pod-2",
        Session(raw),
        serial=SERIAL,
        ap="b-ap-24",
        bridge="br-home",
        underlay="169.254.2.0/24",
    )


def intent(*remotes):
    return TunnelIntent("pod-2", "b-ap-24", "br-home", tuple(remotes))


def submit(pod, wanted):
    result = asyncio.run(pod.submit(wanted, {"transaction_id": "t", "session_generation": 1}))
    assert result.status == "committed", result
    return pod.session.sent[-1]


def test_the_tunnel_is_named_by_the_childs_last_two_octets():
    assert tunnel_name("169.254.2.10") == "pgd2_10"
    assert intent("169.254.2.11", "169.254.2.10").target()["tunnels"] == {
        "pgd2_10": "169.254.2.10",
        "pgd2_11": "169.254.2.11",
    }


def test_a_child_is_an_associated_lease_in_the_underlay():
    stale = ("02:00:00:00:2a:02", "169.254.2.11")  # a lease left from an earlier association
    outside = ("02:00:00:00:2a:03", "192.168.1.20")
    pod = backend(
        tables(
            leases=((CHILD, "169.254.2.10"), stale, outside),
            associated=(CHILD, outside[0]),
        )
    )
    asyncio.run(pod.snapshot())
    assert pod.children == ("169.254.2.10",)


def test_a_new_child_gets_its_gre_row_and_a_port_in_br_home():
    pod = backend(tables())
    asyncio.run(pod.snapshot())
    ops = submit(pod, intent(*pod.children))
    inet = next(op for op in ops if op["op"] == "insert" and op["table"] == "Wifi_Inet_Config")
    assert inet["row"] == {
        "if_name": "pgd2_10",
        "if_type": "gre",
        "enabled": True,
        "network": True,
        "mtu": 1562,
        "ip_assign_scheme": "none",
        "gre_ifname": "b-ap-24",
        "gre_local_inet_addr": "169.254.2.1",
        "gre_remote_inet_addr": "169.254.2.10",
    }
    port = next(op for op in ops if op["op"] == "insert" and op["table"] == "Port")
    assert port["row"]["name"] == "pgd2_10"
    assert {
        "op": "mutate",
        "table": "Bridge",
        "where": [["_uuid", "==", ["uuid", BRIDGE]]],
        "mutations": [["ports", "insert", ["set", [["named-uuid", port["uuid-name"]]]]]],
    } in ops
    # guarded: the pod, the bridge's ports, no such row or port yet
    assert [op["table"] for op in ops if op["op"] == "wait"] == [
        "AWLAN_Node",
        "Bridge",
        "Wifi_Inet_Config",
        "Port",
    ]


def test_a_tunnel_whose_child_is_gone_is_removed_with_its_port():
    pod = backend(tables(leases=(), associated=(), tunnels=(("pgd2_10", "169.254.2.10"),)))
    snap = asyncio.run(pod.snapshot())
    assert snap.config["tunnels"] == {"pgd2_10": "169.254.2.10"} and pod.children == ()
    ops = submit(pod, intent())
    assert {
        "op": "delete",
        "table": "Wifi_Inet_Config",
        "where": [["_uuid", "==", ["uuid", uid(400)]]],
    } in ops
    assert {
        "op": "mutate",
        "table": "Bridge",
        "where": [["_uuid", "==", ["uuid", BRIDGE]]],
        "mutations": [["ports", "delete", ["set", [["uuid", uid(300)]]]]],
    } in ops
    assert not [op for op in ops if op["op"] == "insert"]


def test_tunnels_in_place_are_configured_and_observed_once_up():
    pod = backend(tables(tunnels=(("pgd2_10", "169.254.2.10"),)))
    snap = asyncio.run(pod.snapshot())
    target = intent("169.254.2.10").target()
    assert snap.config == target and snap.observed.satisfies(target)


@pytest.mark.parametrize(
    "remotes,code",
    [
        (("10.0.0.5",), Reason.INVALID_INPUT),  # not the underlay's
        (("not an address",), Reason.INVALID_INPUT),
    ],
)
def test_a_child_outside_the_underlay_is_refused(remotes, code):
    pod = backend(tables())
    with pytest.raises(EmosaError) as error:
        asyncio.run(pod.plan(intent(*remotes)))
    assert error.value.code == code


def test_no_tunnel_before_the_parent_ap_has_its_address():
    pod = backend(tables(address=False))
    with pytest.raises(EmosaError) as error:
        asyncio.run(pod.plan(intent("169.254.2.10")))
    assert error.value.code == Reason.NOT_READY
