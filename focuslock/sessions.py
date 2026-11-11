from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .clock import Clock
from .db import Database
from .errors import InvalidTransitionError
from .events import EventBus, EventType


class State(StrEnum):
    IDLE = "idle"
    FOCUSING = "focusing"
    ON_BREAK = "on_break"


class Mode(StrEnum):
    TIMER = "timer"
    FLOW = "flow"
    GOAL = "goal"


@dataclass(frozen=True)
class ModePolicy:
    """How a mode behaves (the Strategy pattern: same engine, swappable rules)."""

    forced_breaks: bool
    timed: bool
    stop_counts_as_complete: bool


POLICIES = {
    Mode.TIMER: ModePolicy(forced_breaks=True, timed=True, stop_counts_as_complete=False),
    Mode.FLOW: ModePolicy(forced_breaks=False, timed=False, stop_counts_as_complete=True),
    Mode.GOAL: ModePolicy(forced_breaks=True, timed=False, stop_counts_as_complete=False),
}

TRANSITIONS: dict[tuple[State, str], State] = {
    (State.IDLE, "start"): State.FOCUSING,
    (State.IDLE, "resume"): State.FOCUSING,
    (State.FOCUSING, "break"): State.ON_BREAK,
    (State.ON_BREAK, "end_break"): State.FOCUSING,
    (State.FOCUSING, "finish"): State.IDLE,
    (State.ON_BREAK, "finish"): State.IDLE,
    (State.FOCUSING, "suspend"): State.IDLE,
    (State.ON_BREAK, "suspend"): State.IDLE,
}

PERSIST_EVERY_SECONDS = 5.0


@dataclass(frozen=True)
class EngineConfig:
    break_interval_seconds: int
    break_seconds: int
    strict: bool = False


