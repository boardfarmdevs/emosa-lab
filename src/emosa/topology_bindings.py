"""Explicit synthetic identity bindings; no enrollment or physical attestation."""

import copy
import hashlib
import json

from emosa.errors import EmosaError, Reason

REGISTRY_KEY = "virtual_topology_bindings_v1"
MAX_INTERFACES = 64
MAX_RETAINED_PODS = 32


def _require(condition, message):
    if not condition:
        raise EmosaError(Reason.INVALID_INPUT, message)


def canonical_binding(pod):
    agent = pod.get("virtual_agent", {})
    if "topology" not in agent:
        return None
    value = copy.deepcopy(agent)
    value["pod_id"] = pod["pod_id"]
    value["topology"]["radios"].sort(key=lambda r: r["radio_id"])
    for radio in value["topology"]["radios"]:
        radio["interfaces"].sort(key=lambda v: v["interface_id"])
    return value


def binding_digest(binding):
    return hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_profiles(profiles, *, other_al_macs=()):
    """Validate cross-resource uniqueness before pinning or opening pod sessions."""
    _require(len(profiles) <= MAX_RETAINED_PODS, "topology registry pod budget exhausted")
    als = list(other_al_macs) + [p["al_mac"] for p in profiles.values()]
    serials = [p["expected_serial"] for p in profiles.values()]
    _require(len(serials) == len(set(serials)), "duplicate retained topology serial binding")
    ruids, radio_macs, interface_macs = [], [], []
    for profile in profiles.values():
        radios = profile["topology"]["radios"]
        interfaces = [v for r in radios for v in r["interfaces"]]
        _require(len(interfaces) <= MAX_INTERFACES, "topology interface budget exhausted")
        for values in (
            [r["radio_id"] for r in radios],
            [r["if_name"] for r in radios],
            [v["interface_id"] for v in interfaces],
            [v["if_name"] for v in interfaces],
        ):
            _require(
                len(values) == len(set(values)), "duplicate topology identity or interface name"
            )
        ruids.extend(r["ruid"] for r in radios)
        radio_macs.extend(r["expected_mac"] for r in radios)
        interface_macs.extend(v["expected_mac"] for v in interfaces)
    for values in (als + ruids, radio_macs, interface_macs):
        _require(len(values) == len(set(values)), "topology address collision")
        _require("00:00:00:00:00:00" not in values, "zero topology address")


def validate_config_topologies(pods):
    profiles = {
        p["pod_id"]: canonical_binding(p) for p in pods if "topology" in p.get("virtual_agent", {})
    }
    validate_profiles(
        profiles,
        other_al_macs=[
            p["virtual_agent"]["al_mac"]
            for p in pods
            if "virtual_agent" in p and p["pod_id"] not in profiles
        ],
    )
    for pod in pods:
        if pod["pod_id"] not in profiles:
            continue
        radios = profiles[pod["pod_id"]]["topology"]["radios"]
        selected = [r for r in radios if r["radio_id"] == pod["radio_id"]]
        _require(
            len(selected) == 1 and selected[0]["if_name"] == pod["radio_name"],
            "topology does not bind designated radio",
        )
        _require(
            any(
                v["interface_id"] == pod["bss_id"]
                and v["if_name"] == pod["if_name"]
                and v["mode"] == "ap"
                for v in selected[0]["interfaces"]
            ),
            "topology does not bind designated BSS",
        )


def merge_registry(saved, pods):
    """Keep removed identities reserved; fail changes atomically without repinning.

    Endpoint/IP and database UUIDs are deliberately absent from stable identity.
    The local operator config is authority only for this isolated simulation.
    """
    merged = copy.deepcopy(saved)
    for pod in pods:
        value = canonical_binding(pod)
        prior = saved.get(pod["pod_id"])
        if prior is not None and prior != value:
            raise EmosaError(
                Reason.PRECONDITION_FAILED,
                "persisted topology binding differs; explicit migration is required",
                pod_id=pod["pod_id"],
            )
        if value is not None:
            merged[pod["pod_id"]] = value
    validate_profiles(
        merged,
        other_al_macs=[
            p["virtual_agent"]["al_mac"]
            for p in pods
            if "virtual_agent" in p and p["pod_id"] not in merged
        ],
    )
    return merged
