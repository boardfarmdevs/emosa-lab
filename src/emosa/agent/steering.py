"""The agent carries out the controller's client steering mandates on its pod.

One ``ClientSteering`` per agent, with its own operation journal (the steering
scope, ``emosa.opensync.steering``). One mandate at a time: a request while a
window is open is refused (``busy``), like a native agent that cannot take it.

1. ``start`` (from the Client Steering Request handler) journals the window as
   an operation. The Ack has already gone out.
2. ``tick`` opens it: one guarded transaction. It is applied when ``owm``
   reports ``cs_state`` ``steering`` for the station, within ``APPLY``.
3. Then the directed kick: ``owm`` sends the BTM request naming the target, and
   deauthenticates a station that stays (``btm_deauth``).
4. The window closes, and the rows it created are deleted, when ``owm`` stops
   steering (``cs_state`` leaves ``steering``) or at the latest after the
   requested window. Without disassociation imminent the controller asked the
   station to be left where it is if it declines: the window then closes
   ``GENTLE`` seconds after the kick, before ``owm``'s deauthentication (10 s
   after its BTM request, for a station that supports BTM).

Whether the station moved is observed as its association on the source BSS.
EMOSA sends no BTM Report: the pod does not expose the station's BTM status.
"""

import logging

from emosa.errors import EmosaError, Reason
from emosa.model import State
from emosa.opensync.steering import WINDOW, SteeringIntent
from emosa.operations import transition
from emosa.reconcile import Engine

log = logging.getLogger("emosa.agent.steering")
SOURCE = "steering-request"
APPLY = 10  # owm takes the row within a second or two (seen live)
GENTLE = 8  # < owm's 10 s deauthentication delay after a BTM request
MARGIN = 5
TERMINAL = (
    State.REJECTED,
    State.FAILED,
    State.OWNERSHIP_CONFLICT,
    State.TIMED_OUT,
)


def window_seconds(request):
    """The pod's enforcement period for a request (EasyMesh gives seconds)."""
    return min(max(request.window, WINDOW[0], 15), WINDOW[1])


