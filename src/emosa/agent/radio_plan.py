# SPDX-License-Identifier: Apache-2.0
"""The agent writes a legacy pod's radio plan (spec 3.10, profile ``radio_plan: emosa``).

``RadioPlan`` writes the plan (``emosa.opensync.radio_plan``) at most once per start of the
pod's OpenSync and plan, with its own operation journal: when something differs from it (an
unclaimed AP's Inet row on, the wired pod's fronthaul radio off the profile's channel; the AP
itself is left on). Not repeated on that start, even when a row is changed again: the next start
writes it again, and so does a changed plan on the same start (going back to an earlier one
included).

The fronthaul radio follows a station on it that holds its channel (the GTP station, or a Wi-Fi
uplink): the pod's OpenSync enforces its Config's channel, so a radio its station moved (a
gateway's channel change) is pulled back, the station dropped, and again, each a fresh session
(the bench, 20 October: 32 per pod in ten minutes). The plan then has the station's channel and
width (``follows``): that write alone, one per move, at most one per FOLLOW_HOLD seconds, so a
flip cannot become a write loop; the station's channel changing again within it is written once
it ends (its latest channel, when the Config does not have it). The followed channel stays when
the station is gone (a gateway that comes back from an outage on another channel brings it back
on that one): the profile's channel is the plan's only on a start where no station has held the
radio yet.
"""

import dataclasses
import logging
import time

from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, State
from emosa.opensync.radio_plan import RadioPlanIntent
from emosa.operations import transition
from emosa.reconcile import Engine
from emosa.store import start_write

log = logging.getLogger("emosa.agent.radio_plan")
DEADLINE = 30  # wm shows an AP off as soon as it has taken the row
SOURCE = "radio-plan-policy"
FOLLOW_HOLD = 20  # seconds between two writes of the station's channel


class RadioPlan:
    def __init__(
        self, pod_id, backend, store, vault, *, run_id, clock=None, monotonic=time.monotonic
    ):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.base = self.intent = backend.intent  # the profile's plan; self.intent the one wanted
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=RadioPlanIntent)
        self.engine.recover()
        self.waiting = None
        self.key = None  # the wanted write's idempotency key (start_write)
        self.monotonic = monotonic
        # the station's (channel, ht_mode) the plan follows, or None; the one it waits to follow
        # (the hold-down not over); when it last took one; the start it is of; how many times the
        # plan followed a station
        self.follow = self.wanted = self.followed_at = self.follow_start = None
        self.follow_count = 0

    def _follow(self, config):
        """spec 3.10: the plan follows the station that holds the radio (a channel its Config does
        not have), at most once per FOLLOW_HOLD, the latest channel when the station moved again
        within it; the followed channel kept when the station is gone. A new start of the pod's
        OpenSync (its seed's radio) has the profile's plan until a station holds the radio."""
        now = self.monotonic()
        if self.backend.instance != self.follow_start:
            self.follow = self.wanted = self.followed_at = None
            self.follow_start = self.backend.instance
        station = config.get("station_channel")
        if station is not None:
            width = config.get("station_ht_mode") or config.get("ht_mode") or self.base.ht_mode
            self.wanted = (station, width) if station != config.get("channel") else None
        free = self.followed_at is None or now - self.followed_at >= FOLLOW_HOLD
        if self.wanted is not None and self.wanted != self.follow and free:
            self.follow, self.followed_at, self.wanted = self.wanted, now, None
            self.follow_count += 1
            log.info(
                "the radio's station moved it to channel %d: the radio plan follows it",
                self.follow[0],
            )
        self.intent = (
            dataclasses.replace(
                self.base, channel=self.follow[0], ht_mode=self.follow[1], follows=True
            )
            if self.follow
            else self.base
        )
        self.backend.intent = self.intent

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

    def start_write(self):
        """At most one write per OpenSync start and plan (``emosa.store.start_write``): a start
        EMOSA continues (the same start marker after an EMOSA restart) gets one more when the plan
        changed, and so does going back to an earlier plan on that start."""
        wanted = self.engine.vault.fingerprint(self.intent.record())
        prefix = f"{self.backend.instance}:radio-plan:"
        latest = self.store.start_latest(SOURCE, self.pod_id, prefix)
        return start_write(latest, prefix + wanted[:16])

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
        done, self.key = self.start_write()
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
        if snap is not None and snap.ready:
            self._follow(snap.config)
        if snap is None or not self._wanted(self.latest(), snap):
            return
        op = self.engine.request(
            self.intent, source=SOURCE, key=self.key, run_id=self.run_id, deadline=DEADLINE
        )
        if op.state == State.REQUESTED:
            op = await self.engine.execute(op.operation_id)
            log.info("radio plan %s: %s %s", op.operation_id, op.state, op.reason or "")
            removed = (op.plan or {}).get("stations_removed")
            if removed and op.state in (State.CONFIG_COMMITTED, State.OBSERVED_APPLIED):
                log.warning(
                    "radio plan: the pod's stations %s deleted (not EMOSA's GTP station, the pod"
                    " wired)",
                    ", ".join(removed),
                )

    def status(self):
        op = self.latest()
        snap = self.backend.last
        config = {} if snap is None else snap.config
        return {
            "unclaimed": config.get("unclaimed"),
            "unclaimed_enabled": config.get("unclaimed_enabled"),
            "unclaimed_inet_on": config.get("unclaimed_inet_on"),
            "unclaimed_exposed": config.get("unclaimed_exposed"),
            "operator_cloud": config.get("operator_cloud"),
            "foreign_stations": config.get("foreign_stations"),
            "dark_radios": config.get("dark_radios"),
            "channel": config.get("channel"),
            "wired": config.get("wired"),
            # spec 3.10: the station that holds the radio (its channel), the plan's follow of it
            "station_channel": config.get("station_channel"),
            "follow": None
            if self.follow is None
            else {"channel": self.follow[0], "ht_mode": self.follow[1]},
            "follows": self.follow_count,
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
