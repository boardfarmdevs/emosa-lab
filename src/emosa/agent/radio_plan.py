# SPDX-License-Identifier: Apache-2.0
"""The agent writes a legacy pod's radio plan (spec 3.10, profile ``radio_plan: emosa``).

``RadioPlan`` writes the plan (``emosa.opensync.radio_plan``) at most once per start of the
pod's OpenSync, with its own operation journal: when something differs from it (an unclaimed AP
on, its Inet row on, the wired pod's fronthaul radio off the profile's channel). Not repeated on
that start, even when a row is changed again: the next start writes it again.
"""

import logging

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.radio_plan import RadioPlanIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.radio_plan")
DEADLINE = 30  # wm shows an AP off as soon as it has taken the row
SOURCE = "radio-plan-policy"


class RadioPlan:
    def __init__(self, pod_id, backend, store, vault, *, run_id, clock=None):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.intent = backend.intent
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=RadioPlanIntent)
        self.engine.recover()
        self.waiting = None

    def latest(self):
        ops = self.store.operations()
        return ops[-1] if ops else None

    def _settle(self, op, snap):
        if op.state not in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            return
        target = RadioPlanIntent(**{**op.intent, "claimed": tuple(op.intent["claimed"])}).target()
        same_start = snap is not None and self.backend.instance == op.plan.get("instance")
        if (
            same_start
            and snap.ready
            and snap.observed.satisfies(target)
            and self.engine._matches(snap.config, target)
        ):
            evidence = self.engine._evidence(snap, instance=self.backend.instance)
            if op.state == State.INDETERMINATE:
                evidence["attribution"] = "current_condition_only"
            transition(op, State.OBSERVED_APPLIED, evidence=evidence)
            op.application_evidence, op.reason = evidence, None
            self.engine._save(op)
            log.info("the pod's radio plan is applied")
        elif self.engine._expired(op):
            op.original_outcome, op.deadline_elapsed = op.state, True
            transition(op, State.TIMED_OUT)
            op.reason = Reason.APPLY_TIMEOUT
            self.engine._save(op)

    def key(self):
        """At most one write per OpenSync start."""
        return f"{self.backend.instance}:radio-plan"

    def _wanted(self, op, snap):
        if not snap.ready:
            self.waiting = "pod not bound"
            return False
        if self.store.ownership(self.pod_id):
            self.waiting = "another manager changed the plan: admit the pod anew"
            return False
        if op is not None and op.state in ACTIVE:
            self.waiting = "write in progress"
            return False
        done = self.store.lookup(SOURCE, self.pod_id, self.key())
        if done is not None:
            self.waiting = (
                None
                if done.state == State.OBSERVED_APPLIED
                else f"not written on this start: {done.state} {done.reason or ''}".strip()
            )
            return False
        self.waiting = None
        return bool(snap.config.get("pending"))

    async def tick(self):
        try:
            snap = await self.backend.snapshot()
        except (EmosaError, ConnectionError, TimeoutError):
            snap = None
        op = self.latest()
        if op is not None:
            self._settle(op, snap)
        if snap is None or not self._wanted(self.latest(), snap):
            return
        op = self.engine.request(
            self.intent, source=SOURCE, key=self.key(), run_id=self.run_id, deadline=DEADLINE
        )
        if op.state == State.REQUESTED:
            op = await self.engine.execute(op.operation_id)
            log.info("radio plan %s: %s %s", op.operation_id, op.state, op.reason or "")

    def status(self):
        op = self.latest()
        snap = self.backend.last
        config = {} if snap is None else snap.config
        return {
            "unclaimed": config.get("unclaimed"),
            "unclaimed_enabled": config.get("unclaimed_enabled"),
            "channel": config.get("channel"),
            "wired": config.get("wired"),
            "waiting": self.waiting,
            "operation": None
            if op is None
            else {
                "operation_id": op.operation_id,
                "state": op.state,
                "reason": op.reason,
                "instance": (op.plan or {}).get("instance"),
            },
        }
