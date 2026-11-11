import pytest

from focuslock.auth import hash_password, verify_password
from focuslock.errors import PasswordError, SessionConflictError
from focuslock.sessions import State
from tests.conftest import make_app, run_for


def kinds(app) -> list[str]:
    return [cheat["kind"] for cheat in app.cheats()]


def test_hash_and_verify() -> None:
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert verify_password("correct horse", stored)
    assert not verify_password("correct hors", stored)
    assert hash_password("same") != hash_password("same")
    assert not verify_password("x", "garbage")


def test_without_password_nothing_is_protected(app) -> None:
    app.start_session("timer", minutes=60)
    assert app.stop()["status"] == "abandoned"
    assert kinds(app) == []


def test_password_protects_unblock_remove_restore(tmp_path, hosts, clock, events) -> None:
    app = make_app(tmp_path, hosts, clock, events)
    app.reconcile()
    app.set_password("letmein")
    with pytest.raises(PasswordError, match="needs"):
        app.unblock_now()
    with pytest.raises(PasswordError, match="wrong"):
        app.unblock_now("nope")
    with pytest.raises(PasswordError):
        app.remove_domains(["youtube.com"])
    with pytest.raises(PasswordError):
        app.restore_hosts("")
    assert app.hosts.blocked()
    assert app.remove_domains(["youtu.be"], include_www=False, password="letmein") == ["youtu.be"]
    assert app.unblock_now("letmein")
    assert kinds(app).count("password_failed") == 1
    assert kinds(app).count("password_override") == 2
    app.close()


def test_early_stop_needs_password_but_flow_does_not(app) -> None:
    app.set_password("letmein")
    app.start_session("timer", minutes=60)
    with pytest.raises(PasswordError):
        app.stop()
    assert app.engine.state is State.FOCUSING
    assert app.stop("letmein")["status"] == "abandoned"
    app.start_session("flow")
    assert app.stop()["status"] == "completed"
    app.start_session("goal", goal="lab")
    assert app.complete_goal()["status"] == "completed"


def test_changing_password_needs_the_old_one(app) -> None:
    app.set_password("first1")
    with pytest.raises(PasswordError):
        app.set_password("second2", "wrong")
    app.set_password("second2", "first1")
    with pytest.raises(ValueError):
        app.set_password("abc", "second2")
    app.clear_password("second2")
    assert not app.password_required()


def test_closing_mid_session_suspends_logs_and_resumes(tmp_path, hosts, clock, events) -> None:
    app = make_app(tmp_path, hosts, clock, events, block_outside_sessions=False)
    app.start_session("timer", minutes=60)
    run_for(app, clock, 600, step=10)
    app.close()
    assert hosts.blocked(), "closing FocusLock must not unblock sites"

    clock.advance(7200)
    again = make_app(tmp_path, hosts, clock, events, block_outside_sessions=False)
    assert again.enforcing()
    assert again.status()["session"]["state"] == "suspended"
    assert again.status()["session"]["remaining_seconds"] == 3000
    with pytest.raises(SessionConflictError, match="unfinished"):
        again.start_session("flow")
    assert again.resume_suspended()["remaining_seconds"] == 3000
    assert "app_closed_mid_session" in kinds(again)
    run_for(again, clock, 3000 + 300, step=10)  # includes one 5-minute forced break
    assert again.engine.state is State.IDLE
    assert not hosts.blocked()
    again.close()


def test_stop_works_on_a_suspended_session(tmp_path, hosts, clock, events) -> None:
    app = make_app(tmp_path, hosts, clock, events)
    app.set_password("letmein")
    app.start_session("timer", minutes=60)
    app.close()
    again = make_app(tmp_path, hosts, clock, events)
    with pytest.raises(PasswordError):
        again.stop()
    assert again.suspended_session() is not None
    assert again.stop("letmein")["status"] == "abandoned"
    assert again.suspended_session() is None
    again.close()


def test_hand_edited_hosts_file_is_logged_and_repaired(app, clock) -> None:
    app.start_session("flow")
    app.hosts.unblock(["youtube.com"])
    run_for(app, clock, 30)
    assert "youtube.com" in app.hosts.blocked()
    cheats = app.cheats()
    assert cheats[0]["kind"] == "hosts_tampered" and cheats[0]["detail"] == "youtube.com"


def test_legit_changes_are_not_tampering(tmp_path, hosts, clock, events) -> None:
    app = make_app(tmp_path, hosts, clock, events)
    app.reconcile()
    app.unblock_now()
    app.reconcile()
    app.remove_domains(["youtube.com"])
    app.db.add_ledger(600, "earned", clock.now())
    app.spend_reward(5)
    run_for(app, clock, 300)
    assert "hosts_tampered" not in kinds(app)
    assert app.stats(7)["cheats_total"] == 0
    app.close()
