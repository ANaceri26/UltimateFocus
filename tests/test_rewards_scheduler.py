from datetime import date

from focuslock.clock import FakeClock
from focuslock.rewards import current_streak, earned_seconds, longest_streak
from focuslock.scheduler import Scheduler


def test_reward_formula_matches_90_to_10() -> None:
    kwargs = {"focus_unit": 5400, "reward_unit": 600, "minimum": 1500}
    assert earned_seconds(5400, **kwargs) == 600
    assert earned_seconds(2700, **kwargs) == 300
    assert earned_seconds(1499, **kwargs) == 0


def test_streaks() -> None:
    days = {date(2026, 1, d) for d in (1, 2, 3, 5, 6)}
    assert longest_streak(days) == 3
    assert current_streak(days, date(2026, 1, 6)) == 2
    assert current_streak(days, date(2026, 1, 7)) == 2
    assert current_streak(days, date(2026, 1, 8)) == 0


def test_scheduler_runs_in_time_order_and_fifo_on_ties() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    ran = []
    scheduler.call_later(5, ran.append, "b")
    scheduler.call_later(1, ran.append, "a")
    scheduler.call_later(5, ran.append, "c")
    clock.advance(4)
    assert scheduler.run_due() == 1
    clock.advance(1)
    scheduler.run_due()
    assert ran == ["a", "b", "c"]


def test_scheduler_cancel_and_failures() -> None:
    clock = FakeClock()
    scheduler = Scheduler(clock)
    ran = []
    job = scheduler.call_later(1, ran.append, "never")
    scheduler.call_later(2, lambda: 1 / 0)
    scheduler.call_later(3, ran.append, "after error")
    assert scheduler.cancel(job)
    assert not scheduler.cancel(job)
    assert scheduler.next_due_in() == 2
    clock.advance(3)
    assert scheduler.run_due() == 2
    assert ran == ["after error"]
    assert len(scheduler) == 0
