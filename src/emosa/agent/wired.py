# SPDX-License-Identifier: Apache-2.0
"""The agent bridges a wired pod's Ethernet uplink into its home bridge (spec §8.4).

``WiredUplink`` writes ``Connection_Manager_Uplink.bridge`` of the configured port
(``emosa.opensync.wired``) once per OpenSync start, with its own operation journal, when
``cm`` uses the port as the pod's uplink: the operator's cloud does it for a wired pod,
and EMOSA stands in for the cloud. A failed or refused write is not retried on the same
start: the next start, or a new admission, tries again. A bridge another manager set is
left alone.
"""

import logging

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.wired import WiredIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.wired")
DEADLINE = 30  # cm shows the bridge in its row as soon as it has taken it
SOURCE = "wired-uplink-policy"


class WiredUplink:
    def __init__(self, pod_id, backend, store, vault, intent, *, run_id, clock=None):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.intent = intent
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=WiredIntent)
        self.engine.recover()
        self.waiting = None

    def latest(self):
        ops = self.store.operations()
        return ops[-1] if ops else None

    def _settle(self, op, snap):
        if op.state not in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            return
        target = WiredIntent(**op.intent).target()
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
            log.info("the pod's uplink %s is in %s", self.intent.port, self.intent.bridge)
        elif self.engine._expired(op):
            op.original_outcome, op.deadline_elapsed = op.state, True
            transition(op, State.TIMED_OUT)
            op.reason = Reason.APPLY_TIMEOUT
            self.engine._save(op)

    def key(self):
        """One write per OpenSync start, port and bridge."""
        return f"{self.backend.instance}:{self.intent.port}:{self.intent.bridge}"

    def _wanted(self, op, snap):
        if not snap.ready:
            self.waiting = "pod not bound"
            return False
        if self.store.ownership(self.pod_id):
            self.waiting = "another manager changed the uplink: admit the pod anew"
            return False
        if op is not None and op.state in ACTIVE:
            self.waiting = "write in progress"
            return False
        if not snap.config.get("uplink"):
            self.waiting = f"{self.intent.port} is not the pod's Ethernet uplink in use"
            return False
        if snap.config.get("bridge") == self.intent.bridge:
            self.waiting = None  # already bridged on this start (by EMOSA or anyone)
            return False
        done = self.store.lookup(SOURCE, self.pod_id, self.key())
        if done is not None:
            self.waiting = (
                None
                if done.state == State.OBSERVED_APPLIED
                else f"not bridged on this start: {done.state} {done.reason or ''}".strip()
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
        if snap is None or not self._wanted(self.latest(), snap):
            return
        op = self.engine.request(
            self.intent, source=SOURCE, key=self.key(), run_id=self.run_id, deadline=DEADLINE
        )
        if op.state == State.REQUESTED:
            op = await self.engine.execute(op.operation_id)
            log.info("wired uplink %s: %s %s", op.operation_id, op.state, op.reason or "")

    def status(self):
        op = self.latest()
        snap = self.backend.last
        return {
            "station": None,
            "mode": "ethernet",
            "port": self.intent.port,
            "bridge": None if snap is None else snap.config.get("bridge"),
            # the port while cm uses it as the pod's uplink
            "in_use": self.intent.port if snap is not None and snap.config.get("uplink") else None,
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