class ClientSteering:
    def __init__(self, pod_id, backend, store, vault, *, run_id, clock=None):
        self.pod_id, self.backend, self.store, self.run_id = pod_id, backend, store, run_id
        self.engine = Engine(store, vault, {pod_id: backend}, clock, intent_type=SteeringIntent)
        self.clock = self.engine.clock
        self.engine.recover()
        self.job = None
        self.counts = {}
        self.history = []
        # Windows a previous process left open are closed once the pod is reachable
        # (the latest few: older ones are gone with their OpenSync start).
        closed = self._closed()
        self.leftover = [
            op
            for op in store.operations()[-8:]
            if op.operation_id not in closed
            and op.state
            in (
                State.REQUESTED,
                State.CONFIG_COMMITTED,
                State.OBSERVED_APPLIED,
                State.INDETERMINATE,
            )
        ]

    def _closed(self):
        """Operations whose window the journal records as closed."""
        closed, after = set(), 0
        while True:
            events = self.store.events(self.run_id, after=after, limit=500)
            if not events:
                return closed
            closed |= {
                e["operation_id"] for e in events if "window_closed" in (e.get("payload") or {})
            }
            after = events[-1]["sequence"]

    def _record(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1
        return event

    def start(self, request, mid):
        """A mandate for one station and one target (already checked on the wire side)."""
        if self.job is not None:
            return "busy"
        target_bssid, op_class, channel = request.targets[0]
        intent = SteeringIntent(
            self.pod_id,
            request.stations[0].hex(":"),
            request.source_bssid.hex(":"),
            target_bssid.hex(":"),
            op_class,
            channel,
            request.disassoc_imminent,
            window_seconds(request),
            mid,
        )
        try:
            op = self.engine.request(
                intent,
                source=SOURCE,
                key=f"{mid}:{intent.station}:{self.clock.utc()}",
                run_id=self.run_id,
                deadline=APPLY,
            )
        except EmosaError as exc:
            return f"invalid_{exc.code}"
        if op.state != State.REQUESTED:
            return f"{op.state}_{op.reason or ''}".lower().rstrip("_")
        self.job = {"op": op.operation_id, "intent": intent, "phase": "requested"}
        self._record("started")
        return None

    def _finish(self, outcome):
        job, self.job = self.job, None
        self._record(outcome)
        entry = {
            "station": job["intent"].station,
            "target": job["intent"].target_bssid,
            "outcome": outcome,
            "operation_id": job["op"],
        }
        self.history = [*self.history[-7:], entry]
        log.info("client steering %s -> %s: %s", entry["station"], entry["target"], outcome)

    def _end(self, op, outcome):
        """A closed window's operation leaves the active states (else: busy for ever)."""
        if op.state == State.REQUESTED:
            op = self.engine.cancel(op.operation_id)
        elif op.state in (State.CONFIG_COMMITTED, State.INDETERMINATE):
            op.original_outcome, op.deadline_elapsed = op.state, True
            transition(op, State.TIMED_OUT)
            op.reason = op.reason or Reason.APPLY_TIMEOUT
        self.engine._save(op, {"window_closed": outcome})

    async def _close(self, op, intent):
        created = (op.commit_evidence or {}).get("created")
        try:
            if created is None:
                # Outcome unknown: remove only a client row this window would have written.
                snap = await self.backend.snapshot()
                if snap.config.get(intent.station, "").split("/")[0] != intent.target_bssid:
                    return
                raw = await self.backend.session.snapshot()
                decoded, _ = self.backend._binding(raw)
                rows = [
                    u
                    for u, r in decoded.get("Band_Steering_Clients", {}).items()
                    if r.get("mac") == intent.station
                ]
                created = {"client": rows[0]} if len(rows) == 1 else {}
            await self.backend.close(intent, created)
        except (EmosaError, ConnectionError, TimeoutError) as exc:
            log.warning("client steering close: %s", exc)
            self._record("close_failed")

    async def tick(self):
        if self.leftover:
            try:
                reachable = (await self.backend.snapshot()).ready
            except (EmosaError, ConnectionError, TimeoutError):
                reachable = False
            if not reachable:
                return None  # not while the pod is away: every close would wait out its timeout
        while self.leftover:
            op = self.leftover.pop()
            if op.state != State.REQUESTED:
                await self._close(op, SteeringIntent(**op.intent))
            self._end(op, "closed_after_restart")
        if self.job is None:
            return
        job = self.job
        op = self.store.get(job["op"])
        intent = job["intent"]
        if job["phase"] == "requested":
            op = await self.engine.execute(op.operation_id)
            if op.state in TERMINAL:
                return self._finish(f"refused_{(op.reason or op.state).lower()}")
            if op.state == State.INDETERMINATE:
                await self._close(op, intent)
                self._end(op, "outcome_unknown")
                return self._finish("outcome_unknown")
            job["phase"] = "opening"
        now = self.clock.monotonic()
        try:
            cs_state, on_source = await self.backend.observe(intent.station, intent.source_bssid)
        except (EmosaError, ConnectionError, TimeoutError):
            cs_state, on_source = None, None
        if job["phase"] == "opening":
            snap = await self.backend.snapshot() if cs_state == "steering" else None
            if snap is not None and snap.ready and snap.observed.satisfies(intent.target()):
                evidence = self.engine._evidence(snap, instance=self.backend.instance)
                transition(op, State.OBSERVED_APPLIED, evidence=evidence)
                op.application_evidence = evidence
                self.engine._save(op)
                try:
                    await self.backend.kick(intent.station, op.commit_evidence["created"]["client"])
                except (EmosaError, ConnectionError, TimeoutError) as exc:
                    log.warning("client steering kick: %s", exc)
                    await self._close(op, intent)
                    self._end(op, "kick_failed")
                    return self._finish("kick_failed")
                job["phase"], job["kicked"] = "kicked", now
                self._record("kicked")
            elif self.engine._expired(op):
                op.original_outcome, op.deadline_elapsed = op.state, True
                transition(op, State.TIMED_OUT)
                op.reason = Reason.APPLY_TIMEOUT
                self.engine._save(op)
                await self._close(op, intent)
                self._end(op, "not_applied")
                return self._finish("not_applied")
            return None
        limit = GENTLE if not intent.disassoc_imminent else intent.window + MARGIN
        if cs_state == "steering" and on_source is not False and now < job["kicked"] + limit:
            return None
        await self._close(op, intent)
        outcome = "left_source" if on_source is False else "stayed" if on_source else "unobserved"
        self._end(op, outcome)
        return self._finish(outcome)

    def status(self):
        job = self.job
        return {
            "counts": dict(self.counts),
            "active": None
            if job is None
            else {
                "station": job["intent"].station,
                "target": job["intent"].target_bssid,
                "phase": job["phase"],
                "operation_id": job["op"],
            },
            "history": list(self.history),
        }
