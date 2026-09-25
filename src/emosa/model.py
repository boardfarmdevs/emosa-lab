from dataclasses import asdict, dataclass, field
from enum import StrEnum

from emosa.errors import EmosaError, Reason


class State(StrEnum):
    REQUESTED = "REQUESTED"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    CONFIG_COMMITTED = "CONFIG_COMMITTED"
    OBSERVED_APPLIED = "OBSERVED_APPLIED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    INDETERMINATE = "INDETERMINATE"
    OWNERSHIP_CONFLICT = "OWNERSHIP_CONFLICT"
    CANCELLED = "CANCELLED"


ACTIVE = {
    State.REQUESTED,
    State.VALIDATED,
    State.SUBMITTED,
    State.CONFIG_COMMITTED,
    State.INDETERMINATE,
}


@dataclass(frozen=True)
class Intent:
    pod_id: str
    radio_id: str
    bss_id: str
    ssid: str
    secret_ref: str
    enabled: bool = True
    security_mode: str = "wpa2-psk"
    # Further BSSes of the same radio, configured together with this one (one M2
    # set): ({"role": "fronthaul"|"backhaul", "ssid", "secret_ref"}, ...). None
    # means a single-BSS intent that says nothing about other BSSes; an empty
    # tuple means the radio has no other managed BSS.
    additional: tuple | None = None

    def record(self):
        """The persisted form; an absent `additional` is omitted (legacy records)."""
        value = asdict(self)
        if self.additional is None:
            del value["additional"]
        else:
            value["additional"] = [dict(b) for b in self.additional]
        return value

    def validate(self):
        if not 1 <= len(self.ssid.encode("utf-8")) <= 32 or "\x00" in self.ssid:
            raise EmosaError(Reason.INVALID_INPUT, "SSID must contain 1–32 UTF-8 bytes, no NUL")
        if self.additional is not None:
            if not isinstance(self.additional, (tuple, list)) or len(self.additional) > 7:
                raise EmosaError(Reason.INVALID_INPUT, "at most seven additional BSSes")
            for bss in self.additional:
                if (
                    not isinstance(bss, dict)
                    or set(bss) != {"role", "ssid", "secret_ref"}
                    or bss["role"] not in ("fronthaul", "backhaul")
                    or not isinstance(bss["ssid"], str)
                    or not 1 <= len(bss["ssid"].encode("utf-8")) <= 32
                    or "\x00" in bss["ssid"]
                    or not isinstance(bss["secret_ref"], str)
                    or not bss["secret_ref"]
                ):
                    raise EmosaError(Reason.INVALID_INPUT, "invalid additional BSS")
        if self.security_mode != "wpa2-psk" or self.enabled is not True:
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "only enabled existing WPA2-PSK APs")
        if any(
            not isinstance(x, str) or not x
            for x in (self.pod_id, self.radio_id, self.bss_id, self.secret_ref)
        ):
            raise EmosaError(
                Reason.INVALID_INPUT, "explicit resource and secret references required"
            )

    def target(self, vault):
        self.validate()
        return {
            "ssid": self.ssid,
            "enabled": self.enabled,
            "mode": "ap",
            "security_mode": self.security_mode,
            "credential_fingerprint": vault.fingerprint(vault.resolve(self.secret_ref)),
        } | (
            {}
            if self.additional is None
            else {
                "additional": sorted(
                    [b["role"], b["ssid"], vault.fingerprint(vault.resolve(b["secret_ref"]))]
                    for b in self.additional
                )
            }
        )


@dataclass
class Observation:
    pod_id: str
    resource_id: str
    values: dict
    source: str
    backend_mode: str
    session_generation: int
    received_at: str
    fresh: bool
    provenance: str
    source_time: str | None = None
    revision: int = 0

    def satisfies(self, target: dict) -> bool:
        return self.fresh and all(
            k in self.values and self.values[k] == v for k, v in target.items()
        )


@dataclass
class Operation:
    operation_id: str
    run_id: str
    request_source: str
    initiating_interface: str
    intent: dict
    intent_fingerprint: str
    idempotency_key: str
    created_at: str
    updated_at: str
    deadline_at: str
    schema_version: int = 1
    state: State = State.REQUESTED
    reason: str | None = None
    mapping_version: str = "synthetic-existing-bss-v1"
    schema_fingerprint: str | None = None
    attempts: list[dict] = field(default_factory=list)
    commit_evidence: dict = field(default_factory=lambda: {"attribution": "not_submitted"})
    application_predicate: str = "fresh-qualified-existing-ap-ssid-security-v1"
    application_evidence: dict | None = None
    changed: bool | None = None
    deadline_elapsed: bool = False
    original_outcome: str | None = None
    late_resolution: str | None = None
    client_verification: dict = field(default_factory=lambda: {"verdict": "unknown"})
    blocked_for_resubmission: bool = False
    plan: dict | None = None
    timings: dict = field(default_factory=dict)

    @property
    def pod_id(self):
        return self.intent["pod_id"]

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "state": State(value["state"])})
