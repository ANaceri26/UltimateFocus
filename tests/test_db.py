from datetime import UTC, datetime, timedelta

import pytest

from focuslock.db import MIGRATIONS, Database
from focuslock.errors import RewardError, SessionConflictError

NOW = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


def test_migrations_set_schema_version(db: Database) -> None:
    assert db.schema_version == len(MIGRATIONS)


def test_migrations_are_not_rerun(tmp_path) -> None:
    path = tmp_path / "x.db"
    Database(path).close()
    again = Database(path)
    assert again.schema_version == len(MIGRATIONS)
    again.close()


def test_only_one_active_session(db: Database) -> None:
    db.create_session("timer", NOW, 600)
    with pytest.raises(SessionConflictError):
        db.create_session("flow", NOW)


def test_finish_session_credits_reward_atomically(db: Database) -> None:
    session = db.create_session("timer", NOW, 5400)
    db.start_break(session, NOW)
    db.finish_session(session, "completed", NOW + timedelta(hours=2), 5400, 600)
    row = db.get_session(session)
    assert row is not None and row["status"] == "completed"
    assert db.reward_balance() == 600
    assert db.list_sessions()[0]["reward_seconds"] == 600


def test_check_constraint_rejects_bad_mode(db: Database) -> None:
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.create_session("nap", NOW)


def test_spend_reward_checks_balance_and_daily_limit(db: Database) -> None:
    db.add_ledger(3600, "earned", NOW)
    with pytest.raises(RewardError, match="not enough"):
        db.spend_reward(7200, NOW, daily_limit_seconds=10_000)
    assert db.spend_reward(1200, NOW, daily_limit_seconds=1800) == 2400
    with pytest.raises(RewardError, match="daily reward limit"):
        db.spend_reward(1200, NOW, daily_limit_seconds=1800)
    assert db.reward_balance() == 2400


def test_failed_transaction_rolls_back(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
        raise RuntimeError("boom")
    assert db.get_meta("k") is None


def test_active_session_and_progress(db: Database) -> None:
    assert db.active_session() is None
    session = db.create_session("timer", NOW, 600)
    db.save_progress(session, 120, 45)
    row = db.active_session()
    assert row is not None and (row["focused_seconds"], row["since_break_seconds"]) == (120, 45)


def test_cheat_log(db: Database) -> None:
    db.add_cheat("password_failed", NOW, "unblock")
    db.add_cheat("hosts_tampered", NOW, "youtube.com")
    db.add_cheat("hosts_tampered", NOW - timedelta(days=30), "old")
    day = NOW.astimezone().date()
    counts = db.cheat_counts(day - timedelta(days=6), day)
    assert counts["hosts_tampered"] == 1 and counts["password_override"] == 0
    assert db.list_cheats(1)[0]["kind"] in {"password_failed", "hosts_tampered"}
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.add_cheat("made_up_kind", NOW)


def test_daily_focus_fills_empty_days(db: Database) -> None:
    session = db.create_session("timer", NOW, 1800)
    db.finish_session(session, "completed", NOW, 1800, 0)
    today = NOW.astimezone().date()
    days = db.daily_focus(3, today)
    assert [day["focused_seconds"] for day in days] == [0, 0, 1800]
    assert days[-1]["completed"] == 1
    assert db.completed_days() == {today}


def test_blocklist_add_and_remove(db: Database) -> None:
    assert db.add_domains(["b.com", "a.com", "b.com"], NOW) == ["b.com", "a.com"]
    assert db.list_domains() == ["a.com", "b.com"]
    assert db.remove_domains(["a.com", "x.com"]) == ["a.com"]


def test_sql_injection_is_just_text(db: Database) -> None:
    evil = "x.com'); DROP TABLE blocklist; --"
    db.add_domains([evil], NOW)
    assert evil in db.list_domains()
