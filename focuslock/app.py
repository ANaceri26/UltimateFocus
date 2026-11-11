"""The composition root: builds every part and wires them together.

Blocking uses *reconciliation* (the idea behind Kubernetes controllers): instead of
remembering "I blocked X earlier, now unblock it", the app computes the desired hosts
block from the current state and makes the file match. Running it twice changes nothing
(idempotent), and it repairs manual edits to the hosts file on the next pass.
"""

import json
import logging
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .auth import MIN_PASSWORD_LENGTH, hash_password, verify_password
from .clock import Clock, SystemClock
from .config import SETTINGS_FILE, Settings, load_settings
from .db import Database, iso
from .domains import normalize, unique, with_variants
from .errors import (
    AlreadyRunningError,
    FocusLockError,
    PasswordError,
    RewardError,
    SessionConflictError,
)
from .events import Event, EventBus, EventType
from .hosts import HostsManager
from .logfmt import EventLog
from .rewards import current_streak, earned_seconds, longest_streak
from .scheduler import Scheduler
from .sessions import EngineConfig, FocusEngine, Mode, State, suspended_snapshot
from .system import InstanceLock, data_dir, default_hosts_path, flush_dns

logger = logging.getLogger(__name__)

DB_FILE = "focuslock.db"
LOG_FILE = "focuslock.log"
LOCK_FILE = "focuslock.lock"
RECONCILE_EVERY_SECONDS = 30.0
_REWARD_UNTIL = "reward_until"
_SEEDED = "blocklist_seeded"
_PASSWORD = "password_hash"
_APPLIED = "hosts_applied"


