import pytest

from focuslock.errors import AlreadyRunningError, RewardError
from focuslock.events import EventType
from tests.conftest import make_app, run_for


def test_sites_blocked_only_during_sessions(app, clock) -> None:
    assert app.hosts.blocked() == []
    app.start_session("timer", minutes=30)
    assert set(app.hosts.blocked()) == set(app.blocklist())
    run_for(app, clock, 30 * 60, step=10)
    assert app.hosts.blocked() == []


def test_block_outside_sessions_blocks_while_idle(tmp_path, hosts, clock, events) -> None:
    app = make_app(tmp_path, hosts, clock, events, block_outside_sessions=True)
    app.reconcile()
    assert app.hosts.blocked()
    app.close()


def test_reconcile_repairs_manual_hosts_edits(app, clock) -> None:
    app.start_session("flow")
    app.hosts.unblock()
    run_for(app, clock, 30)
    assert app.hosts.blocked()


def test_add_and_remove_domains_sync_hosts(app) -> None:
    app.start_session("flow")
    assert app.add_domains(["https://twitch.tv/somebody"]) == ["twitch.tv", "www.twitch.tv"]
    assert "twitch.tv" in app.hosts.blocked()
    assert app.remove_domains(["twitch.tv"]) == ["twitch.tv", "www.twitch.tv"]
    assert "twitch.tv" not in app.hosts.blocked()


def test_reward_spending_unblocks_then_reblocks(tmp_path, hosts, clock, events) -> None:
    app = make_app(tmp_path, hosts, clock, events, block_outside_sessions=True)
    app.reconcile()
    with pytest.raises(RewardError):
        app.spend_reward(5)
    app.start_session("timer", minutes=90)
    run_for(app, clock, (90 + 5) * 60, step=30)
    assert app.rewards()["balance_seconds"] == 600
    app.spend_reward(5)
    assert app.hosts.blocked() == []
    assert app.reward_window_remaining() == 300
    run_for(app, clock, 300)
    assert app.hosts.blocked()
    assert EventType.REWARD_WINDOW_ENDED in [event.type for event in events]
    assert app.rewards()["balance_seconds"] == 300
    app.close()


def test_cannot_spend_during_session(app) -> None:
    app.db.add_ledger(600, "earned", app.clock.now())
    app.start_session("flow")
    with pytest.raises(RewardError, match="finish"):
        app.spend_reward(5)


def test_reward_window_survives_restart(tmp_path, hosts, clock, events) -> None:
    app = make_app(tmp_path, hosts, clock, events)
    app.db.add_ledger(600, "earned", clock.now())
    app.spend_reward(10)
    app.close()
    clock.advance(120)
    again = make_app(tmp_path, hosts, clock, events)
    assert again.reward_window_remaining() == 480
    again.close()


def test_stats_and_status(app, clock) -> None:
    app.start_session("timer", minutes=30)
    run_for(app, clock, 30 * 60, step=60)
    stats = app.stats(7)
    assert stats["sessions"] == 1 and stats["completion_rate"] == 1.0
    assert stats["current_streak"] == 1
    assert stats["days"][-1]["focused_seconds"] == 1800
    assert app.status()["session"]["state"] == "idle"


def test_events_are_written_to_log(tmp_path, hosts_file) -> None:
    from focuslock.app import FocusLockApp

    app = FocusLockApp.open(home=tmp_path / "home", hosts_path=hosts_file, flush=False)
    app.start_session("flow")
    app.stop()
    app.close()
    log = (tmp_path / "home" / "focuslock.log").read_text()
    assert "event=session_started" in log and "event=session_ended" in log


def test_second_instance_is_refused(tmp_path, hosts_file) -> None:
    from focuslock.app import FocusLockApp

    first = FocusLockApp.open(home=tmp_path, hosts_path=hosts_file, flush=False)
    with pytest.raises(AlreadyRunningError):
        FocusLockApp.open(home=tmp_path, hosts_path=hosts_file, flush=False)
    first.close()
    FocusLockApp.open(home=tmp_path, hosts_path=hosts_file, flush=False).close()
