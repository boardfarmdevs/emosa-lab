# SPDX-License-Identifier: Apache-2.0
"""The agent keeps a GRE parent's tunnels to its children (spec 8.6).

``GreTunnels`` makes the parent AP's tunnels the set of its children
(``emosa.opensync.gre_parent``) whenever the two differ, with its own operation journal: a child
that associated and took a lease gets its tunnel, a child gone loses it, and with the parent AP
withdrawn every tunnel goes. A failed or refused write is not made again for the same children
on the same OpenSync start: a change in the children, or the next start, tries again.
"""

import logging

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.gre_parent import TunnelIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.gre_parent")
DEADLINE = 30  # nm shows a tunnel's interface within seconds of its row
SOURCE = "gre-parent-policy"


class GreTunnels:
    def __init__(self, pod_id, backend, store, vault, *, run_id, clock=None):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=TunnelIntent)
        self.engine.recover()
        self.waiting = None

    def latest(self):
        ops = self.store.operations()
        return ops[-1] if ops else None

    def intent(self):
        """The tunnels the parent AP's children need now."""
        return TunnelIntent(
            self.pod_id, self.backend.ap, self.backend.bridge, self.backend.children
        )

    def key(self, intent):
        """One write per OpenSync start and set of children."""
        return f"{self.backend.instance}:{','.join(intent.remotes)}"

    def _settle(self, op, snap):
        if op.state not in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            return
        target = TunnelIntent(**op.intent).target()
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
            log.info(
                "the parent AP %s's tunnels: %s",
                self.backend.ap,
                ", ".join(sorted(target["tunnels"])) or "none",
            )
        elif self.engine._expired(op):
            op.original_outcome, op.deadline_elapsed = op.state, True
            transition(op, State.TIMED_OUT)
            op.reason = Reason.APPLY_TIMEOUT
            self.engine._save(op)

    def _wanted(self, op, snap, intent):
        if not snap.ready:
            self.waiting = "pod not bound"
            return False
        if self.store.ownership(self.pod_id):
            self.waiting = "another manager changed the tunnels: admit the pod anew"
            return False
        if op is not None and op.state in ACTIVE:
            self.waiting = "write in progress"
            return False
        if snap.config.get("tunnels") == intent.target()["tunnels"]:
            self.waiting = None
            return False
        done = self.store.lookup(SOURCE, self.pod_id, self.key(intent))
        if done is not None:
            self.waiting = (
                None
                if done.state == State.OBSERVED_APPLIED
                else f"not made for these children: {done.state} {done.reason or ''}".strip()
            )
            return False
        self.waiting = None
        return True

    async def tick(self):
        try:
            snap = await self.backend.snapshot()
        except (EmosaError, ConnectionError, TimeoutError):
            snap = None
        op = self.latest()
        if op is not None:
            self._settle(op, snap)
        if snap is None:
            return
        intent = self.intent()
        if not self._wanted(self.latest(), snap, intent):
            return
        op = self.engine.request(
            intent, source=SOURCE, key=self.key(intent), run_id=self.run_id, deadline=DEADLINE
        )
        if op.state == State.REQUESTED:
            op = await self.engine.execute(op.operation_id)
            log.info("gre parent %s: %s %s", op.operation_id, op.state, op.reason or "")

    def status(self):
        op = self.latest()
        snap = self.backend.last
        return {
            "ap": self.backend.ap,
            "underlay": str(self.backend.underlay),
            "children": list(self.backend.children),
            "tunnels": None if snap is None else snap.config.get("tunnels"),
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