class FocusLockApp:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        hosts: HostsManager,
        *,
        home: str | Path,
        clock: Clock | None = None,
        bus: EventBus | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.hosts = hosts
        self.home = Path(home)
        self.clock = clock or SystemClock()
        self.bus = bus or EventBus()
        self.lock = threading.RLock()
        self.scheduler = Scheduler(self.clock)
        self.event_log = event_log
        self._instance_lock: InstanceLock | None = None
        if event_log is not None:
            self.bus.subscribe(EventBus.WILDCARD, self._log_event)
        self.engine = FocusEngine(
            db,
            self.bus,
            self.clock,
            EngineConfig(
                break_interval_seconds=settings.break_interval_minutes * 60,
                break_seconds=settings.break_minutes * 60,
                strict=settings.strict_mode,
            ),
            self._reward_for,
        )
        self._reward_job: int | None = None
        self._reward_until: float | None = None
        self._last_state = self.engine.state
        self._last_reconcile = self.clock.monotonic()
        if db.get_meta(_SEEDED) is None:
            db.add_domains(unique(settings.default_blocklist), self.clock.now())
            db.set_meta(_SEEDED, iso(self.clock.now()))
        self._restore_reward_window()

    @classmethod
    def open(
        cls,
        home: str | Path | None = None,
        hosts_path: str | Path | None = None,
        *,
        flush: bool = True,
    ) -> "FocusLockApp":
        home = Path(home) if home else data_dir()
        home.mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(home / LOCK_FILE)
        if not lock.acquire():
            raise AlreadyRunningError(
                "FocusLock is already running (serve, gui, session or spend). "
                "Stop it first, or talk to it through the REST API."
            )
        try:
            settings = load_settings(home / SETTINGS_FILE)
            path = Path(hosts_path or settings.hosts_path or default_hosts_path())
            hosts = HostsManager(path, home / "backups", flush=flush_dns if flush else None)
            app = cls(
                settings,
                Database(home / DB_FILE),
                hosts,
                home=home,
                event_log=EventLog(home / LOG_FILE),
            )
        except BaseException:
            lock.release()
            raise
        app._instance_lock = lock
        return app

    def close(self) -> None:
        """Shut down. A running session is NOT ended: it is saved and resumes next time,
        and the early close is written to the cheat log."""
        with self.lock:
            try:
                if self.engine.state is not State.IDLE:
                    remaining = self.engine.snapshot()["remaining_seconds"]
                    session_id = self.engine.suspend()
                    detail = f"{remaining // 60} min left" if remaining is not None else ""
                    self.record_cheat("app_closed_mid_session", detail, session_id)
            finally:
                self.db.close()
                if self.event_log is not None:
                    self.event_log.close()
                if self._instance_lock is not None:
                    self._instance_lock.release()

    def _log_event(self, event: Event) -> None:
        assert self.event_log is not None
        self.event_log.write(event.type, event.data, event.at)

    def _reward_for(self, focused_seconds: int) -> int:
        return earned_seconds(
            focused_seconds,
            focus_unit=self.settings.reward_focus_minutes * 60,
            reward_unit=self.settings.reward_minutes * 60,
            minimum=self.settings.min_reward_session_minutes * 60,
        )

    # password and cheat log ---------------------------------------------------------------

    def password_required(self) -> bool:
        return self.db.get_meta(_PASSWORD) is not None

    def authorize(self, action: str, password: str | None) -> None:
        """Allow a protected action, or raise PasswordError.

        Without a configured password everything is allowed. With one, a correct password
        is logged as an override and a wrong one as a failed attempt."""
        stored = self.db.get_meta(_PASSWORD)
        if stored is None:
            return
        session_id = self.engine.session_id
        if not password:
            raise PasswordError(f"'{action}' needs the FocusLock password")
        if not verify_password(password, stored):
            self.record_cheat("password_failed", action, session_id)
            raise PasswordError("wrong password")
        self.record_cheat("password_override", action, session_id)

    def set_password(self, new_password: str, current: str | None = None) -> None:
        with self.lock:
            if self.password_required():
                self.authorize("change password", current)
            if len(new_password) < MIN_PASSWORD_LENGTH:
                raise ValueError(f"use at least {MIN_PASSWORD_LENGTH} characters")
            self.db.set_meta(_PASSWORD, hash_password(new_password))

    def clear_password(self, current: str | None) -> None:
        with self.lock:
            self.authorize("remove password", current)
            self.db.set_meta(_PASSWORD, None)

    def record_cheat(self, kind: str, detail: str = "", session_id: int | None = None) -> None:
        self.db.add_cheat(kind, self.clock.now(), detail, session_id)
        self.bus.publish(EventType.CHEAT_RECORDED, kind=kind, detail=detail)

    def cheats(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.list_cheats(limit)

    # blocking -----------------------------------------------------------------------------

    def suspended_session(self) -> dict[str, Any] | None:
        """A session saved in the database while FocusLock was closed (not running now)."""
        if self.engine.state is not State.IDLE:
            return None
        return self.db.active_session()

    def in_session(self) -> bool:
        return self.engine.state is not State.IDLE or self.db.active_session() is not None

    def enforcing(self) -> bool:
        if self.in_session():
            return True
        if self._reward_until is not None:
            return False
        return self.settings.block_outside_sessions

    def _applied(self) -> list[str] | None:
        stored = self.db.get_meta(_APPLIED)
        return json.loads(stored) if stored else None

    def _set_applied(self, domains: list[str]) -> None:
        value = json.dumps(sorted(domains))
        if self.db.get_meta(_APPLIED) != value:
            self.db.set_meta(_APPLIED, value)

    def reconcile(self) -> dict[str, list[str]]:
        with self.lock:
            enforcing = self.enforcing()
            desired = self.db.list_domains() if enforcing else []
            if enforcing:
                applied = set(self._applied() or [])
                present = set(self.hosts.blocked())
                missing = sorted((applied & set(desired)) - present)
                if missing:
                    self.record_cheat("hosts_tampered", ",".join(missing), self.engine.session_id)
            added, removed = self.hosts.set_managed(desired)
            self._set_applied(desired)
            self._last_reconcile = self.clock.monotonic()
            if added or removed:
                self.bus.publish(
                    EventType.HOSTS_UPDATED,
                    added=",".join(added),
                    removed=",".join(removed),
                    blocked=len(desired),
                )
            return {"added": added, "removed": removed}

    def unblock_now(self, password: str | None = None) -> list[str]:
        with self.lock:
            self.authorize("unblock", password)
            removed = self.hosts.unblock()
            self._set_applied([])
            return removed

    def restore_hosts(self, password: str | None = None) -> bool:
        with self.lock:
            self.authorize("restore", password)
            restored = self.hosts.restore_original()
            if restored:
                self._set_applied([])
            return restored

    def blocklist(self) -> list[str]:
        return self.db.list_domains()

    def _expand(self, raws: Iterable[str], include_www: bool) -> list[str]:
        domains = [normalize(raw) for raw in raws]
        if include_www:
            return unique(variant for domain in domains for variant in with_variants(domain))
        return unique(domains)

    def add_domains(self, raws: Iterable[str], *, include_www: bool = True) -> list[str]:
        with self.lock:
            added = self.db.add_domains(self._expand(raws, include_www), self.clock.now())
            for domain in added:
                self.bus.publish(EventType.DOMAIN_ADDED, domain=domain)
            self.reconcile()
            return added

    def remove_domains(
        self, raws: Iterable[str], *, include_www: bool = True, password: str | None = None
    ) -> list[str]:
        with self.lock:
            domains = self._expand(raws, include_www)
            listed = set(self.db.list_domains())
            if not any(domain in listed for domain in domains):
                return []
            self.authorize("remove " + ",".join(domains), password)
            removed = self.db.remove_domains(domains)
            for domain in removed:
                self.bus.publish(EventType.DOMAIN_REMOVED, domain=domain)
            self.reconcile()
            return removed

    # sessions -----------------------------------------------------------------------------

    def start_session(
        self, mode: str, *, minutes: int | None = None, goal: str | None = None
    ) -> dict[str, Any]:
        if mode not in {m.value for m in Mode}:
            raise ValueError("mode must be one of: timer, flow, goal")
        if minutes is not None and minutes <= 0:
            raise ValueError("minutes must be positive")
        with self.lock:
            suspended = self.suspended_session()
            if suspended is not None:
                left = suspended_snapshot(suspended)["remaining_seconds"]
                extra = f" ({left // 60} min left)" if left is not None else ""
                raise SessionConflictError(
                    f"an earlier {suspended['mode']} session is unfinished{extra}; "
                    "resume it with `focuslock session` or the GUI"
                )
            planned = (minutes or self.settings.focus_minutes) * 60 if mode == Mode.TIMER else None
            snapshot = self.engine.start(mode, planned_seconds=planned, goal=goal)
            self._close_reward_window(announce=False)
            self._after_change()
            return snapshot

    def resume_suspended(self) -> dict[str, Any] | None:
        """Continue a session left unfinished when FocusLock was closed."""
        with self.lock:
            row = self.suspended_session()
            if row is None:
                return None
            snapshot = self.engine.resume_from(row)
            self._after_change()
            return snapshot

    def _run(self, action: Any) -> dict[str, Any]:
        with self.lock:
            result = action()
            self._after_change()
            return result

    def take_break(self) -> dict[str, Any]:
        return self._run(self.engine.take_break)

    def skip_break(self) -> dict[str, Any]:
        return self._run(self.engine.skip_break)

    def stop(self, password: str | None = None) -> dict[str, Any]:
        """End the session. Ending a timer or goal session early needs the password."""
        with self.lock:
            loaded_here = False
            if self.engine.state is State.IDLE:
                row = self.db.active_session()
                if row is None:
                    raise FocusLockError("no session is running")
                self.engine.resume_from(row)
                loaded_here = True
            if self.engine.stop_is_early:
                try:
                    self.authorize("end session early", password)
                except PasswordError:
                    if loaded_here:
                        self.engine.suspend()
                    raise
            return self._run(self.engine.stop)

    def complete_goal(self) -> dict[str, Any]:
        return self._run(self.engine.complete_goal)

    def _after_change(self) -> None:
        self._last_state = self.engine.state
        self.reconcile()

    def tick(self) -> None:
        with self.lock:
            self.engine.tick()
            self.scheduler.run_due()
            changed = self.engine.state is not self._last_state
            self._last_state = self.engine.state
            due = self.clock.monotonic() - self._last_reconcile >= RECONCILE_EVERY_SECONDS
            if changed or due:
                try:
                    self.reconcile()
                except (OSError, FocusLockError) as exc:
                    self._last_reconcile = self.clock.monotonic()
                    logger.warning("could not update the hosts file: %s", exc)

    def sessions(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        return self.db.list_sessions(limit, offset)

    # rewards ------------------------------------------------------------------------------

    def reward_window_remaining(self) -> int:
        if self._reward_until is None:
            return 0
        return max(0, round(self._reward_until - self.clock.monotonic()))

    def spend_reward(self, minutes: int) -> dict[str, Any]:
        if minutes <= 0:
            raise ValueError("minutes must be positive")
        with self.lock:
            if self.in_session():
                raise RewardError("finish your focus session before spending reward time")
            seconds = minutes * 60
            limit = self.settings.daily_reward_limit_minutes * 60
            self.db.spend_reward(seconds, self.clock.now(), limit)
            window = self.reward_window_remaining() + seconds
            self._open_reward_window(window)
            self.bus.publish(EventType.REWARD_SPENT, seconds=seconds, window_seconds=window)
            self.reconcile()
            return self.rewards()

    def end_reward_window(self) -> None:
        with self.lock:
            self._close_reward_window(announce=True)
            self.reconcile()

    def _open_reward_window(self, seconds: int) -> None:
        if self._reward_job is not None:
            self.scheduler.cancel(self._reward_job)
        self._reward_until = self.clock.monotonic() + seconds
        self._reward_job = self.scheduler.call_later(seconds, self._reward_window_expired)
        self.db.set_meta(_REWARD_UNTIL, iso(self.clock.now() + timedelta(seconds=seconds)))

    def _reward_window_expired(self) -> None:
        self._close_reward_window(announce=True)
        self.reconcile()

    def _close_reward_window(self, *, announce: bool) -> None:
        if self._reward_until is None:
            return
        if self._reward_job is not None:
            self.scheduler.cancel(self._reward_job)
        self._reward_until = None
        self._reward_job = None
        self.db.set_meta(_REWARD_UNTIL, None)
        if announce:
            self.bus.publish(EventType.REWARD_WINDOW_ENDED)

    def _restore_reward_window(self) -> None:
        stored = self.db.get_meta(_REWARD_UNTIL)
        if not stored:
            return
        remaining = (datetime.fromisoformat(stored) - self.clock.now()).total_seconds()
        if remaining > 0:
            self._open_reward_window(int(remaining))
        else:
            self.db.set_meta(_REWARD_UNTIL, None)

    def rewards(self) -> dict[str, Any]:
        today = self.clock.now().astimezone().date()
        return {
            "balance_seconds": self.db.reward_balance(),
            "spent_today_seconds": self.db.reward_spent_on(today),
            "daily_limit_seconds": self.settings.daily_reward_limit_minutes * 60,
            "window_remaining_seconds": self.reward_window_remaining(),
            "rate": {
                "focus_minutes": self.settings.reward_focus_minutes,
                "reward_minutes": self.settings.reward_minutes,
                "minimum_session_minutes": self.settings.min_reward_session_minutes,
            },
        }

    # reporting ----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self.lock:
            suspended = self.suspended_session()
            session = suspended_snapshot(suspended) if suspended else self.engine.snapshot()
            return {
                "session": session,
                "enforcing": self.enforcing(),
                "password_set": self.password_required(),
                "blocklist": self.blocklist(),
                "blocked_in_hosts": self.hosts.blocked(),
                "hosts_path": str(self.hosts.path),
                "rewards": self.rewards(),
            }

    def stats(self, days: int = 7) -> dict[str, Any]:
        if days < 1:
            raise ValueError("days must be at least 1")
        today = self.clock.now().astimezone().date()
        daily = self.db.daily_focus(days, today)
        totals = self.db.totals()
        completed_days = self.db.completed_days()
        sessions = totals["sessions"]
        cheats = self.db.cheat_counts(today - timedelta(days=days - 1), today)
        return {
            "days": daily,
            "window_focused_seconds": sum(day["focused_seconds"] for day in daily),
            "total_focused_seconds": totals["focused_seconds"],
            "sessions": sessions,
            "completed": totals["completed"],
            "completion_rate": round(totals["completed"] / sessions, 3) if sessions else 0.0,
            "current_streak": current_streak(completed_days, today),
            "longest_streak": longest_streak(completed_days),
            "cheats": cheats,
            "cheats_total": sum(cheats.values()),
        }
