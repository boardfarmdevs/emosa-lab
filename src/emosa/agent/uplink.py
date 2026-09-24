"""The agent performs the option-1 uplink switch itself (data-plane.md §5.3).

One ``UplinkSwitch`` per agent, with its own operation journal: the uplink is a
different scope from the fronthaul BSS, with its own lifecycle rules.

- **Credentials** come from the controller: the backhaul BSS of the applied M2
  set (multi-BSS mode). They can instead come from the agent configuration
  (``uplink.ssid`` and ``uplink.secret_ref``, a file in the agent's secret
  directory).
- **When:** the pod is bound, reports a working uplink, serves the controller's
  fronthaul with no fronthaul operation in flight (the switch moves the path a
  fronthaul write travels on), and is not already on the controller's backhaul.
  One operation per OpenSync start of the pod (its radio rows, see
  ``UplinkBackend``), so the switch is re-applied after every re-onboarding.
  A pod restarts to its bootstrap uplink, option 2, whenever OpenSync restarts.
- **Applied** only when the pod's State shows the Multi-AP backhaul STA as
  ``cm``'s only uplink, on the same instance the switch was written to.
- **Held:** a switch that is not confirmed within the deadline (the pod has
  restarted to option 2, or will), or is rejected, or is changed by another
  manager, holds the pod on option 2. EMOSA never retries on its own. A new
  admission clears the hold, because the fleet archives the agent's state.
"""

import logging

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.uplink import MULTI_AP, UplinkIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.uplink")
DEADLINE = 90  # observed: adopted in 27 s; OpenSync restarts a failed uplink in 40-120 s
SOURCE = "uplink-policy"


