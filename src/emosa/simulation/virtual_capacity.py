"""Service-work estimate for the explicitly shaped owned Ethernet simulation.

This is not a PHY measurement or a complete IEEE 1905 per-neighbor metric.
The retained calibration checks non-GSO Ethernet traffic against independent
packets. A native publisher still needs complete path and peer qualification.
"""

from emosa.simulation.egress_accounting import EgressAccountingSource, observed_interface
from emosa.simulation.forwarding import integer

RATE_BYTES = 12_500_000
BURST_BYTES = 65536
OPTIONS = {"rate": RATE_BYTES, "burst": "64Kb/1", "mpu": 0, "lat": 36700, "linklayer": "ethernet"}
STAB = {"linklayer": "ethernet", "overhead": 24, "mpu": 84, "mtu": 2048, "tsize": 2048}


def service_counters(qdisc):
    if (qdisc["kind"], qdisc["handle"], qdisc["root"]) != ("tbf", "4e00:", True):
        raise ValueError("unsupported_service")
    if qdisc["options"] != OPTIONS or qdisc["stab"] != STAB:
        raise ValueError("changed_service_contract")
    if integer(qdisc["requeues"]) != 0:
        raise ValueError("unqualified_requeue_accounting")
    for name in ("backlog", "qlen"):
        integer(qdisc[name])
    return {
        "service_bytes": integer(qdisc["bytes"]),
        "service_packets": integer(qdisc["packets"]),
        "queue_drops": integer(qdisc["drops"]),
        "token_waits": integer(qdisc["overlimits"]),
    }


def counters(observation, ifindex, address):
    observed_interface(observation, ifindex, address)
    if any(row["mtu"] != 1500 for key in ("link", "link_after") for row in observation[key]):
        raise ValueError("unsupported_mtu")
    if observation["filters"] != {"root": [], "ingress": [], "egress": []}:
        raise ValueError("unsupported_filter_path")
    (qdisc,) = observation["qdiscs"]
    return service_counters(qdisc), ("owned-tbf-100m-stab24-min84-v1",)


class VirtualCapacitySource(EgressAccountingSource):
    name = "virtual_capacity"
    scope = "owned 100-Mbit egress service-work estimate; not physical PHY or neighbor metrics"
    read_counters = staticmethod(counters)
    losses = staticmethod(lambda _delta: {})

    def status(self):
        status = super().status()
        estimate = None
        if self.window:
            a, b = self.window["first_read_ns"]
            c, d = self.window["last_read_ns"]
            minimum, maximum = c - b, d - a
            charged = self.window["deltas"]["service_bytes"]
            # Token credit permits an initial burst; it does not change the
            # configured sustained rate. Never hide traffic beyond that budget.
            if charged * 1_000_000_000 > RATE_BYTES * maximum + BURST_BYTES * 1_000_000_000:
                self.invalidate("service_work_exceeds_rate_and_burst")
                return self.status()
            work_ns = charged * 1_000_000_000 / RATE_BYTES
            estimate = {
                "profile": "owned-tbf-100m-stab24-min84-v1",
                "nominal_service_mbps": 100,
                "mtu_payload_capacity_mbps": 100 * 1500 / 1538,
                "service_work_ns": work_ns,
                "interval_ns_bounds": [minimum, maximum],
                "available_percent": max(0.0, 100 * (1 - work_ns / ((minimum + maximum) / 2))),
                "available_percent_bounds": [
                    max(0.0, 100 * (1 - work_ns / period)) for period in (minimum, maximum)
                ],
                "burst_credit_bytes": BURST_BYTES,
            }
        status["service_estimate"] = estimate
        status["native_neighbor_metric_delivery_proven"] = False
        return status