class FocusEngine:
    """A finite state machine for one focus session at a time.

    TRANSITIONS lists every legal (state, action) pair; anything else raises
    InvalidTransitionError. There is deliberately no pause: once started, a session runs
    until it completes or is stopped (stopping a timer/goal session early needs the
    password, see app.py). If FocusLock closes mid-session, suspend() saves progress and
    leaves the session 'active' in the database; resume_from() continues it later.

    The engine has no threads and never sleeps: something outside calls tick() about once
    a second, and the clock is injected, which makes every behaviour testable instantly.
    """

    def __init__(
        self,
        db: Database,
        bus: EventBus,
        clock: Clock,
        config: EngineConfig,
        reward_for: Callable[[int], int],
    ) -> None:
        self._db = db
        self._bus = bus
        self._clock = clock
        self._config = config
        self._reward_for = reward_for
        self._last = clock.monotonic()
        self._reset()

    def _reset(self) -> None:
        self.state = State.IDLE
        self.session_id: int | None = None
        self.mode: Mode | None = None
        self.goal: str | None = None
        self.planned_seconds: int | None = None
        self.focused = 0.0
        self.since_break = 0.0
        self._break_id: int | None = None
        self._break_started: float | None = None
        self._break_forced = False
        self._suggested = False
        self._last_persist = self._clock.monotonic()

    @property
    def config(self) -> EngineConfig:
        return self._config

    @property
    def stop_is_early(self) -> bool:
        """Stopping now would cut the session short (timer or goal, not flow)."""
        return self.mode is not None and not POLICIES[self.mode].stop_counts_as_complete

    def _require(self, action: str) -> State:
        target = TRANSITIONS.get((self.state, action))
        if target is None:
            verb = action.replace("_", " ")
            state = self.state.value.replace("_", " ")
            raise InvalidTransitionError(f"cannot {verb} while {state}")
        return target

    def _advance(self) -> None:
        """Count time since the last call, but only while actually focusing."""
        now = self._clock.monotonic()
        elapsed = max(0.0, now - self._last)
        self._last = now
        if self.state is State.FOCUSING:
            self.focused += elapsed
            self.since_break += elapsed
            if self.planned_seconds is not None:
                self.focused = min(self.focused, float(self.planned_seconds))

    def _persist(self) -> None:
        if self.session_id is not None:
            self._db.save_progress(self.session_id, int(self.focused), int(self.since_break))
        self._last_persist = self._clock.monotonic()

    def start(
        self, mode: Mode | str, *, planned_seconds: int | None = None, goal: str | None = None
    ) -> dict[str, Any]:
        self._advance()
        mode = Mode(mode)
        policy = POLICIES[mode]
        if policy.timed:
            if planned_seconds is None or planned_seconds <= 0:
                raise ValueError("timer mode needs a positive duration")
        else:
            planned_seconds = None
        goal = (goal or "").strip() or None
        if mode is Mode.GOAL and goal is None:
            raise ValueError("goal mode needs a goal, e.g. 'finish the lab report'")
        target = self._require("start")
        session_id = self._db.create_session(mode.value, self._clock.now(), planned_seconds, goal)
        self.state = target
        self.session_id, self.mode, self.goal = session_id, mode, goal
        self.planned_seconds = planned_seconds
        self._last_persist = self._clock.monotonic()
        self._bus.publish(
            EventType.SESSION_STARTED,
            session_id=session_id,
            mode=mode.value,
            planned_seconds=planned_seconds or 0,
            goal=goal or "",
        )
        return self.snapshot()

    def resume_from(self, row: dict[str, Any]) -> dict[str, Any]:
        """Continue a session saved by suspend() (or by a crash) with the time it had left.

        An interrupted break is not resumed: since_break is restored, so if a break was
        due, the next tick() starts a fresh one.
        """
        self._advance()
        target = self._require("resume")
        self._db.close_open_breaks(int(row["id"]), self._clock.now())
        self.state = target
        self.session_id = int(row["id"])
        self.mode = Mode(row["mode"])
        self.goal = row["goal"]
        self.planned_seconds = row["planned_seconds"]
        self.focused = float(row["focused_seconds"])
        self.since_break = float(row["since_break_seconds"])
        self._last_persist = self._clock.monotonic()
        self._bus.publish(
            EventType.SESSION_RESUMED,
            session_id=self.session_id,
            mode=self.mode.value,
            focused_seconds=int(self.focused),
        )
        return self.snapshot()

    def suspend(self) -> int | None:
        """Save progress and go idle WITHOUT ending the session (FocusLock is closing)."""
        self._advance()
        self._require("suspend")
        session_id = self.session_id
        self._persist()
        self._reset()
        return session_id

    def take_break(self) -> dict[str, Any]:
        self._advance()
        self._begin_break(forced=False)
        return self.snapshot()

    def skip_break(self) -> dict[str, Any]:
        self._advance()
        if self.state is not State.ON_BREAK:
            raise InvalidTransitionError("there is no break to skip")
        if self._break_forced and self._config.strict:
            raise InvalidTransitionError("strict mode is on: this break cannot be skipped")
        self._end_break(skipped=True)
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._advance()
        self._require("finish")
        assert self.mode is not None
        complete = POLICIES[self.mode].stop_counts_as_complete
        return self._finish("completed" if complete else "abandoned")

    def complete_goal(self) -> dict[str, Any]:
        self._advance()
        if self.mode is not Mode.GOAL:
            raise InvalidTransitionError("only goal sessions can be marked done")
        self._require("finish")
        return self._finish("completed")

    def tick(self) -> None:
        self._advance()
        now = self._clock.monotonic()
        if self.state is State.FOCUSING:
            assert self.mode is not None
            if self.planned_seconds is not None and self.focused >= self.planned_seconds:
                self._finish("completed")
                return
            if self.since_break >= self._config.break_interval_seconds:
                if POLICIES[self.mode].forced_breaks:
                    self._begin_break(forced=True)
                    return
                if not self._suggested:
                    self._suggested = True
                    self._bus.publish(
                        EventType.BREAK_SUGGESTED,
                        session_id=self.session_id,
                        focused_seconds=int(self.focused),
                    )
            if now - self._last_persist >= PERSIST_EVERY_SECONDS:
                self._persist()
        elif (
            self.state is State.ON_BREAK
            and self._break_started is not None
            and now - self._break_started >= self._config.break_seconds
        ):
            self._end_break(skipped=False)

    def _begin_break(self, forced: bool) -> None:
        target = self._require("break")
        assert self.session_id is not None
        self._break_id = self._db.start_break(self.session_id, self._clock.now())
        self.state = target
        self._break_started = self._clock.monotonic()
        self._break_forced = forced
        self._persist()
        self._bus.publish(
            EventType.BREAK_STARTED,
            session_id=self.session_id,
            forced=forced,
            seconds=self._config.break_seconds,
        )

    def _end_break(self, skipped: bool) -> None:
        target = self._require("end_break")
        started = self._break_started if self._break_started is not None else self._last
        taken = max(0.0, self._clock.monotonic() - started)
        if self._break_id is not None:
            self._db.end_break(self._break_id, self._clock.now(), skipped)
        self.state = target
        self.since_break = 0.0
        self._suggested = False
        self._break_id = None
        self._break_started = None
        self._break_forced = False
        self._persist()
        self._bus.publish(
            EventType.BREAK_ENDED, session_id=self.session_id, skipped=skipped, seconds=int(taken)
        )

    def _finish(self, status: str) -> dict[str, Any]:
        assert self.session_id is not None and self.mode is not None
        focused = int(self.focused)
        reward = self._reward_for(focused) if status == "completed" else 0
        self._db.finish_session(self.session_id, status, self._clock.now(), focused, reward)
        summary = {
            "session_id": self.session_id,
            "mode": self.mode.value,
            "status": status,
            "focused_seconds": focused,
            "reward_seconds": reward,
            "goal": self.goal or "",
        }
        self._reset()
        self._bus.publish(EventType.SESSION_ENDED, **summary)
        return summary

    def snapshot(self) -> dict[str, Any]:
        now = self._clock.monotonic()
        remaining = None
        if self.planned_seconds is not None:
            remaining = max(0, int(self.planned_seconds - self.focused))
        next_break = None
        if self.state is State.FOCUSING:
            next_break = max(0, int(self._config.break_interval_seconds - self.since_break))
        break_remaining = None
        if self.state is State.ON_BREAK and self._break_started is not None:
            break_remaining = max(0, int(self._config.break_seconds - (now - self._break_started)))
        return {
            "state": self.state.value,
            "session_id": self.session_id,
            "mode": self.mode.value if self.mode else None,
            "goal": self.goal,
            "focused_seconds": int(self.focused),
            "planned_seconds": self.planned_seconds,
            "remaining_seconds": remaining,
            "next_break_in_seconds": next_break,
            "break_remaining_seconds": break_remaining,
            "break_forced": self._break_forced,
            "break_suggested": self._suggested,
            "strict": self._config.strict,
        }


def suspended_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """What status shows for a session saved in the database but not running right now."""
    planned = row["planned_seconds"]
    focused = int(row["focused_seconds"])
    return {
        "state": "suspended",
        "session_id": int(row["id"]),
        "mode": row["mode"],
        "goal": row["goal"],
        "focused_seconds": focused,
        "planned_seconds": planned,
        "remaining_seconds": max(0, planned - focused) if planned is not None else None,
        "next_break_in_seconds": None,
        "break_remaining_seconds": None,
        "break_forced": False,
        "break_suggested": False,
        "strict": False,
    }
