from enum import StrEnum


class Reason(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    NOT_READY = "NOT_READY"
    OWNERSHIP_CONFLICT = "OWNERSHIP_CONFLICT"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    APPLY_TIMEOUT = "APPLY_TIMEOUT"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    MISSING_PREREQUISITE = "MISSING_PREREQUISITE"
    BUSY = "BUSY"
    NOT_FOUND = "NOT_FOUND"


class EmosaError(Exception):
    def __init__(self, code: Reason, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def public(self):
        return {"code": self.code.value, "message": str(self), "details": self.details}
