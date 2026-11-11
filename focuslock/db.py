import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import RewardError, SessionConflictError

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE sessions (
            id              INTEGER PRIMARY KEY,
            mode            TEXT    NOT NULL CHECK (mode IN ('timer', 'flow', 'goal')),
            goal            TEXT,
            planned_seconds INTEGER CHECK (planned_seconds IS NULL OR planned_seconds > 0),
            started_at      TEXT    NOT NULL,
            ended_at        TEXT,
            focused_seconds INTEGER NOT NULL DEFAULT 0 CHECK (focused_seconds >= 0),
            status          TEXT    NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'completed', 'abandoned'))
        )
        """,
        "CREATE INDEX idx_sessions_started_at ON sessions (started_at)",
        "CREATE UNIQUE INDEX idx_one_active_session ON sessions (status) WHERE status = 'active'",
        """
        CREATE TABLE breaks (
            id         INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
            started_at TEXT    NOT NULL,
            ended_at   TEXT,
            skipped    INTEGER NOT NULL DEFAULT 0 CHECK (skipped IN (0, 1))
        )
        """,
        "CREATE INDEX idx_breaks_session ON breaks (session_id)",
        """
        CREATE TABLE reward_ledger (
            id            INTEGER PRIMARY KEY,
            created_at    TEXT    NOT NULL,
            delta_seconds INTEGER NOT NULL CHECK (delta_seconds <> 0),
            reason        TEXT    NOT NULL CHECK (reason IN ('earned', 'spent', 'adjustment')),
            session_id    INTEGER REFERENCES sessions (id) ON DELETE SET NULL
        )
        """,
        "CREATE INDEX idx_ledger_created_at ON reward_ledger (created_at)",
        """
        CREATE TABLE blocklist (
            domain   TEXT PRIMARY KEY,
            added_at TEXT NOT NULL
        )
        """,
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ),
    (
        "ALTER TABLE sessions ADD COLUMN since_break_seconds INTEGER NOT NULL DEFAULT 0",
        """
        CREATE TABLE cheat_log (
            id         INTEGER PRIMARY KEY,
            created_at TEXT    NOT NULL,
            kind       TEXT    NOT NULL CHECK (kind IN (
                               'password_override', 'password_failed',
                               'app_closed_mid_session', 'hosts_tampered')),
            detail     TEXT    NOT NULL DEFAULT '',
            session_id INTEGER REFERENCES sessions (id) ON DELETE SET NULL
        )
        """,
        "CREATE INDEX idx_cheat_log_created_at ON cheat_log (created_at)",
    ),
)

CHEAT_KINDS = ("password_override", "password_failed", "app_closed_mid_session", "hosts_tampered")


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class Database:
    """All SQL lives here (the Repository pattern): the rest of the app calls methods,
    never writes queries. Every query uses ? placeholders, so user text can never be run
    as SQL (no SQL injection)."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE takes the write lock up front, so check-then-write logic
        (e.g. "is there enough balance? then spend") cannot be raced by another writer."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        rows = self._rows(sql, params)
        return rows[0][0] if rows else None

    @property
    def schema_version(self) -> int:
        return int(self._scalar("PRAGMA user_version"))

    def _migrate(self) -> None:
        current = self.schema_version
        for version, statements in enumerate(MIGRATIONS[current:], start=current + 1):
            with self.transaction() as conn:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {int(version)}")

    # meta ---------------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        return self._scalar("SELECT value FROM meta WHERE key = ?", (key,))

    def set_meta(self, key: str, value: str | None) -> None:
        with self.transaction() as conn:
            if value is None:
                conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    # sessions -----------------------------------------------------------------------------

    def create_session(
        self,
        mode: str,
        started_at: datetime,
        planned_seconds: int | None = None,
        goal: str | None = None,
    ) -> int:
        try:
            with self.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO sessions (mode, goal, planned_seconds, started_at) "
                    "VALUES (?, ?, ?, ?)",
                    (mode, goal, planned_seconds, iso(started_at)),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise SessionConflictError("a focus session is already active") from exc
            raise

    def save_progress(self, session_id: int, focused_seconds: int, since_break: int = 0) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET focused_seconds = ?, since_break_seconds = ? "
                "WHERE id = ? AND status = 'active'",
                (focused_seconds, since_break, session_id),
            )

    def active_session(self) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM sessions WHERE status = 'active'")
        return dict(rows[0]) if rows else None

    def close_open_breaks(self, session_id: int, at: datetime) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE breaks SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
                (iso(at), session_id),
            )

    def finish_session(
        self,
        session_id: int,
        status: str,
        ended_at: datetime,
        focused_seconds: int,
        reward_seconds: int,
    ) -> None:
        """Close the session, any open break, and credit the reward in ONE transaction:
        either all three happen or none do."""
        stamp = iso(ended_at)
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, ended_at = ?, focused_seconds = ? "
                "WHERE id = ? AND status = 'active'",
                (status, stamp, focused_seconds, session_id),
            )
            conn.execute(
                "UPDATE breaks SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
                (stamp, session_id),
            )
            if reward_seconds > 0:
                conn.execute(
                    "INSERT INTO reward_ledger (created_at, delta_seconds, reason, session_id) "
                    "VALUES (?, ?, 'earned', ?)",
                    (stamp, reward_seconds, session_id),
                )

    def start_break(self, session_id: int, at: datetime) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO breaks (session_id, started_at) VALUES (?, ?)", (session_id, iso(at))
            )
            return int(cursor.lastrowid)

    def end_break(self, break_id: int, at: datetime, skipped: bool) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE breaks SET ended_at = ?, skipped = ? WHERE id = ?",
                (iso(at), int(skipped), break_id),
            )

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return dict(rows[0]) if rows else None

    def list_sessions(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._rows(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM breaks b WHERE b.session_id = s.id) AS breaks,
                   (SELECT COALESCE(SUM(l.delta_seconds), 0) FROM reward_ledger l
                     WHERE l.session_id = s.id) AS reward_seconds
            FROM sessions s
            ORDER BY s.started_at DESC, s.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(row) for row in rows]

    # rewards ------------------------------------------------------------------------------

    def reward_balance(self) -> int:
        return int(self._scalar("SELECT COALESCE(SUM(delta_seconds), 0) FROM reward_ledger"))

    def reward_spent_on(self, day: date) -> int:
        return int(
            self._scalar(
                "SELECT COALESCE(-SUM(delta_seconds), 0) FROM reward_ledger "
                "WHERE reason = 'spent' AND date(created_at, 'localtime') = ?",
                (day.isoformat(),),
            )
        )

    def add_ledger(
        self, delta_seconds: int, reason: str, at: datetime, session_id: int | None = None
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO reward_ledger (created_at, delta_seconds, reason, session_id) "
                "VALUES (?, ?, ?, ?)",
                (iso(at), delta_seconds, reason, session_id),
            )

    def spend_reward(self, seconds: int, at: datetime, daily_limit_seconds: int) -> int:
        if seconds <= 0:
            raise RewardError("amount must be positive")
        day = at.astimezone().date().isoformat()
        with self.transaction() as conn:
            balance = conn.execute(
                "SELECT COALESCE(SUM(delta_seconds), 0) FROM reward_ledger"
            ).fetchone()[0]
            spent = conn.execute(
                "SELECT COALESCE(-SUM(delta_seconds), 0) FROM reward_ledger "
                "WHERE reason = 'spent' AND date(created_at, 'localtime') = ?",
                (day,),
            ).fetchone()[0]
            if seconds > balance:
                raise RewardError(f"not enough reward time: {balance // 60} min available")
            left_today = max(0, daily_limit_seconds - spent)
            if seconds > left_today:
                raise RewardError(f"daily reward limit reached: {left_today // 60} min left today")
            conn.execute(
                "INSERT INTO reward_ledger (created_at, delta_seconds, reason) "
                "VALUES (?, ?, 'spent')",
                (iso(at), -seconds),
            )
            return int(balance - seconds)

    # statistics ---------------------------------------------------------------------------

    def daily_focus(self, days: int, today: date) -> list[dict[str, Any]]:
        start = today - timedelta(days=days - 1)
        rows = self._rows(
            """
            SELECT date(started_at, 'localtime') AS day,
                   SUM(focused_seconds)          AS focused,
                   COUNT(*)                      AS sessions,
                   SUM(status = 'completed')     AS completed
            FROM sessions
            WHERE status <> 'active'
              AND date(started_at, 'localtime') BETWEEN ? AND ?
            GROUP BY day
            ORDER BY day
            """,
            (start.isoformat(), today.isoformat()),
        )
        by_day = {row["day"]: row for row in rows}
        result = []
        for offset in range(days):
            day = (start + timedelta(days=offset)).isoformat()
            row = by_day.get(day)
            result.append(
                {
                    "day": day,
                    "focused_seconds": int(row["focused"]) if row else 0,
                    "sessions": int(row["sessions"]) if row else 0,
                    "completed": int(row["completed"]) if row else 0,
                }
            )
        return result

    def completed_days(self) -> set[date]:
        rows = self._rows(
            "SELECT DISTINCT date(started_at, 'localtime') FROM sessions WHERE status = 'completed'"
        )
        return {date.fromisoformat(row[0]) for row in rows}

    def totals(self) -> dict[str, int]:
        row = self._rows(
            "SELECT COUNT(*), COALESCE(SUM(status = 'completed'), 0), "
            "COALESCE(SUM(focused_seconds), 0) FROM sessions WHERE status <> 'active'"
        )[0]
        return {"sessions": int(row[0]), "completed": int(row[1]), "focused_seconds": int(row[2])}

    # blocklist ----------------------------------------------------------------------------

    def list_domains(self) -> list[str]:
        return [row[0] for row in self._rows("SELECT domain FROM blocklist ORDER BY domain")]

    def add_domains(self, domains: Iterable[str], at: datetime) -> list[str]:
        added = []
        with self.transaction() as conn:
            for domain in domains:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO blocklist (domain, added_at) VALUES (?, ?)",
                    (domain, iso(at)),
                )
                if cursor.rowcount:
                    added.append(domain)
        return added

    def remove_domains(self, domains: Iterable[str]) -> list[str]:
        removed = []
        with self.transaction() as conn:
            for domain in domains:
                cursor = conn.execute("DELETE FROM blocklist WHERE domain = ?", (domain,))
                if cursor.rowcount:
                    removed.append(domain)
        return removed

    # cheat log ----------------------------------------------------------------------------

    def add_cheat(
        self, kind: str, at: datetime, detail: str = "", session_id: int | None = None
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO cheat_log (created_at, kind, detail, session_id) VALUES (?, ?, ?, ?)",
                (iso(at), kind, detail, session_id),
            )

    def list_cheats(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM cheat_log ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    def cheat_counts(self, start: date, end: date) -> dict[str, int]:
        rows = self._rows(
            "SELECT kind, COUNT(*) FROM cheat_log "
            "WHERE date(created_at, 'localtime') BETWEEN ? AND ? GROUP BY kind",
            (start.isoformat(), end.isoformat()),
        )
        counts = dict.fromkeys(CHEAT_KINDS, 0)
        counts.update({row[0]: int(row[1]) for row in rows})
        return counts
