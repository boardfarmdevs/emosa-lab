"""Common interface/loss/service intervals for the owned shaped backhaul.

Root scheduler drops include child FIFO drops; use the root exactly once.
TC action, scheduler and veth-driver losses occur at separate path stages.
Complete peer/media qualification is still required before native reporting.
"""

from emosa.simulation.backhaul_accounting import BackhaulAccountingSource, interface_counters
from emosa.simulation.egress_accounting import observed_interface
from emosa.simulation.virtual_capacity import VirtualCapacitySource, service_counters


def counters(observation, ifindex, address):
    link = observed_interface(observation, ifindex, address)
    if any(row["mtu"] != 1500 for key in ("link", "link_after") for row in observation[key]):
        raise ValueError("unsupported_shaped_mtu")
    (root,) = [q for q in observation["qdiscs"] if q.get("root")]
    extra = [q for q in observation["qdiscs"] if not q.get("root")]
    if extra and (len(extra) != 1 or extra[0]["kind"] != "clsact" or extra[0].get("options") != {}):
        raise ValueError("unsupported_shaped_child_or_shared_block")
    filters = observation["filters"]
    if set(filters) != {"root", "ingress", "egress"} or filters["root"]:
        raise ValueError("unsupported_shaped_filter_path")
    values, path = interface_counters(link, bool(extra), filters)
    return values | service_counters(root), ("owned-tbf-100m-stab24-min84-v1", *path)


class ShapedBackhaulSource(VirtualCapacitySource):
    name = "shaped_backhaul"
    scope = "owned shaped eth1 TX/RX, disjoint action/scheduler/driver losses and service work"
    read_counters = staticmethod(counters)

    @staticmethod
    def losses(delta):
        values = BackhaulAccountingSource.losses(delta)
        values["transmit_losses"] += delta["queue_drops"]
        return values
