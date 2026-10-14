# SPDX-License-Identifier: Apache-2.0
"""The agent writes a legacy pod's station on the site's GTP BSS (spec 3.10).

``GtpStation`` writes the station (``emosa.opensync.gtp_station``) once per start of the pod's
OpenSync, with its own operation journal, while the station has no row: a legacy pod's core
makes none, and its database is its vendor's seed again at every start. A row already there is
left alone, whoever wrote it. A failed or refused write is not retried on the same start: the
next start, or a new admission, tries again.
"""

import logging

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.gtp_station import GtpStationIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.gtp_station")
DEADLINE = 30  # wm shows the station as soon as it has taken the row
SOURCE = "gtp-station-policy"


class GtpStation:
    def __init__(self, pod_id, backend, store, vault, intent, *, run_id, clock=None):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.intent = intent
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=GtpStationIntent)
        self.engine.recover()
        self.waiting = None

    def latest(self):
        ops = self.store.operations()
        return ops[-1] if ops else None

    def _settle(self, op, snap):
        if op.state not in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            return
        target = GtpStationIntent(**op.intent).target(self.engine.vault)
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
            log.info("the pod's station %s is on the GTP's SSID", self.intent.station)
        elif self.engine._expired(op):
            op.original_outcome, op.deadline_elapsed = op.state, True
            transition(op, State.TIMED_OUT)
            op.reason = Reason.APPLY_TIMEOUT
            self.engine._save(op)

    def key(self):
        """One write per OpenSync start and station."""
        return f"{self.backend.instance}:{self.intent.station}"

    def _wanted(self, op, snap):
        if not snap.ready:
            self.waiting = "pod not bound"
            return False
        if self.store.ownership(self.pod_id):
            self.waiting = "another manager changed the station: admit the pod anew"
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
        if snap.config.get("present"):
            # the pod's, an operator's or a start-up hook's station (spec 3.10)
            self.waiting = f"{self.intent.station} has a row: left alone"
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
            log.info("GTP station %s: %s %s", op.operation_id, op.state, op.reason or "")

    def status(self):
        """The scope's status: never the passphrase, only its reference's use."""
        op = self.latest()
        snap = self.backend.last
        return {
            "station": self.intent.station,
            "ssid": self.intent.ssid,
            "pinned": self.intent.bssid is not None,
            "present": None if snap is None else bool(snap.config.get("present")),
            "joined": None if snap is None else bool(snap.config.get("joined")),
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