class UplinkSwitch:
    def __init__(
        self, pod_id, backend, store, vault, credentials, *, run_id, settled=None, clock=None
    ):
        """``credentials()`` -> (ssid, secret_ref) of the EasyMesh backhaul, or None.

        ``settled()`` is true while the fronthaul scope is served and idle.
        """
        self.pod_id, self.backend, self.store = pod_id, backend, store
        self.credentials, self.run_id = credentials, run_id
        self.settled = settled or (lambda: True)
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=UplinkIntent)
        self.engine.recover()
        self.waiting = None  # why no switch is being made, for status

    def latest(self):
        ops = self.store.operations()
        return ops[-1] if ops else None

    def held(self):
        return self.store.ownership(self.pod_id)

    def hold(self, op, reason):
        if not self.held():
            log.warning("uplink held on option 2: %s", reason)
            self.store.conflict(
                self.pod_id, {"operation_id": op.operation_id if op else None, "reason": reason}
            )

    def _save(self, op, payload=None):
        self.engine._save(op, payload)

    def _settle(self, op, snap):
        """Confirm or time out the latest operation; hold on failure."""
        if op.state in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            target = UplinkIntent(**op.intent).target(self.engine.vault)
            same_start = snap.ready and self.backend.instance == op.plan.get("instance")
            if (
                same_start
                and snap.observed.fresh
                and snap.observed.satisfies(target)
                and self.engine._matches(snap.config, target)
            ):
                evidence = self.engine._evidence(snap, instance=self.backend.instance)
                if op.state == State.INDETERMINATE:
                    evidence["attribution"] = "current_condition_only"
                transition(op, State.OBSERVED_APPLIED, evidence=evidence)
                op.application_evidence, op.reason = evidence, None
                self._save(op, {"observation": self.backend.facts})
                log.info("uplink on the EasyMesh backhaul: %s", self.backend.facts)
            elif self.engine._expired(op):
                op.original_outcome, op.deadline_elapsed = op.state, True
                transition(op, State.TIMED_OUT)
                op.reason = Reason.APPLY_TIMEOUT
                self._save(op, {"observation": self.backend.facts})
                self.hold(op, "switch not confirmed within the deadline")
        elif op.state == State.OBSERVED_APPLIED and snap.ready and snap.observed.fresh:
            target = UplinkIntent(**op.intent).target(self.engine.vault)
            same_start = self.backend.instance == op.plan.get("instance")
            if same_start and not self.engine._matches(snap.config, target):
                op.reason = Reason.OWNERSHIP_CONFLICT
                self._save(op, {"observation": self.backend.facts})
                self.hold(op, "the station's configuration was changed by another manager")
        elif op.state in (State.REJECTED, State.FAILED, State.OWNERSHIP_CONFLICT):
            # Only a rejection before anything was sent may be retried, on a later start.
            if op.state != State.REJECTED or op.reason not in (Reason.NOT_READY, Reason.BUSY):
                self.hold(op, f"switch {op.state.lower()}: {op.reason}")

    def _wanted(self, op, snap):
        """The intent to request now, or None (``self.waiting`` says why)."""
        if self.held():
            self.waiting = "held on option 2"
            return None
        if not snap.ready:
            self.waiting = "pod not bound"
            return None
        credentials = self.credentials()
        if credentials is None:
            self.waiting = "no EasyMesh backhaul credentials"
            return None
        intent = UplinkIntent(self.pod_id, self.backend.station, *credentials)
        try:
            intent.target(self.engine.vault)
        except EmosaError as exc:
            self.waiting = f"backhaul credentials unusable: {exc}"
            return None
        if op is not None and op.state in ACTIVE:
            self.waiting = "switch in progress"
            return None
        if op is not None and op.plan and op.plan.get("instance") == self.backend.instance:
            if op.state == State.OBSERVED_APPLIED and op.intent == intent.record():
                self.waiting = None  # applied on this start
                return None
            if op.state in (State.REJECTED, State.CANCELLED) and op.intent == intent.record():
                self.waiting = f"not switched on this start: {op.reason}"
                return None
        if (self.backend.facts or {}).get("kind") is None:
            self.waiting = "no working uplink yet"
            return None
        if not self.settled():
            self.waiting = "fronthaul not settled"
            return None
        self.waiting = None
        return intent

    async def tick(self):
        """One step, on the agent's refresh cadence."""
        try:
            snap = await self.backend.snapshot()
        except (EmosaError, ConnectionError, TimeoutError):
            return  # never read yet
        op = self.latest()
        if op is not None:
            self._settle(op, snap)
        intent = self._wanted(self.latest(), snap)
        if intent is None:
            return
        vault = self.engine.vault
        wanted = vault.fingerprint({**intent.record(), "target": intent.target(vault)})
        key = f"{self.backend.instance}:{wanted[:16]}"  # one switch per start and credential
        op = self.engine.request(
            intent, source=SOURCE, key=key, run_id=self.run_id, deadline=DEADLINE
        )
        if op.state != State.REQUESTED:
            return
        log.info("switching %s to the EasyMesh backhaul %r", intent.station, intent.ssid)
        op = await self.engine.execute(op.operation_id)
        log.info("uplink switch %s: %s %s", op.operation_id, op.state, op.reason or "")

    def status(self):
        op = self.latest()
        facts = self.backend.facts or {}
        return {
            "station": self.backend.station,
            "uplink": facts.get("kind"),
            "in_use": facts.get("in_use"),
            "parent": facts.get("parent"),
            "option": 1 if facts.get("kind") == MULTI_AP else 2,
            "held": self.held(),
            "waiting": self.waiting,
            "operation": None
            if op is None
            else {
                "operation_id": op.operation_id,
                "state": op.state,
                "reason": op.reason,
                "ssid": op.intent.get("ssid"),
                "instance": (op.plan or {}).get("instance"),
            },
        }


def m2_backhaul(store):
    """The backhaul BSS (SSID, secret reference) of the fronthaul scope's applied M2 set."""
    for op in reversed(store.operations()):
        if op.state == State.OBSERVED_APPLIED:
            for bss in op.intent.get("additional") or ():
                if bss.get("role") == "backhaul":
                    return bss["ssid"], bss["secret_ref"]
            return None
    return None
