from collections.abc import Iterable
from datetime import date, timedelta


def earned_seconds(focused_seconds: int, *, focus_unit: int, reward_unit: int, minimum: int) -> int:
    """Reward = focused * reward_unit / focus_unit, rounded down (integer division).

    With the defaults (90 min focus -> 10 min reward): 5400 s * 600 // 5400 = 600 s.
    Sessions shorter than `minimum` earn nothing, so 1-minute sessions can't be farmed.
    """
    if focus_unit <= 0 or focused_seconds <= 0 or focused_seconds < minimum:
        return 0
    return focused_seconds * reward_unit // focus_unit


def current_streak(days: Iterable[date], today: date) -> int:
    """Consecutive days with a completed session, ending today (or yesterday, so a streak
    is not lost in the morning before you've worked)."""
    have = set(days)
    cursor = today if today in have else today - timedelta(days=1)
    streak = 0
    while cursor in have:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def longest_streak(days: Iterable[date]) -> int:
    best = run = 0
    previous: date | None = None
    for day in sorted(set(days)):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = day
    return best
