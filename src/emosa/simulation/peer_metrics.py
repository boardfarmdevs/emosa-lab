"""Qualified *owned-lab* Ethernet service profile feeding actual IEEE 1905 reports.

This opt-in profile requires observed isolation, one discovered peer, disabled
packet aggregation/VLAN offloads and the previously calibrated service/loss
path. It makes no physical-pod, RF or physical PHY claim.
"""

import hashlib
import json
import time
import uuid

from emosa.errors import EmosaError
from emosa.simulation.forwarding import integer, mac
from emosa.wire.link_metrics import LinkBinding, LinkMetrics, LinkMetricSource, RxLink, TxLink

PROFILE = "owned-isolated-peer-path-v1"


def no_offloads(value):
    selected = {
        k: f
        for k, f in value.items()
        if any(token in k for token in ("segmentation", "receive-offload", "gso", "gro", "vlan"))
    }
    if not {
        "generic-segmentation-offload",
        "generic-receive-offload",
        "tx-udp-segmentation",
        "rx-vlan-offload",
        "tx-vlan-offload",
    } <= selected.keys() or any(f["active"] is not False for f in selected.values()):
        raise ValueError("aggregation_or_vlan_offload_unqualified")


def path_epoch(sample, neighbor, label, now):
    payload = sample.peer_path_observation
    before, after = payload["before"], payload["after"]
    epochs = []
    for value in (before, after):
        start, end = integer(value["started_ns"], 1), integer(value["ended_ns"], 1)
        if not start <= end <= now < start + 2_000_000_000 or end - start > 250_000_000:
            raise ValueError("stale_or_slow_peer_path")
        descriptor = value["descriptor"]
        if descriptor["profile"] != PROFILE or descriptor["run_label"] != label:
            raise ValueError("unbound_peer_path")
        port = sample.interfaces["eth1"]
        pod, controller = descriptor["pod"], descriptor["controller"]
        peer, control = descriptor["pod_peer"], descriptor["control_peer"]
        if (
            (pod["ifindex"], pod["address"], pod["link_index"])
            != (port["ifindex"], port["mac"], port["peer_ifindex"])
            or neighbor.local_interface != pod["address"]
            or neighbor.neighbor_interface != controller["address"]
        ):
            raise ValueError("peer_identity_differs_from_discovery_or_pod")
        if peer["ifindex"] != pod["link_index"] or peer["link_index"] != pod["ifindex"]:
            raise ValueError("not_paired_veth")
        (bridge,) = value["bridge"]
        if bridge["ifname"] != "em-base-bh" or bridge["addr_info"] or bridge["mtu"] != 1500:
            raise ValueError("unsupported_root_bridge")
        data = bridge["linkinfo"]["info_data"]
        if data["stp_state"] != 0 or data["mcast_querier"] != 0 or data["vlan_filtering"] != 1:
            raise ValueError("unsupported_bridge_service")
        ports = value["ports"]
        if len(ports) != 3 or {r["ifindex"] for r in ports} != {
            peer["ifindex"],
            control["ifindex"],
            controller["link_index"],
        }:
            raise ValueError("incomplete_peer_inventory")
        for row in ports:
            info = row["linkinfo"]
            state = info["info_slave_data"]
            isolated = row["ifindex"] != controller["link_index"]
            if (
                row["master"] != "em-base-bh"
                or info["info_kind"] != "veth"
                or row.get("xdp")
                or row["mtu"] != 1500
                or not {"UP", "LOWER_UP"} <= set(row["flags"])
                or state["isolated"] is not isolated
                or state["hairpin"] is not False
                or state["state"] != "forwarding"
            ):
                raise ValueError("unqualified_bridge_port")
            if (
                row["ifindex"] == controller["link_index"]
                and row["link_index"] != controller["ifindex"]
            ):
                raise ValueError("controller_veth_changed")
            for expected in (peer, control):
                if row["ifindex"] == expected["ifindex"] and any(
                    row[k] != v for k, v in expected.items()
                ):
                    raise ValueError("owned_bridge_port_replaced")
            if value["vlans"][row["ifname"]] != [
                {
                    "ifname": row["ifname"],
                    "vlans": [{"vlan": 1, "flags": ["PVID", "Egress Untagged"]}],
                }
            ]:
                raise ValueError("unsupported_vlan_path")
        no_offloads(value["offloads"])
        epochs.append(
            (
                str(uuid.UUID(value["collector_epoch"])),
                integer(value["configuration_epoch"]),
                str(uuid.UUID(descriptor["profile_epoch"])),
                descriptor,
            )
        )
    if epochs[0] != epochs[1] or before["ended_ns"] > after["started_ns"]:
        raise ValueError("path_changed_during_observation")
    (features,) = sample.egress_observation["observation"]["features"]
    if features["ifname"] != "eth1":
        raise ValueError("foreign_offload_observation")
    no_offloads(features)
    return hashlib.sha256(
        json.dumps((sample.generation, epochs[0]), sort_keys=True).encode()
    ).hexdigest()


