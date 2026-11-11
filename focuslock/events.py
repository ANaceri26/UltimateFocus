import contextlib
import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class EventType:
    SESSION_STARTED = "session_started"
    SESSION_RESUMED = "session_resumed"
    SESSION_ENDED = "session_ended"
    BREAK_SUGGESTED = "break_suggested"
    BREAK_STARTED = "break_started"
    BREAK_ENDED = "break_ended"
    REWARD_SPENT = "reward_spent"
    REWARD_WINDOW_ENDED = "reward_window_ended"
    HOSTS_UPDATED = "hosts_updated"
    DOMAIN_ADDED = "domain_added"
    DOMAIN_REMOVED = "domain_removed"
    CHEAT_RECORDED = "cheat_recorded"


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


Handler = Callable[[Event], None]


class EventBus:
    """Publish/subscribe (Observer pattern).

    The session engine announces what happened without knowing who listens: the event log,
    the GUI and the CLI each subscribe on their own. A failing listener is logged and
    skipped so it cannot break the others (fault isolation).
    """

    WILDCARD = "*"

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._lock = threading.Lock()
        self.errors = 0

    def subscribe(self, event_type: str, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event_type: str, **data: Any) -> Event:
        event = Event(event_type, data)
        with self._lock:
            handlers = [
                *self._handlers.get(event_type, ()),
                *self._handlers.get(self.WILDCARD, ()),
            ]
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                self.errors += 1
                logger.exception("event handler failed for %s", event_type)
        return event
