import pytest

from focuslock.clock import FakeClock
from focuslock.db import Database
from focuslock.errors import InvalidTransitionError
from focuslock.events import Event, EventBus, EventType
from focuslock.sessions import EngineConfig, FocusEngine, Mode, State


def make_engine(db: Database, clock: FakeClock, events: list[Event], strict: bool = False):
    bus = EventBus()
    bus.subscribe(EventBus.WILDCARD, events.append)
    config = EngineConfig(break_interval_seconds=600, break_seconds=60, strict=strict)
    return FocusEngine(db, bus, clock, config, reward_for=lambda focused: focused // 9)


def advance(engine: FocusEngine, clock: FakeClock, seconds: int) -> None:
    for _ in range(seconds):
        clock.advance(1)
        engine.tick()


def types(events: list[Event]) -> list[str]:
    return [event.type for event in events]


def test_timer_session_completes_and_earns_reward(db, clock, events) -> None:
    engine = make_engine(db, clock, events)
    engine.start(Mode.TIMER, planned_seconds=300)
    advance(engine, clock, 300)
    assert engine.state is State.IDLE
    ended = events[-1]
    assert ended.type == EventType.SESSION_ENDED
    assert ended.data["status"] == "completed"
    assert ended.data["focused_seconds"] == 300
    assert db.reward_balance() == 300 // 9


def test_suspend_and_resume_keep_the_time_left(db, clock, events) -> None:
    engine = make_engine(db, clock, events)
    engine.start(Mode.TIMER, planned_seconds=500)
    advance(engine, clock, 120)
    session_id = engine.suspend()
    assert engine.state is State.IDLE
    clock.advance(3600)
    row = db.active_session()
    assert row is not None and row["id"] == session_id and row["focused_seconds"] == 120
    other = make_engine(db, clock, events)
    snap = other.resume_from(row)
    assert snap["remaining_seconds"] == 380
    advance(other, clock, 380)
    assert other.state is State.IDLE and db.get_session(session_id)["status"] == "completed"


def test_interrupted_break_is_taken_again_after_resume(db, clock, events) -> None:
    engine = make_engine(db, clock, events)
    engine.start(Mode.TIMER, planned_seconds=3600)
    advance(engine, clock, 610)
    assert engine.state is State.ON_BREAK
    engine.suspend()
    other = make_engine(db, clock, events)
    other.resume_from(db.active_session())
    advance(other, clock, 1)
    assert other.state is State.ON_BREAK


def test_forced_break_starts_and_ends_on_its_own(db, clock, events) -> None:
    engine = make_engine(db, clock, events)
    engine.start(Mode.TIMER, planned_seconds=3600)
    advance(engine, clock, 600)
    assert engine.state is State.ON_BREAK
    advance(engine, clock, 59)
    assert engine.snapshot()["break_remaining_seconds"] == 1
    advance(engine, clock, 1)
    assert engine.state is State.FOCUSING
    assert types(events).count(EventType.BREAK_STARTED) == 1
    assert engine.snapshot()["focused_seconds"] == 600


def test_flow_mode_only_suggests_breaks(db, clock, events) -> None:
    engine = make_engine(db, clock, events)
    engine.start(Mode.FLOW)
    advance(engine, clock, 1200)
    assert engine.state is State.FOCUSING
    assert types(events).count(EventType.BREAK_SUGGESTED) == 1
    summary = engine.stop()
    assert summary["status"] == "completed"


def test_goal_mode_needs_goal_and_completes_manually(db, clock, events) -> None:
    engine = make_engine(db, clock, events)
    with pytest.raises(ValueError):
        engine.start(Mode.GOAL)
    engine.start(Mode.GOAL, goal="  finish ELEC 3160 lab  ")
    assert engine.goal == "finish ELEC 3160 lab"
    advance(engine, clock, 90)
    assert engine.complete_goal()["status"] == "completed"


def test_stopping_a_timer_early_abandons_it(db, clock, events) -> None:
    engine = make_engine(db, clock, events)
    engine.start(Mode.TIMER, planned_seconds=900)
    advance(engine, clock, 100)
    assert engine.stop()["status"] == "abandoned"
    assert db.reward_balance() == 0


def test_invalid_transitions_raise(db, clock, events) -> None:
    engine = make_engine(db, clock, events)
    with pytest.raises(InvalidTransitionError):
        engine.stop()
    with pytest.raises(InvalidTransitionError):
        engine.take_break()
    engine.start(Mode.FLOW)
    with pytest.raises(InvalidTransitionError):
        engine.start(Mode.FLOW)
    with pytest.raises(InvalidTransitionError):
        engine.skip_break()
    with pytest.raises(InvalidTransitionError):
        engine.complete_goal()


def test_strict_mode_blocks_skipping_forced_breaks(db, clock, events) -> None:
    engine = make_engine(db, clock, events, strict=True)
    engine.start(Mode.TIMER, planned_seconds=3600)
    advance(engine, clock, 600)
    with pytest.raises(InvalidTransitionError, match="strict"):
        engine.skip_break()


def test_manual_break_can_be_skipped(db, clock, events) -> None:
    engine = make_engine(db, clock, events, strict=True)
    engine.start(Mode.FLOW)
    engine.take_break()
    engine.skip_break()
    assert engine.state is State.FOCUSING
    assert events[-1].data["skipped"] is True


def test_progress_is_saved_for_crash_recovery(db, clock, events) -> None:
    engine = make_engine(db, clock, events)
    engine.start(Mode.FLOW)
    advance(engine, clock, 30)
    row = db.get_session(engine.session_id)
    assert row is not None and row["focused_seconds"] >= 25
