"""Exercise the real Tk controls against an isolated app and hosts file."""

import tkinter as tk

import pytest

from focuslock.gui import FocusLockWindow


@pytest.fixture
def window(app):
    try:
        probe = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    probe.destroy()
    view = FocusLockWindow(app)
    view.root.withdraw()
    yield view
    view._unsubscribe()
    for job in (view._tick_job, view._transition_job):
        if job is not None:
            view.root.after_cancel(job)
    view.root.destroy()


@pytest.mark.parametrize("mode", ["timer", "flow", "goal"])
def test_fields_and_actions_follow_mode(window, app, mode):
    window.mode.set(mode)
    window._mode_changed()
    assert bool(window.minutes_row.winfo_manager()) == (mode == "timer")
    assert bool(window.goal_row.winfo_manager()) == (mode == "goal")
    window.goal.set("Finish the report")
    window._start()
    snap = app.engine.snapshot()
    assert snap["mode"] == mode
    assert snap["goal"] == ("Finish the report" if mode == "goal" else None)
    assert window.minutes_input.instate(["disabled"])
    assert all(button.instate(["disabled"]) for button in window.mode_buttons)
    assert not window.buttons["start"].winfo_manager()
    assert bool(window.buttons["complete"].winfo_manager()) == (mode == "goal")
    assert window.clock_caption.get() == ("TIME REMAINING" if mode == "timer" else "TIME FOCUSED")
    if mode == "flow":
        assert "Optional break reminder" in window.detail_var.get()
    window._stop()
    assert window.minutes_input.instate(["!disabled"])
    assert window.buttons["start"].winfo_manager()
    assert not window.buttons["stop"].winfo_manager()


def test_flow_ignores_invalid_hidden_duration(window):
    window.mode.set("flow")
    window._mode_changed()
    window.minutes.set("not a number")
    window._start()
    assert window.app.engine.snapshot()["mode"] == "flow"


def test_external_session_synchronizes_controls(window, app):
    app.start_session("goal", goal="A restored goal")
    window._refresh()
    assert window.mode.get() == "goal"
    assert window.goal.get() == "A restored goal"
    assert not window.minutes_row.winfo_manager()
    assert window.goal_row.winfo_manager()


def test_failed_domain_is_preserved(window, monkeypatch):
    monkeypatch.setattr("focuslock.gui.messagebox.showerror", lambda *args, **kwargs: None)
    window.new_domain.set("invalid domain!")
    window._add_domain()
    assert window.new_domain.get() == "invalid domain!"


def test_empty_stats_and_rewards(window):
    window._draw_stats()
    assert window.spend_button.instate(["disabled"])
    assert window.end_reward_button.instate(["disabled"])
    assert window.activity_hint.get() == "No protection events recorded."


def test_navigation_cancels_previous_transition(window):
    window._show_page("Stats")
    previous = window._transition_job
    window.reduce_motion.set(True)
    window._show_page("Rewards")
    assert window._transition_job is None
    assert previous not in window.root.tk.call("after", "info")
    assert window.pages["Rewards"].winfo_manager() == "place"
    assert not window.pages["Stats"].winfo_manager()


@pytest.mark.parametrize("page", ["Focus", "Blocklist", "Rewards", "Stats", "Preferences"])
def test_pages_fit_small_window(window, page):
    window.root.geometry("820x600")
    window.root.deiconify()
    window._show_page(page, animate=False)
    window.root.update()
    frame = window.pages[page]
    for child in frame.winfo_children():
        assert child.winfo_height() >= child.winfo_reqheight(), f"{page}: {child} shrunk"
        assert child.winfo_ismapped(), f"{page}: {child} was clipped"
        assert child.winfo_y() + child.winfo_height() <= frame.winfo_height(), page
