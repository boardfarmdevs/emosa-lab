import asyncio
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta

from emosa.clock import Clock
from emosa.errors import EmosaError, Reason
from emosa.model import ACTIVE, Intent, Operation, State
from emosa.operations import transition


class Engine:
    def __init__(self, store, vault, backends, clock=None):
        self.store, self.vault, self.backends = store, vault, backends
        self.clock = clock or Clock()
        self.deadlines = {}
        self.busy = set()
        self.quiesced = False
        self.starts = {}
        self.last_observations = {}
        self.wsc_guards = {}  # Process-local authority; deliberately never recovered.

    def _save(self, op, payload=None):
        op.updated_at = self.clock.utc()
        if op.operation_id in self.starts and op.state not in op.timings:
            op.timings[op.state] = {
                "seconds_since_request": self.clock.monotonic() - self.starts[op.operation_id],
                "process_clock_id": self.store.process_id,
            }
        self.store.save(op, payload)

    def request(
        self, intent: Intent, *, source, key, run_id, deadline=30, initiating_interface="semantic"
    ):
        if initiating_interface != "semantic":
            raise EmosaError(Reason.MISSING_PREREQUISITE, "P0 wire binding is not implemented")
        return self._request(
            intent,
            source=source,
            key=key,
            run_id=run_id,
            deadline=deadline,
            initiating_interface=initiating_interface,
        )

    def _request(
        self, intent, *, source, key, run_id, deadline=30, initiating_interface, wsc_receipt=None
    ):
        """Internal journal handoff, also used by the owned WSC component lab.

        The public request/serve API cannot select that component path. A receipt
        records authenticated input correlation; it is not full profile admission.
        """
        intent.validate()
        if initiating_interface != "semantic" and (
            initiating_interface != "wsc-component" or wsc_receipt is None
        ):
            raise EmosaError(Reason.MISSING_PREREQUISITE, "missing component WSC receipt")
        if not source or not key or len(key) > 128 or not 0 < deadline <= 3600:
            raise EmosaError(Reason.INVALID_INPUT, "invalid source, idempotency key or deadline")
        if intent.pod_id not in self.backends:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "pod outside configured allowlist")
        fingerprint = self.vault.fingerprint(
            {**intent.record(), "target": intent.target(self.vault)}
        )
        old = self.store.lookup(source, intent.pod_id, key)
        if old:
            if old.intent_fingerprint != fingerprint:
                raise EmosaError(
                    Reason.INVALID_INPUT, "idempotency key reused for different intent"
                )
            return old
        now = self.clock.utc()
        deadline_at = (datetime.fromisoformat(now) + timedelta(seconds=deadline)).isoformat()
        op = Operation(
            str(uuid.uuid4()),
            run_id,
            source,
            initiating_interface,
            intent.record(),
            fingerprint,
            key,
            now,
            now,
            deadline_at,
        )
        self.store.add(op, wsc_receipt=wsc_receipt)
        self.starts[op.operation_id] = self.clock.monotonic()
        self.deadlines[op.operation_id] = self.clock.monotonic() + deadline
        if (
            self.quiesced
            or intent.pod_id in self.busy
            or any(
                p.pod_id == intent.pod_id
                and p.state in ACTIVE
                and p.operation_id != op.operation_id
                for p in self.store.operations()
            )
        ):
            transition(op, State.REJECTED)
            op.reason = Reason.BUSY
            self._save(op)
        elif self.store.ownership(intent.pod_id):
            transition(op, State.REJECTED)
            op.reason = Reason.OWNERSHIP_CONFLICT
            self._save(op)
        return op

    async def plan(self, intent):
        if intent.pod_id not in self.backends:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "unknown pod")
        if self.store.ownership(intent.pod_id):
            raise EmosaError(Reason.OWNERSHIP_CONFLICT, "ownership must be re-qualified")
        return await self.backends[intent.pod_id].plan(intent)

    async def execute(self, operation_id):
        op = self.store.get(operation_id)
        if op.state != State.REQUESTED:
            return op
        if op.pod_id in self.busy:
            raise EmosaError(Reason.BUSY, "pod already executing")
        self.busy.add(op.pod_id)
        try:
            backend = self.backends[op.pod_id]
            intent = Intent(**op.intent)
            try:
                await self._check_wsc(op)
                op.plan = await self.plan(intent)
                snap = await backend.snapshot()
                if not snap.ready:
                    raise EmosaError(Reason.NOT_READY, "fresh complete snapshot required")
                await self._check_wsc(op)
                # A CLI cancellation may have happened while validation awaited I/O.
                if self.store.get(operation_id).state != State.REQUESTED:
                    return self.store.get(operation_id)
                op.schema_fingerprint = snap.schema_fingerprint
                transition(op, State.VALIDATED)
                self._save(op, {"plan": op.plan})
            except EmosaError as exc:
                transition(op, State.REJECTED)
                op.reason = exc.code
                self._save(op)
                return op
            target = intent.target(self.vault)
            if snap.observed.satisfies(target) and self._matches(snap.config, target):
                evidence = self._evidence(snap, observed_noop=True)
                transition(op, State.OBSERVED_APPLIED, evidence=evidence)
                op.changed, op.application_evidence = False, evidence
                self._save(op)
                return op
            attempt = {
                "attempt_id": str(uuid.uuid4()),
                "transaction_id": str(uuid.uuid4()),
                "session_generation": snap.generation,
                "prepared_at": self.clock.utc(),
            }
            op.attempts.append(attempt)
            transition(op, State.SUBMITTED)
            op.commit_evidence = {"attribution": "unknown"}
            self._save(op, {"attempt": attempt})  # FULL SQLite commit before send.
            try:
                result = await backend.submit(intent, attempt)
            except (EmosaError, ConnectionError, TimeoutError):
                transition(op, State.INDETERMINATE)
                op.reason = Reason.OUTCOME_UNKNOWN
            else:
                if result.status == "committed":
                    transition(op, State.CONFIG_COMMITTED, evidence=result.evidence)
                    op.commit_evidence, op.changed = result.evidence, True
                elif result.status == "conflict":
                    transition(op, State.OWNERSHIP_CONFLICT)
                    op.reason = Reason.OWNERSHIP_CONFLICT
                    self.store.conflict(
                        op.pod_id, {"operation_id": op.operation_id, "reason": result.reason}
                    )
                elif result.status == "rejected":
                    transition(op, State.FAILED)
                    op.reason = result.reason
                else:
                    transition(op, State.INDETERMINATE)
                    op.reason = Reason.OUTCOME_UNKNOWN
            self._save(op)
            return op
        finally:
            self.busy.discard(op.pod_id)
            if self.store.get(operation_id).state != State.REQUESTED:
                self.wsc_guards.pop(operation_id, None)

    async def _check_wsc(self, op):
        if op.initiating_interface != "wsc-component":
            return
        receipt = self.store.wsc_receipt(op.operation_id)
        guard = self.wsc_guards.get(op.operation_id)
        if receipt is None or receipt["process_id"] != self.store.process_id or guard is None:
            raise EmosaError(Reason.NOT_READY, "WSC exchange authority is no longer live")
        await guard()

    @staticmethod
    def _matches(values, target):
        return all(values.get(k) == v for k, v in target.items())

    @staticmethod
    def _evidence(snap, **extra):
        return {
            "fresh": snap.observed.fresh,
            "predicate_satisfied": True,
            "generation": snap.generation,
            "revision": snap.observed.revision,
            "received_at": snap.observed.received_at,
            "source": snap.observed.source,
            "provenance": snap.observed.provenance,
            **extra,
        }

    def _expired(self, op):
        if op.operation_id not in self.deadlines:
            # A new process establishes a new monotonic budget from persisted UTC.
            remaining = max(
                0,
                (
                    datetime.fromisoformat(op.deadline_at)
                    - datetime.fromisoformat(self.clock.utc())
                ).total_seconds(),
            )
            self.deadlines[op.operation_id] = self.clock.monotonic() + remaining
        return self.clock.monotonic() >= self.deadlines[op.operation_id]

    async def reconcile(self, pod_id):
        snap = await self.backends[pod_id].snapshot()
        all_ops = self.store.operations()
        for index, op in enumerate(all_ops):
            if op.pod_id != pod_id or op.state not in {
                State.CONFIG_COMMITTED,
                State.INDETERMINATE,
                State.TIMED_OUT,
                State.OBSERVED_APPLIED,
            }:
                continue
            # Only the most recent legitimate intent owns this scope.
            newer = any(
                p.pod_id == pod_id
                and p.state
                in {
                    State.SUBMITTED,
                    State.CONFIG_COMMITTED,
                    State.OBSERVED_APPLIED,
                    State.INDETERMINATE,
                    State.TIMED_OUT,
                    State.OWNERSHIP_CONFLICT,
                }
                for p in all_ops[index + 1 :]
            )
            if newer:
                continue
            expired = self._expired(op)
            changed = False
            if expired and op.state in {State.CONFIG_COMMITTED, State.INDETERMINATE}:
                if not op.deadline_elapsed:
                    op.original_outcome = op.state
                    op.deadline_elapsed = True
                    changed = True
                if op.state == State.CONFIG_COMMITTED:
                    transition(op, State.TIMED_OUT)
                    op.reason = Reason.APPLY_TIMEOUT
            try:
                target = Intent(**op.intent).target(self.vault)
            except EmosaError:
                if not op.blocked_for_resubmission:
                    op.blocked_for_resubmission = True
                    changed = True
            else:
                signature = json.dumps(
                    {
                        "values": snap.observed.values,
                        "config": snap.config,
                        "generation": snap.generation,
                        "fresh": snap.observed.fresh,
                    },
                    sort_keys=True,
                )
                if self.last_observations.get(op.operation_id) != signature:
                    self.last_observations[op.operation_id] = signature
                    self.store.event(
                        op.run_id,
                        "OBSERVATION",
                        {
                            "operation_id": op.operation_id,
                            "observation": asdict(snap.observed),
                            "config": snap.config,
                            "desired_observed_diff": {
                                key: {"desired": value, "observed": snap.observed.values.get(key)}
                                for key, value in target.items()
                                if snap.observed.values.get(key) != value
                            },
                        },
                        pod_id,
                    )
                if (
                    snap.ready
                    and op.state == State.INDETERMINATE
                    and op.deadline_elapsed
                    and not self._matches(snap.config, target)
                ):
                    # The current configuration, past the deadline, lacks the
                    # write (it never landed, or a pod restart dropped it). Stop
                    # blocking the pod; a late application is still recorded.
                    transition(op, State.TIMED_OUT)
                    op.reason = Reason.APPLY_TIMEOUT
                    changed = True
                if snap.ready and snap.observed.fresh:
                    config_matches = self._matches(snap.config, target)
                    applied = snap.observed.satisfies(target)
                    if not config_matches and op.state in {
                        State.CONFIG_COMMITTED,
                        State.OBSERVED_APPLIED,
                        State.TIMED_OUT,
                    }:
                        self.store.conflict(
                            pod_id,
                            {
                                "operation_id": op.operation_id,
                                "reason": "owned configuration changed",
                            },
                        )
                        if op.state == State.CONFIG_COMMITTED:
                            transition(op, State.OWNERSHIP_CONFLICT)
                        if op.reason != Reason.OWNERSHIP_CONFLICT:
                            op.reason = Reason.OWNERSHIP_CONFLICT
                            changed = True
                    elif config_matches and applied and op.application_evidence is None:
                        op.application_evidence = self._evidence(snap)
                        if op.deadline_elapsed:
                            op.late_resolution = "applied_after_deadline"
                        if op.state != State.TIMED_OUT:
                            transition(op, State.OBSERVED_APPLIED, evidence=op.application_evidence)
                        if op.commit_evidence["attribution"] == "unknown":
                            op.application_evidence["attribution"] = "current_condition_only"
                        else:
                            op.reason = None if not op.deadline_elapsed else Reason.APPLY_TIMEOUT
                        changed = True
            if changed:
                self._save(op, {"observation": asdict(snap.observed)})
        return snap

    def recover(self):
        for op in self.store.operations():
            if op.initiating_interface == "wsc-component" and op.state == State.REQUESTED:
                # The WSC transcript and link authority do not survive restart.
                # Never let the ordinary REQUESTED scheduler apply an unsent
                # controller request from an earlier process's exchange.
                transition(op, State.CANCELLED)
                self._save(op, {"recovery": "unsent WSC component request; new exchange required"})
            elif op.state == State.SUBMITTED:
                transition(op, State.INDETERMINATE)
                op.commit_evidence = {"attribution": "unknown"}
                op.reason = Reason.OUTCOME_UNKNOWN
                self._save(op, {"recovery": "possibly sent; observation required before action"})
            elif op.state == State.VALIDATED:
                transition(op, State.CANCELLED)
                self._save(op, {"recovery": "unsent intent; explicit new request required"})

    def cancel(self, operation_id):
        op = self.store.get(operation_id)
        transition(op, State.CANCELLED)
        self._save(op)
        return op

    async def wait(self, operation_id, timeout=10):
        end = asyncio.get_running_loop().time() + min(max(timeout, 0), 60)
        while True:
            op = self.store.get(operation_id)
            if op.state not in ACTIVE:
                return op
            if asyncio.get_running_loop().time() >= end:
                raise EmosaError(
                    Reason.NOT_READY,
                    "caller wait expired",
                    operation_id=operation_id,
                    wait_timeout=True,
                )
            await asyncio.sleep(0.02)
