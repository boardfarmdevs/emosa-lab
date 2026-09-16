from dataclasses import dataclass
from typing import Protocol

from emosa.model import Intent, Observation


@dataclass
class Snapshot:
    config: dict
    observed: Observation
    ready: bool
    generation: int
    schema_fingerprint: str


@dataclass
class SubmitResult:
    status: str
    evidence: dict
    reason: str | None = None


class Backend(Protocol):
    mode: str

    async def snapshot(self) -> Snapshot: ...
    async def plan(self, intent: Intent) -> dict: ...
    async def submit(self, intent: Intent, attempt: dict) -> SubmitResult: ...
    async def close(self): ...
