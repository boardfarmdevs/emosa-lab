# SPDX-License-Identifier: Apache-2.0
"""Pods as parents of other pods (spec 8.5): the peer directory, the backhaul's 1905 neighbors,
the no-loop rule and the backhaul link metrics."""

import json
import struct

import pytest

from emosa.agent.peers import PeerDirectory, children, loops, mac_bytes, upstream
from emosa.opensync.easymesh_view import neighbors_1905
from emosa.wire.cmdu import Message, Reassembler, Tlv
from emosa.wire.link_metrics import BackhaulLinkMetrics, BackhaulPair

pytestmark = pytest.mark.unit

CONTROLLER = mac_bytes("02:00:00:00:00:02")
PARENT_AL, CHILD_AL, OWN_AL = (mac_bytes(f"02:72:00:00:00:0{n}") for n in (1, 2, 3))
PARENT_BH = mac_bytes("72:00:00:00:6a:00")  # the parent pod's backhaul BSS
CHILD_STA = mac_bytes("02:00:00:00:6b:00")  # the child pod's backhaul station


def status(al, *, backhaul_bss=None, station=None, parent=None, pid=111, stations=None):
    bsses = [{"role": "fronthaul", "bssid": "82:00:00:00:6a:00", "ssid": "home"}]
    if backhaul_bss:
        bsses.append({"role": "backhaul", "bssid": backhaul_bss, "ssid": "mesh_backhaul"})
    return {
        "agent_al": al,
        "worker_pid": pid,
        "pod": {
            "bsses": bsses,
            "backhaul": None
            if station is None
            else {"station": "bhaul-sta-24", "mac": station, "parent": parent, "band": "2.4G"},
        },
        "telemetry": {"stations": stations or {}},
    }


def write(root, pod, value):
    (root / pod).mkdir(parents=True)
    (root / pod / "status.json").write_text(json.dumps(value))


def test_the_directory_reads_the_fleets_other_live_agents(tmp_path):
    write(tmp_path, "OWN", status("02:72:00:00:00:03"))
    write(tmp_path, "PARENT", status("02:72:00:00:00:01", backhaul_bss="72:00:00:00:6a:00"))
    write(
        tmp_path,
        "CHILD",
        status("02:72:00:00:00:02", station="02:00:00:00:6b:00", parent="72:00:00:00:6a:00"),
    )
    write(tmp_path, "GONE", status("02:72:00:00:00:09", pid=999))
    (tmp_path / "BROKEN").mkdir()
    (tmp_path / "BROKEN" / "status.json").write_text("{")
    directory = PeerDirectory(tmp_path / "OWN", "OWN", alive=lambda pid: pid != 999)
    peers = directory.peers()
    assert sorted(p.pod_id for p in peers) == ["CHILD", "PARENT"]
    parent = upstream(peers, PARENT_BH)
    assert parent.al == PARENT_AL and parent.backhaul_bsses == {PARENT_BH}
    child = next(p for p in peers if p.pod_id == "CHILD")
    assert (child.station, child.parent) == (CHILD_STA, PARENT_BH)
    assert children(peers, {PARENT_BH: {CHILD_STA}}) == ((PARENT_BH, child),)
    assert children(peers, {PARENT_BH: set()}) == ()  # not associated: no neighbor


def test_an_upstream_whose_chain_reaches_this_pod_is_a_loop(tmp_path):
    a_bh, b_bh = mac_bytes("72:00:00:00:0a:00"), mac_bytes("72:00:00:00:0b:00")
    own_bh = mac_bytes("72:00:00:00:0c:00")
    write(tmp_path, "OWN", status("02:72:00:00:00:03"))
    # A hangs off this pod, B off A: B's chain reaches this pod
    write(
        tmp_path,
        "A",
        status(
            "02:72:00:00:00:0a",
            backhaul_bss=a_bh.hex(":"),
            station="02:00:00:00:0a:01",
            parent=own_bh.hex(":"),
        ),
    )
    write(
        tmp_path,
        "B",
        status(
            "02:72:00:00:00:0b",
            backhaul_bss=b_bh.hex(":"),
            station="02:00:00:00:0b:01",
            parent=a_bh.hex(":"),
        ),
    )
    peers = PeerDirectory(tmp_path / "OWN", "OWN", alive=lambda pid: True).peers()
    assert loops(peers, OWN_AL, {own_bh}, b_bh)
    assert loops(peers, OWN_AL, {own_bh}, own_bh)
    assert not loops(peers, OWN_AL, {own_bh}, mac_bytes("02:00:00:4d:06:73"))  # an RDK node


def test_backhaul_neighbors_join_the_controller_on_ethernet():
    entries = neighbors_1905(OWN_AL, CONTROLLER, ((CHILD_STA, PARENT_AL),))
    assert [(e.local_interface, [n.al_mac for n in e.neighbors]) for e in entries] == [
        (OWN_AL, [CONTROLLER]),
        (CHILD_STA, [PARENT_AL]),
    ]
    assert not any(n.bridges_present for e in entries for n in e.neighbors)


def query(neighbor=None, direction=2, mid=7):
    value = (b"\0" if neighbor is None else b"\1" + neighbor) + bytes((direction,))
    return Message(OWN_AL, CONTROLLER, 5, mid, False, (Tlv(8, value),), 1)


def pair(tx=(3, 1000, 65), rx=(1, 900, 38)):
    return BackhaulPair(PARENT_AL, CHILD_STA, PARENT_BH, 0x0103, tx, rx)


def answer(pairs, message):
    sent = []
    result = BackhaulLinkMetrics(OWN_AL, CONTROLLER, lambda: pairs, sent.append).handle(message)
    return result, [Reassembler().feed(frame) for frame in sent]


def test_a_backhaul_link_metric_query_is_answered_from_the_measurement():
    result, messages = answer((pair(),), query())
    assert result == "backhaul_link_metric_response_sent"
    (reply,) = messages
    assert (reply.message_type, reply.mid, reply.destination) == (6, 7, CONTROLLER)
    tx, rx = reply.tlvs
    assert (tx.kind, rx.kind) == (9, 10)
    assert tx.value[:12] == OWN_AL + PARENT_AL and rx.value[:12] == OWN_AL + PARENT_AL
    local, neighbor, media, bridge, errors, packets, throughput, availability, phy = struct.unpack(
        "!6s6sHBIIHHH", tx.value[12:]
    )
    assert (local, neighbor, media, bridge) == (CHILD_STA, PARENT_BH, 0x0103, 0)
    assert (errors, packets, throughput, availability, phy) == (3, 1000, 65, 100, 65535)
    local, neighbor, media, errors, packets, rssi = struct.unpack("!6s6sHIIB", rx.value[12:])
    assert (errors, packets, rssi) == (1, 900, 38)


@pytest.mark.parametrize(
    ("message", "pairs", "kinds"),
    [
        (query(direction=0), (pair(),), [9]),
        (query(direction=1), (pair(),), [10]),
        (query(neighbor=PARENT_AL), (pair(rx=None),), [9]),  # one direction measured
        (query(neighbor=CONTROLLER), (pair(),), None),  # another neighbor: not this responder's
        (query(), (pair(tx=None, rx=None),), None),  # nothing measured: nothing sent
        (query(), (), None),
    ],
)
def test_what_a_backhaul_link_metric_answer_holds(message, pairs, kinds):
    result, messages = answer(pairs, message)
    if kinds is None:
        assert result is None and messages == []
    else:
        assert [t.kind for t in messages[0].tlvs] == kinds