class PeerMetricPublisher:
    def __init__(self, reports, label, *, clock=time.monotonic_ns):
        self.source = LinkMetricSource(reports, clock=lambda: clock() / 1e9)
        self.label, self.clock = label, clock
        self.epoch = self.since = self.last_key = None
        self.reason = "awaiting_qualified_path"
        self.published = 0
        self.path_marks = {}

    def invalidate(self, reason="source_unavailable"):
        self.source.invalidate()
        self.reason = reason
        self.epoch = self.since = self.last_key = None

    def refresh(self, sample, neighbor, accounting):
        try:
            now = self.clock()
            if sample is None or neighbor is None or now >= neighbor.valid_until_ns:
                raise ValueError("missing_current_peer")
            epoch = path_epoch(sample, neighbor, self.label, now)
            path = sample.peer_path_observation["after"]
            collector = path["collector_epoch"]
            mark = (
                path["configuration_epoch"],
                path["ended_ns"],
                hashlib.sha256(
                    json.dumps(sample.peer_path_observation, sort_keys=True).encode()
                ).hexdigest(),
            )
            old = self.path_marks.get(collector)
            if (collector not in self.path_marks and len(self.path_marks) >= 8) or (
                old
                and (mark[0] < old[0] or mark[1] < old[1] or (mark[1] == old[1] and mark != old))
            ):
                raise ValueError("replayed_or_excessive_path_lifetime")
            self.path_marks[collector] = mark
            if epoch != self.epoch:
                self.epoch, self.since = epoch, sample.peer_path_observation["after"]["ended_ns"]
                self.source.invalidate()
            window, estimate = accounting["window"], accounting["service_estimate"]
            if window is None or estimate is None or window["first_read_ns"][0] < self.since:
                self.source.invalidate()
                self.reason = "awaiting_interval_inside_qualified_path"
                return
            reports = self.source.reports.current()
            if reports is None:
                raise ValueError("missing_current_control_context")
            key = (reports.context_token, epoch, window["last_read_ns"][1])
            if key == self.last_key:
                return
            local, peer, al = (
                bytes.fromhex(mac(s).replace(":", ""))
                for s in (
                    neighbor.local_interface,
                    neighbor.neighbor_interface,
                    neighbor.neighbor_al,
                )
            )
            # The declared Ethernet media fixture is unchanged. The measured
            # software service limits MAC capacity; physical PHY is unknown.
            media = next(i.media_type for i in reports.topology.device.interfaces if i.mac == local)
            if media != 1 or neighbor.bridges_present is not True:
                raise ValueError("outside_owned_ethernet_bridge_profile")
            delta = window["deltas"]
            tx = TxLink(
                local,
                peer,
                media,
                True,
                window["transmit_losses"],
                delta["tx_packets"],
                int(estimate["mtu_payload_capacity_mbps"]),
                int(estimate["available_percent"]),
                65535,
            )
            rx = RxLink(local, peer, media, window["receive_losses"], delta["rx_packets"], 255)
            counter_epoch = hashlib.sha256(
                json.dumps((epoch, accounting["sample"]["epoch"])).encode()
            ).hexdigest()
            self.source.publish(
                context_token=reports.context_token,
                counter_epoch=counter_epoch,
                interval_started=window["first_read_ns"][0] / 1e9,
                observed_at=window["last_read_ns"][1] / 1e9,
                inventory=(LinkBinding(al, local, peer, media, True),),
                metrics=(
                    LinkMetrics(reports.topology.device.al_mac, al, (tx,)),
                    LinkMetrics(reports.topology.device.al_mac, al, (rx,)),
                ),
                inventory_complete=True,
            )
            self.last_key, self.reason = key, "owned_profile_metrics_available"
            self.published += 1
        except (KeyError, TypeError, ValueError, StopIteration, EmosaError) as error:
            self.invalidate("unavailable:" + str(error))

    def status(self):
        sample = self.source.current()
        return {
            "profile": PROFILE,
            "available": sample is not None,
            "reason": self.reason,
            "published_samples": self.published,
            "qualified_since_ns": self.since,
            "path_epoch": self.epoch,
            "sample": {
                "context_token": sample.context_token,
                "counter_epoch": sample.counter_epoch,
                "interval_started": sample.interval_started,
                "observed_at": sample.stamp.observed_at,
                "valid_until": sample.stamp.valid_until,
                "metrics": [
                    {"kind": r.tlv().kind, "value_hex": r.tlv().value.hex()} for r in sample.metrics
                ],
            }
            if sample
            else None,
            "physical_pod_proven": False,
        }
