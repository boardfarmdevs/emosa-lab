"""Measured successful TX and disjoint egress losses in the owned veth profile.

No PHY/capacity estimate, RX loss qualification or complete wire metric is implied.
Unsupported scheduler/XDP paths fail closed; TC changes reset the baseline.
"""

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass

from emosa.simulation.forwarding import integer, mac

PROFILE = "owned-veth-egress-observation-v1"


def observed_interface(observation, ifindex, address):
    (link,), (after,) = observation["link"], observation["link_after"]
    for row in (link, after):
        if (
            row["ifname"] != "eth1"
            or row["ifindex"] != ifindex
            or row["address"] != address
            or row["linkinfo"]["info_kind"] != "veth"
            or row.get("xdp")
            or not {"UP", "LOWER_UP"} <= set(row["flags"])
            or row["master"] != "br-lan"
            or row["linkinfo"]["info_slave_data"]["state"] != "forwarding"
        ):
            raise ValueError("unsupported_or_changed_egress_interface")
    return link


def observed_path(observation, ifindex, address):
    link = observed_interface(observation, ifindex, address)
    # Only noqueue, optionally with one software-only drop action in clsact.
    qdiscs = observation["qdiscs"]
    roots = [q for q in qdiscs if q.get("root")]
    if len(roots) != 1 or roots[0]["kind"] != "noqueue" or roots[0]["handle"] != "0:":
        raise ValueError("unsupported_root_scheduler")
    extra = [q for q in qdiscs if not q.get("root")]
    if extra and (len(extra) != 1 or extra[0]["kind"] != "clsact"):
        raise ValueError("unsupported_qdisc")
    if any(q.get("options") != {} for q in qdiscs):
        raise ValueError("unsupported_qdisc_options_or_shared_block")
    if any(
        integer(roots[0][k]) != 0 for k in ("drops", "overlimits", "requeues", "backlog", "qlen")
    ):
        raise ValueError("unexpected_noqueue_accounting")
    filters = observation["filters"]
    if set(filters) != {"root", "ingress", "egress"} or filters["root"]:
        raise ValueError("unsupported_filter_path")
    return link, bool(extra), filters


def action_drops(entries, clsact, direction):
    drops, action_identity = 0, None
    if entries:
        if not clsact or len(entries) != 2:
            raise ValueError("unsupported_egress_filter_set")
        header, rule = entries
        for entry in (header, rule):
            if (entry["protocol"], entry["pref"], entry["kind"], entry["chain"]) != (
                "ip",
                49190,
                "flower",
                0,
            ):
                raise ValueError("unsupported_egress_filter")
        config = rule["options"]
        if config["keys"] != {
            "eth_type": "ipv4",
            "ip_proto": "icmp",
            "src_ip": "192.0.2.20" if direction == "egress" else "192.0.2.1",
            "dst_ip": "192.0.2.1" if direction == "egress" else "192.0.2.20",
        }:
            raise ValueError("unsupported_drop_scope")
        if config["skip_hw"] is not True or config.get("in_hw") or config["handle"] != 1:
            raise ValueError("unsupported_hardware_or_filter_handle")
        (action,) = config["actions"]
        if (
            action["kind"] != "gact"
            or action["control_action"] != {"type": "drop"}
            or action["prob"]["random_type"] != "none"
            or action["ref"] != 1
            or action["bind"] != 1
        ):
            raise ValueError("unsupported_or_shared_action")
        stats = action["stats"]
        drops = integer(stats["drops"])
        if drops != integer(stats["packets"]) or any(
            stats[k] != 0 for k in ("overlimits", "requeues", "backlog", "qlen")
        ):
            raise ValueError("inconsistent_drop_action_counters")
        action_identity = integer(action["index"], 1)
    return drops, action_identity


def path_counters(observation, ifindex, address):
    link, clsact, filters = observed_path(observation, ifindex, address)
    if filters["ingress"]:
        raise ValueError("unsupported_ingress_filter_path")
    drops, action_identity = action_drops(filters["egress"], clsact, "egress")
    tx = link["stats64"]["tx"]
    if integer(tx["errors"]) != 0:
        raise ValueError("unexpected_veth_tx_error_semantics")
    counters = {
        "successful_packets": integer(tx["packets"]),
        "successful_bytes": integer(tx["bytes"]),
        "driver_drops": integer(tx["dropped"]),
        "action_drops": drops,
    }
    return counters, (clsact, action_identity)


@dataclass(frozen=True)
class EgressSample:
    epoch: tuple
    started_ns: int
    ended_ns: int
    counters: dict
    local_interface: str
    ifindex: int


