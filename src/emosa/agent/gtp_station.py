# SPDX-License-Identifier: Apache-2.0
"""The agent writes a legacy pod's station on the site's GTP BSS (spec 3.10).

``GtpStation`` writes the station (``emosa.opensync.gtp_station``) once per start of the pod's
OpenSync, with its own operation journal, while the station has no row: a legacy pod's core
makes none, and its database is its vendor's seed again at every start. A row already there is
left alone, whoever wrote it. A failed or refused write is not retried on the same start: the
next start, or a new admission, tries again.

The guard: the pods' vendor healthcheck reboots a pod after ten minutes of a configured station
that is not associated. The station in the scope's form (the GTP's SSID) that stays unassociated
for ``unassociated`` seconds (300) is deleted, and written again after a back-off (``retry``: 15,
30, then 60 minutes; back to the first once a write associates). On a converted pod
(``guard_all``) every other station row is guarded the same way, and never written again: the
seed's onboarding stations or a hook's, which the radio plan deletes only while the pod is wired.
"""

import logging
import time

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.gtp_station import GtpStationIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.gtp_station")
DEADLINE = 30  # wm shows the station as soon as it has taken the row
SOURCE = "gtp-station-policy"
UNASSOCIATED = 300  # well inside the vendor healthcheck's ten failing minutes
RETRY = (900, 1800, 3600)


class GtpStation:
    def __init__(
        self,
        pod_id,
        backend,
        store,
        vault,
        intent,
        *,
        run_id,
        clock=None,
        unassociated=UNASSOCIATED,
        retry=RETRY,
        monotonic=time.monotonic,
        guard_all=False,
    ):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.intent = intent
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=GtpStationIntent)
        self.engine.recover()
        self.waiting = None
        self.unassociated, self.retry, self.monotonic = unassociated, tuple(retry), monotonic
        self.start = None  # the start of the pod's OpenSync the attempts below count on
        self.attempt = 0  # the station's writes removed on that start
        self.since = None  # the station in the scope's form unassociated since (monotonic)
        self.retry_at = None  # no write before (monotonic), after a removal
        self.backoff = 0  # the next removal's back-off: retry[backoff]
        self.removed = 0  # stations removed unassociated, since the agent started
        self.guard_all = guard_all  # every station row guarded (a converted pod)
        self.others = {}  # another station row's uuid -> unassociated since (monotonic)
        self.others_removed = 0  # other station rows removed unassociated, since the agent started

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
        """One write per OpenSync start, station and credential (a start EMOSA continues gets one
        more when the credential changed), and one more after each removal."""
        vault = self.engine.vault
        try:
            target = self.intent.target(vault)
        except EmosaError:
            target = None  # an unusable credential: the request refuses it
        wanted = vault.fingerprint({**self.intent.record(), "target": target})
        key = f"{self.backend.instance}:{self.intent.station}:{wanted[:16]}"
        return key if not self.attempt else f"{key}:{self.attempt}"

    async def _guard(self, snap):
        """The station's guard: True once it removed the station (nothing else on this tick)."""
        config = snap.config if snap.ready else {}
        ours = config.get("ssid") == self.intent.ssid  # the station in the scope's form
        if not ours or config.get("associated"):
            if ours:
                self.backoff = 0  # a write that associated
            self.since = None
            return False
        now = self.monotonic()
        if self.since is None:
            self.since = now
        if now - self.since < self.unassociated:
            return False
        try:
            deleted = await self.backend.remove(self.intent)
        except (EmosaError, ConnectionError, TimeoutError) as exc:
            log.warning("the pod's station %s: not removed: %s", self.intent.station, exc)
            return True
        delay = self.retry[min(self.backoff, len(self.retry) - 1)]
        self.attempt, self.backoff, self.removed = (
            self.attempt + 1,
            self.backoff + 1,
            self.removed + 1,
        )
        self.since, self.retry_at = None, now + delay
        self.store.event(
            self.run_id,
            "GTP_STATION_REMOVED",
            {"station": self.intent.station, "unassociated_s": self.unassociated, "retry_s": delay},
            pod_id=self.pod_id,
        )
        log.warning(
            "the pod's station %s was not associated for %d s: removed (%d rows), written again in"
            " %d s",
            self.intent.station,
            self.unassociated,
            deleted,
            delay,
        )
        return True

    async def _guard_others(self, snap):
        """The guard over every other station row (a converted pod): True once it removed one."""
        if not (self.guard_all and snap.ready):
            return False
        now = self.monotonic()
        stations = [s for s in snap.config.get("stations") or [] if not s["own"]]
        for station in stations:
            if station["associated"]:
                self.others.pop(station["uuid"], None)
            else:
                self.others.setdefault(station["uuid"], now)
        present = {s["uuid"] for s in stations}
        self.others = {u: t for u, t in self.others.items() if u in present}
        for station in stations:
            since = self.others.get(station["uuid"])
            if since is None or now - since < self.unassociated:
                continue
            try:
                await self.backend.remove_station(station["uuid"])
            except (EmosaError, ConnectionError, TimeoutError) as exc:
                log.warning("the pod's station %s: not removed: %s", station["if_name"], exc)
                return True
            self.others.pop(station["uuid"], None)
            self.others_removed += 1
            self.store.event(
                self.run_id,
                "STATION_REMOVED",
                {"station": station["if_name"], "unassociated_s": self.unassociated},
                pod_id=self.pod_id,
            )
            log.warning(
                "the pod's station %s (not EMOSA's) was not associated for %d s: removed",
                station["if_name"],
                self.unassociated,
            )
            return True
        return False

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
        if self.retry_at is not None and self.monotonic() < self.retry_at:
            self.waiting = (
                f"removed, not associated: written again in "
                f"{int(self.retry_at - self.monotonic())} s"
            )
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
        if snap is None:
            return
        if snap.ready and self.start != self.backend.instance:  # a new start: its own attempts
            self.start, self.attempt, self.since, self.retry_at = (
                self.backend.instance,
                0,
                None,
                None,
            )
            self.others = {}
        if await self._guard(snap) or await self._guard_others(snap):
            return
        if not self._wanted(self.latest(), snap):
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
            "associated": None if snap is None else bool(snap.config.get("associated")),
            "removed_unassociated": self.removed,
            "others_removed_unassociated": self.others_removed,
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
