import time
from datetime import UTC, datetime, timedelta


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utc(self) -> str:
        return utc_now()


class ManualClock(Clock):
    def __init__(self):
        self.elapsed = 0.0
        self.origin = datetime(2026, 1, 1, tzinfo=UTC)

    def advance(self, seconds: float):
        if seconds < 0:
            raise ValueError("clock cannot move backwards")
        self.elapsed += seconds

    def monotonic(self) -> float:
        return self.elapsed

    def utc(self) -> str:
        return (self.origin + timedelta(seconds=self.elapsed)).isoformat().replace("+00:00", "Z")
