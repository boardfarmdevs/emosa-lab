"""Which stations the pod watches for probe requests (spec §3.9).

The controller's Unassociated STA Link Metrics Queries name the stations it
wants measured. The agent remembers each one (``ask``) and keeps a watch row
on the pod for the most recently asked (``emosa.opensync.probe_watch``), so
that the pod reports their probe requests and a later query can be answered.
A station is dropped when it was not asked about for ``TTL``, when it is
associated with the pod (the query refuses it anyway, and client steering
needs the row), and when another manager has a row for it. The watch set is
written at most every ``MIN_INTERVAL``, one guarded write at a time.
"""

import hashlib
import time

from emosa.errors import Reason
from emosa.model import ACTIVE, State
from emosa.opensync.probe_watch import MAX_WATCHED, WatchIntent
from emosa.operations import transition
from emosa.reconcile import Engine

SOURCE = "probe-watch"
TTL = 600  # seconds without a query before a station is no longer watched
MIN_INTERVAL = 10  # seconds between two writes of the watch set
DEADLINE = 30
MAX_ASKED = 256


class ProbeWatch:
    def __init__(
        self, pod_id, backend, store, vault, *, if_name, band, run_id, clock=None, monotonic=None
    ):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.if_name, self.band = if_name, band
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=WatchIntent)
        self.engine.recover()
        self.monotonic = monotonic or time.monotonic
        self.asked = {}  # mac -> when last asked (monotonic)
        self.last_request = None
        self.waiting = None

    def ask(self, stations):
        now = self.monotonic()
        for mac in stations:
            self.asked[mac] = now
        if len(self.asked) > MAX_ASKED:
            for mac in sorted(self.asked, key=self.asked.get)[: len(self.asked) - MAX_ASKED]:
                del self.asked[mac]

    def latest(self):
        ops = self.store.operations()
        return ops[-1] if ops else None

    def wanted(self, config):
        now = self.monotonic()
        excluded = set(config["blocked"]) | set(config["associated"])
        recent = sorted(
            (t, mac) for mac, t in self.asked.items() if now - t <= TTL and mac not in excluded
        )
        return tuple(sorted(mac for _, mac in recent[-MAX_WATCHED:]))

    def _settle(self, op, snap):
        if op.state not in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            return
        target = WatchIntent(**op.intent).target()
        if (
            snap is not None
            and snap.ready
            and snap.observed.satisfies(target)
            and self.engine._matches(snap.config, target)
        ):
            evidence = self.engine._evidence(snap, instance=self.backend.instance)
            if op.state == State.INDETERMINATE:
                evidence["attribution"] = "current_condition_only"
            self._transition(op, State.OBSERVED_APPLIED, evidence)
        elif self.engine._expired(op):
            op.original_outcome, op.deadline_elapsed = op.state, True
            self._transition(op, State.TIMED_OUT, None)

    def _transition(self, op, state, evidence):
        if evidence is None:
            transition(op, state)
            op.reason = Reason.APPLY_TIMEOUT
        else:
            transition(op, state, evidence=evidence)
            op.application_evidence, op.reason = evidence, None
        self.engine._save(op)

    async def tick(self):
        snap = await self.backend.snapshot()
        op = self.latest()
        if op is not None:
            self._settle(op, snap)
            op = self.latest()
        if not snap.ready:
            self.waiting = "pod not bound"
            return
        if self.store.ownership(self.pod_id):
            self.waiting = "another manager changed the configuration: admit the pod anew"
            return
        if op is not None and op.state in ACTIVE:
            self.waiting = "write in progress"
            return
        wanted = self.wanted(snap.config)
        if ",".join(wanted) == snap.config["watched"]:
            self.waiting = None
            return
        now = self.monotonic()
        if self.last_request is not None and now - self.last_request < MIN_INTERVAL:
            self.waiting = "next write after the minimum interval"
            return
        self.last_request = now
        intent = WatchIntent(self.pod_id, self.if_name, self.band, wanted)
        digest = hashlib.sha256(",".join(wanted).encode()).hexdigest()[:12]
        key = f"{self.backend.instance}:{len(self.store.operations())}:{digest}"
        op = self.engine.request(
            intent, source=SOURCE, key=key, run_id=self.run_id, deadline=DEADLINE
        )
        if op.state == State.REQUESTED:
            await self.engine.execute(op.operation_id)
        self.waiting = None

    def status(self):
        op = self.latest()
        config = self.backend.last.config if self.backend.last else {}
        return {
            "watched": [m for m in config.get("watched", "").split(",") if m],
            "asked": len(self.asked),
            "waiting": self.waiting,
            "operation": None
            if op is None
            else {"operation_id": op.operation_id, "state": op.state, "reason": op.reason},
        }