class EgressAccountingSource:
    name = "egress"
    scope = "owned eth1 egress; driver and selected disjoint TC drops"
    read_counters = staticmethod(path_counters)

    @staticmethod
    def losses(delta):
        return {"egress_losses": delta["driver_drops"] + delta["action_drops"]}

    def __init__(self, run_label, *, clock=time.monotonic_ns):
        self.run_label, self.clock = run_label, clock
        self.sample = self.window = None
        self.reason = f"awaiting_{self.name}_observation"
        self.watermarks = {}
        self.observation_marks = {}

    def invalidate(self, reason="source_unavailable"):
        self.sample = self.window = None
        self.reason = reason

    def refresh(self, payload, *, generation, ifindex, address, boot_id, netns_inode):
        try:
            now = self.clock()
            if (
                payload is None
                or payload["profile"] != PROFILE
                or payload["run_label"] != self.run_label
                or payload["running"] is not True
                or payload["errors"]
                or payload["ifindex"] != ifindex
                or payload["mac"] != address
                or payload["interface"] != "eth1"
                or payload["boot_id"] != boot_id
                or payload["netns_inode"] != netns_inode
            ):
                raise ValueError("unbound_or_unhealthy_egress_source")
            collector = str(uuid.UUID(payload["collector_epoch"]))
            config_epoch = integer(payload["configuration_epoch"])
            heartbeat = integer(payload["heartbeat_ns"], 1)
            if not heartbeat <= now < heartbeat + 2_000_000_000:
                raise ValueError("stale_egress_observer")
            if collector not in self.watermarks and len(self.watermarks) >= 8:
                raise ValueError("collector_restart_budget")
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            previous_time, previous_epoch, previous_digest = self.watermarks.get(
                collector, (0, 0, None)
            )
            if (
                heartbeat < previous_time
                or config_epoch < previous_epoch
                or (heartbeat == previous_time and digest != previous_digest)
            ):
                raise ValueError("replayed_or_changed_egress_observation")
            self.watermarks[collector] = heartbeat, config_epoch, digest
            observation = payload["observation"]
            if observation is None:
                self.invalidate("configuration_changed_or_incomplete_dump")
                return
            start = integer(observation["started_ns"], 1)
            end = integer(observation["ended_ns"], start, start + 250_000_000)
            if not end <= heartbeat <= now < start + 2_000_000_000:
                raise ValueError("stale_or_future_egress_sample")
            raw_digest = hashlib.sha256(
                json.dumps(observation, sort_keys=True).encode()
            ).hexdigest()
            last_start, last_config, last_digest = self.observation_marks.get(
                collector, (0, 0, None)
            )
            if start < last_start or (
                start == last_start and (config_epoch != last_config or raw_digest != last_digest)
            ):
                raise ValueError("replayed_or_changed_counter_dump")
            self.observation_marks[collector] = start, config_epoch, raw_digest
            counters, path = self.read_counters(observation, integer(ifindex, 1), mac(address))
            epoch = (
                integer(generation, 1),
                collector,
                config_epoch,
                boot_id,
                netns_inode,
                ifindex,
                address,
                path,
            )
            new = EgressSample(epoch, start, end, counters, address, ifindex)
            if new == self.sample:
                return
            old, self.sample, self.window = self.sample, new, None
            self.reason = f"awaiting_{self.name}_baseline"
            if old is None or old.epoch != new.epoch:
                return
            if start <= old.ended_ns or start - old.started_ns >= 2_000_000_000:
                self.reason = f"{self.name}_sample_gap"
                return
            delta = {k: v - old.counters[k] for k, v in counters.items()}
            if any(v < 0 for v in delta.values()):
                self.reason = f"{self.name}_counter_reset"
                return
            self.window = {
                "first_read_ns": [old.started_ns, old.ended_ns],
                "last_read_ns": [start, end],
                "deltas": delta,
                **self.losses(delta),
            }
            self.reason = f"observed_{self.name}_window_available"
        except (KeyError, TypeError, ValueError, OverflowError):
            self.invalidate(f"unavailable_or_unsupported_{self.name}_source")

    def status(self):
        if self.sample and self.clock() >= self.sample.started_ns + 2_000_000_000:
            self.invalidate(f"{self.name}_observation_expired")
        return {
            "available": self.window is not None,
            "reason": self.reason,
            "sample": asdict(self.sample) if self.sample else None,
            "window": self.window,
            "scope": self.scope,
            "measurement_source_qualified": False,
            "capacity_qualified": False,
            "rx_loss_qualified": False,
        }
