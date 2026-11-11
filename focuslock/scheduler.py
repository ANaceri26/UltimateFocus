import heapq
import itertools
import logging
import threading
from collections.abc import Callable
from typing import Any

from .clock import Clock

logger = logging.getLogger(__name__)


class Scheduler:
    """Run callbacks at a future time, using a binary min-heap as a priority queue.

    heap[0] is always the job due soonest: push and pop are O(log n), peeking is O(1).
    The sequence number breaks ties so jobs due at the same instant run in FIFO order.
    Cancelling just forgets the id ("lazy deletion"); the stale heap entry is skipped
    when it surfaces, which avoids an O(n) search through the heap.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._heap: list[tuple[float, int, Callable[..., Any], tuple[Any, ...]]] = []
        self._counter = itertools.count(1)
        self._pending: set[int] = set()
        self._lock = threading.Lock()

    def call_later(self, delay: float, callback: Callable[..., Any], *args: Any) -> int:
        job_id = next(self._counter)
        with self._lock:
            heapq.heappush(
                self._heap, (self._clock.monotonic() + max(0.0, delay), job_id, callback, args)
            )
            self._pending.add(job_id)
        return job_id

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            if job_id in self._pending:
                self._pending.discard(job_id)
                return True
            return False

    def run_due(self) -> int:
        ran = 0
        while True:
            with self._lock:
                if not self._heap or self._heap[0][0] > self._clock.monotonic():
                    return ran
                _, job_id, callback, args = heapq.heappop(self._heap)
                if job_id not in self._pending:
                    continue
                self._pending.discard(job_id)
            try:
                callback(*args)
            except Exception:
                logger.exception("scheduled job %s failed", job_id)
            ran += 1

    def next_due_in(self) -> float | None:
        with self._lock:
            while self._heap and self._heap[0][1] not in self._pending:
                heapq.heappop(self._heap)
            if not self._heap:
                return None
            return max(0.0, self._heap[0][0] - self._clock.monotonic())

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)
