"""Local diagnostic representations, never evidence of EasyMesh wire onboarding."""

import copy
import time
import uuid

from emosa.errors import EmosaError, Reason

FRESHNESS_SECONDS = 2.0


def validate_bindings(config):
    for pod in config["pods"]:
        if pod.get("mapping_scope") == "sole-fronthaul-radio" and (
            config["backend_mode"] != "ovsdb-sim" or "virtual_agent" not in pod
        ):
            raise EmosaError(
                Reason.INVALID_INPUT, "sole-radio scope requires a bound simulation pod"
            )
    bindings = [p["virtual_agent"] for p in config["pods"] if "virtual_agent" in p]
    if bindings and config["backend_mode"] != "ovsdb-sim":
        raise EmosaError(Reason.INVALID_INPUT, "virtual agent directory requires ovsdb-sim")
    for field in ("al_mac", "expected_serial"):
        if len({b[field] for b in bindings}) != len(bindings):
            raise EmosaError(Reason.INVALID_INPUT, "duplicate virtual agent binding", field=field)


class AgentDirectory:
    def __init__(self, pods, backend_mode="ovsdb-sim"):
        self.bindings = {p["pod_id"]: p["virtual_agent"] for p in pods if "virtual_agent" in p}
        self.backend_mode = backend_mode
        self.instance_id = str(uuid.uuid4())
        self.observations = {}
        self.failures = {}

    def observe(self, pod_id, inventory):
        if pod_id in self.bindings:
            self.observations[pod_id] = (time.monotonic(), copy.deepcopy(inventory))
            self.failures.pop(pod_id, None)

    def invalidate(self, pod_id, reason="NOT_READY"):
        self.failures[pod_id] = str(reason)

    def view(self, *, offset=0, limit=100):
        agents = []
        now = time.monotonic()
        for pod_id, binding in self.bindings.items():
            observed_at, inventory = self.observations.get(pod_id, (None, {}))
            age = None if observed_at is None else max(0, now - observed_at)
            fresh = age is not None and age <= FRESHNESS_SECONDS and pod_id not in self.failures
            agents.append(
                {
                    "pod_id": pod_id,
                    "al_mac": binding["al_mac"],
                    "identity_source": "configured synthetic AL address",
                    "identity_check": "serial match in private simulation; not attestation",
                    "state": "ready" if fresh else "unavailable" if inventory else "pending",
                    "fresh": fresh,
                    "reason": None if fresh else self.failures.get(pod_id, "NOT_READY"),
                    "last_observation_age_seconds": age,
                    "inventory": {**inventory, "fresh": fresh, "ready": fresh}
                    if inventory
                    else None,
                }
            )
        return {
            "schema_version": 1,
            "interface": "local-diagnostic",
            "backend_mode": self.backend_mode,
            "adapter_instance_id": self.instance_id,
            "freshness_limit_seconds": FRESHNESS_SECONDS,
            "easymesh_wire_state": "blocked_P0",
            "controller_onboarding_proven": False,
            "physical_pod_proven": False,
            "agents": agents[offset : offset + limit],
            "offset": offset,
            "total": len(agents),
        }
