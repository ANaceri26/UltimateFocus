import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...


class SystemClock:
    """monotonic() measures durations (never jumps); now() stamps records (UTC wall clock)."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock tests can move by hand, so a 90-minute session takes microseconds to test."""

    def __init__(self, start: datetime | None = None) -> None:
        self._mono = 1000.0
        self._wall = start or datetime(2026, 1, 5, 12, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self._mono

    def now(self) -> datetime:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._wall += timedelta(seconds=seconds)
