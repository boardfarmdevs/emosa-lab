"""Joint receive/transmit accounting for the selected owned veth/TC path.

RX packets are interface arrivals, not client deliveries. TC ingress drops are
separate from the kernel's aggregate RX-drop counter. Complete peer attribution,
capacity and media qualification are still required before publishing metrics.
"""

from emosa.simulation.egress_accounting import (
    EgressAccountingSource,
    action_drops,
    observed_path,
)
from emosa.simulation.forwarding import integer


def interface_counters(link, clsact, filters):
    transmit, tx_action = action_drops(filters["egress"], clsact, "egress")
    receive, rx_action = action_drops(filters["ingress"], clsact, "ingress")
    if tx_action is not None and tx_action == rx_action:
        raise ValueError("shared_action_across_directions")
    rx, tx = link["stats64"]["rx"], link["stats64"]["tx"]
    if integer(rx["errors"]) or integer(tx["errors"]):
        raise ValueError("unexpected_veth_error_semantics")
    return {
        "tx_packets": integer(tx["packets"]),
        "tx_bytes": integer(tx["bytes"]),
        "tx_driver_drops": integer(tx["dropped"]),
        "tx_action_drops": transmit,
        "rx_packets": integer(rx["packets"]),
        "rx_bytes": integer(rx["bytes"]),
        "rx_interface_drops": integer(rx["dropped"]),
        "rx_action_drops": receive,
    }, (clsact, tx_action, rx_action)


def path_counters(observation, ifindex, address):
    return interface_counters(*observed_path(observation, ifindex, address))


class BackhaulAccountingSource(EgressAccountingSource):
    name = "backhaul"
    scope = "owned eth1 common TX/RX reads; selected driver, stack and disjoint TC drops"
    read_counters = staticmethod(path_counters)

    @staticmethod
    def losses(delta):
        return {
            "transmit_losses": delta["tx_driver_drops"] + delta["tx_action_drops"],
            "receive_losses": delta["rx_interface_drops"] + delta["rx_action_drops"],
        }
