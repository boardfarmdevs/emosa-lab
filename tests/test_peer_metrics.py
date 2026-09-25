import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from emosa.wire.link_metrics import LinkMetricCoordinator, decode_metrics
from emosa.wire.topology_values import Neighbor, Neighbors1905
from emosa_lab.simulation.peer_metrics import PeerMetricPublisher
from test_link_metrics import Rig

pytestmark = pytest.mark.unit


def observations(rig, start):
    local = rig.binding.local_al.hex(":")
    remote = "02:00:00:00:50:02"

    def ident(index, peer, name, address):
        return {"ifindex": index, "link_index": peer, "ifname": name, "address": address}

    pod = ident(8, 9, "eth1", local)
    controller = ident(12, 13, "eth1", remote)
    peer = ident(9, 8, "vpod", "02:00:00:00:50:03")
    proxy = ident(21, 22, "vproxy", "02:00:00:00:50:04")
    ports = []
    for row in (peer, proxy, ident(13, 12, "vcontroller", "02:00:00:00:50:05")):
        ports.append(
            row
            | {
                "master": "em-base-bh",
                "mtu": 1500,
                "flags": ["UP", "LOWER_UP"],
                "linkinfo": {
                    "info_kind": "veth",
                    "info_slave_data": {
                        "isolated": row["ifindex"] != 13,
                        "hairpin": False,
                        "state": "forwarding",
                    },
                },
            }
        )
    features = {
        k: {"active": False}
        for k in (
            "generic-segmentation-offload",
            "generic-receive-offload",
            "tx-udp-segmentation",
            "rx-vlan-offload",
            "tx-vlan-offload",
        )
    }
    desc = {
        "profile": "owned-isolated-peer-path-v1",
        "run_label": "test-path",
        "profile_epoch": "60fd4140-512a-46a1-8aa2-e9d9de072fa7",
        "pod": pod,
        "controller": controller,
        "pod_peer": peer,
        "control_peer": proxy,
    }
    before = {
        "descriptor": desc,
        "collector_epoch": "80fd4140-512a-46a1-8aa2-e9d9de072fa7",
        "configuration_epoch": 1,
        "started_ns": start,
        "ended_ns": start + 1_000_000,
        "ports": ports,
        "bridge": [
            {
                "ifname": "em-base-bh",
                "mtu": 1500,
                "addr_info": [],
                "linkinfo": {
                    "info_data": {"stp_state": 0, "mcast_querier": 0, "vlan_filtering": 1}
                },
            }
        ],
        "offloads": features,
        "vlans": {
            r["ifname"]: [
                {
                    "ifname": r["ifname"],
                    "vlans": [{"vlan": 1, "flags": ["PVID", "Egress Untagged"]}],
                }
            ]
            for r in ports
        },
    }
    after = copy.deepcopy(before)
    after.update(started_ns=start + 2_000_000, ended_ns=start + 3_000_000)
    sample = SimpleNamespace(
        generation=1,
        interfaces={"eth1": {"ifindex": 8, "mac": local, "peer_ifindex": 9}},
        peer_path_observation={"before": before, "after": after},
        egress_observation={"observation": {"features": [features | {"ifname": "eth1"}]}},
    )
    neighbor = SimpleNamespace(
        local_interface=local,
        neighbor_interface=remote,
        neighbor_al=rig.binding.controller_al.hex(":"),
        bridges_present=True,
        valid_until_ns=100_000_000_000,
    )
    accounting = {
        "sample": {"epoch": [1, "counter-lifetime"]},
        "window": {
            "first_read_ns": [start - 600_000_000, start - 590_000_000],
            "last_read_ns": [start - 100_000_000, start - 90_000_000],
            "transmit_losses": 17,
            "receive_losses": 3,
            "deltas": {"tx_packets": 200, "rx_packets": 190},
        },
        "service_estimate": {
            "mtu_payload_capacity_mbps": 100 * 1500 / 1538,
            "available_percent": 73.9,
        },
    }
    return sample, neighbor, accounting


def ready():
    rig = Rig()
    rig.topology = replace(
        rig.topology,
        neighbors1905=(
            Neighbors1905(rig.binding.local_al, (Neighbor(rig.binding.controller_al, True),)),
        ),
    )
    rig.refresh(revision=(1, 2))
    publisher = PeerMetricPublisher(rig.reports, "test-path", clock=lambda: int(rig.now * 1e9))
    publisher.refresh(*observations(rig, 9_000_000_000))
    assert publisher.source.current() is None  # Need an interval inside the observed path.
    publisher.refresh(*observations(rig, 9_900_000_000))
    assert publisher.source.current() is not None, publisher.status()
    return rig, publisher


def test_owned_path_publishes_real_coordinator_tx_rx_without_inventing_physical_phy():
    rig, publisher = ready()
    rig.coordinator = LinkMetricCoordinator(
        publisher.source, rig.sent.append, clock=lambda: rig.now
    )
    response = rig.handle()
    assert response is not None and rig.sent
    tx, rx = publisher.source.current().metrics
    assert decode_metrics(tx.tlv()).links[0].packet_errors == 17
    assert tx.links[0].mac_throughput_mbps == 97 and tx.links[0].availability_percent == 73
    assert tx.links[0].phy_rate_mbps == 65535 and rx.links[0].rssi_db == 255
    count = publisher.published
    publisher.refresh(*observations(rig, 9_900_000_000))
    assert publisher.published == count


@pytest.mark.parametrize(
    "case",
    [
        "isolated",
        "extra_peer",
        "peer_mac",
        "address",
        "vlan",
        "vm_offload",
        "pod_offload",
        "stale",
        "epoch",
        "generation",
        "replay",
        "missing",
    ],
)
def test_incomplete_changed_or_stale_paths_withdraw_wire_source(case):
    rig, publisher = ready()
    sample, neighbor, accounting = observations(rig, 9_950_000_000)
    before, after = (sample.peer_path_observation[k] for k in ("before", "after"))
    if case == "isolated":
        after["ports"][0]["linkinfo"]["info_slave_data"]["isolated"] = False
    elif case == "extra_peer":
        after["ports"].append(copy.deepcopy(after["ports"][0]))
    elif case == "peer_mac":
        neighbor.neighbor_interface = "02:00:00:00:50:ff"
    elif case == "address":
        after["bridge"][0]["addr_info"] = [{"local": "192.0.2.99"}]
    elif case == "vlan":
        after["vlans"]["vpod"][0]["vlans"][0]["vlan"] = 2
    elif case == "vm_offload":
        after["offloads"]["tx-udp-segmentation"]["active"] = True
    elif case == "pod_offload":
        sample.egress_observation["observation"]["features"][0]["tx-udp-segmentation"]["active"] = (
            True
        )
    elif case == "stale":
        rig.now += 3
    elif case == "epoch":
        before["configuration_epoch"] = after["configuration_epoch"] = 2
    elif case == "generation":
        sample.generation = 2
    elif case == "replay":
        sample, neighbor, accounting = observations(rig, 9_000_000_000)
    elif case == "missing":
        sample.peer_path_observation = None
    publisher.refresh(sample, neighbor, accounting)
    assert publisher.source.current() is None
