# SPDX-License-Identifier: Apache-2.0
"""When the agent asks for a fresh attempt on its own (the agent loop's timeouts).

- **Unserved:** provisioned, yet the pod serves no BSS of the controller and
  nothing is being written for ``UNSERVED_RENEW`` (its configuration was lost,
  e.g. a write lost to an uplink move): ask for the configuration again.
- **No M2:** an M1 sent and no M2 for ``M2_TIMEOUT``: a controller that restarted
  meanwhile has forgotten the M1, and its other queries keep the controller
  timeout from firing.
- **No Topology Query:** provisioned, and no Topology Query from the controller for
  the configured window (``topology_query_window``; none: the rule is off). A
  controller that restarted and forgot the agent may keep sending its other queries
  but no Topology Query (RDK). Only for a controller that queries its agents
  periodically: prplMesh queries on events only.
- **Silent controller:** no message from the controller for ``CONTROLLER_TIMEOUT``,
  about two discovery periods, like a native agent's controller connectivity check.

Each rule's timer restarts when it fires. The caller renews the session (a fresh
M1) once per reason.
"""

CONTROLLER_TIMEOUT = 130
UNSERVED_RENEW = 60
M2_TIMEOUT = 30


class RenewRules:
    def __init__(self, now, *, topology_query_window=None):
        self.last_contact = self.last_query = now
        self.unserved_since = self.awaiting_since = self.provisioning_since = None
        self.topology_query_window = topology_query_window

    def contact(self, now):
        """A frame from the controller arrived."""
        self.last_contact = now

    def topology_query(self, now):
        """A Topology Query from the controller arrived."""
        self.last_query = now

    def check(self, now, *, unserved, awaiting, provisioning=False):
        """The reasons to renew now, in order: "unserved", "no_m2", "no_topology_query",
        "controller_silent".

        ``unserved``: provisioned, the pod serves none of the controller's BSSes and
        no fronthaul write is active; ``awaiting``: M1 sent, no M2 yet;
        ``provisioning``: M2 accepted. A renewal for an unserved pod ends the
        session, so it is no longer awaiting an M2 nor provisioning.
        """
        reasons = []
        if not unserved:
            self.unserved_since = None
        elif self.unserved_since is None:
            self.unserved_since = now
        if self.unserved_since is not None and now - self.unserved_since > UNSERVED_RENEW:
            reasons.append("unserved")
            self.unserved_since = None
            awaiting = provisioning = False
        if not awaiting:
            self.awaiting_since = None
        elif self.awaiting_since is None:
            self.awaiting_since = now
        if self.awaiting_since is not None and now - self.awaiting_since > M2_TIMEOUT:
            reasons.append("no_m2")
            self.awaiting_since = None
        if not provisioning or self.topology_query_window is None:
            self.provisioning_since = None
        elif self.provisioning_since is None:
            self.provisioning_since = now
        if (
            self.provisioning_since is not None
            and now - max(self.provisioning_since, self.last_query) > self.topology_query_window
        ):
            reasons.append("no_topology_query")
            self.provisioning_since = None
        if now - self.last_contact > CONTROLLER_TIMEOUT:
            reasons.append("controller_silent")
            self.last_contact = now
        return reasons
