"""All lifecycle transitions pass these guards before any asynchronous side effect."""

from emosa.errors import EmosaError, Reason
from emosa.model import Operation, State

TRANSITIONS = {
    State.REQUESTED: {State.VALIDATED, State.REJECTED, State.CANCELLED},
    State.VALIDATED: {
        State.SUBMITTED,
        State.REJECTED,
        State.OWNERSHIP_CONFLICT,
        State.CANCELLED,
        State.OBSERVED_APPLIED,
    },
    State.SUBMITTED: {
        State.CONFIG_COMMITTED,
        State.INDETERMINATE,
        State.FAILED,
        State.OWNERSHIP_CONFLICT,
    },
    State.CONFIG_COMMITTED: {
        State.OBSERVED_APPLIED,
        State.FAILED,
        State.TIMED_OUT,
        State.OWNERSHIP_CONFLICT,
    },
    State.INDETERMINATE: {
        State.CONFIG_COMMITTED,
        State.OBSERVED_APPLIED,
        State.OWNERSHIP_CONFLICT,
        State.FAILED,
        State.TIMED_OUT,
    },
}


def transition(op: Operation, target: State, *, evidence: dict | None = None):
    evidence = evidence or {}
    if target not in TRANSITIONS.get(op.state, set()):
        raise EmosaError(Reason.PRECONDITION_FAILED, f"invalid transition {op.state} → {target}")
    if target == State.SUBMITTED and not op.attempts:
        raise EmosaError(Reason.PRECONDITION_FAILED, "durable attempt association required")
    if target == State.CONFIG_COMMITTED and not evidence.get("transaction_validated"):
        raise EmosaError(Reason.PRECONDITION_FAILED, "validated transaction results required")
    if target == State.OBSERVED_APPLIED:
        if not (evidence.get("fresh") and evidence.get("predicate_satisfied")):
            raise EmosaError(Reason.PRECONDITION_FAILED, "fresh application evidence required")
        if op.state == State.VALIDATED and not evidence.get("observed_noop"):
            raise EmosaError(Reason.PRECONDITION_FAILED, "no-op needs observed evidence")
    if target == State.TIMED_OUT and not op.deadline_elapsed:
        raise EmosaError(Reason.PRECONDITION_FAILED, "application deadline has not elapsed")
    op.state = target
