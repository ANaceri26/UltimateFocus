"""Tkinter desktop window.

Tkinter is single-threaded: only the main thread may touch widgets. Events can arrive
from the REST API's worker threads, so every event goes into a thread-safe queue.Queue
and the main thread drains it once a second (producer/consumer).
"""

import queue
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from .api import ApiServer
from .app import FocusLockApp
from .errors import FocusLockError, PasswordError
from .events import Event, EventBus, EventType
from .sessions import Mode, State

BACKGROUND = "#101418"
ACCENT = "#7fd1b9"
BAR = "#4c9be8"
TIPS = (
    "Stand up and stretch your back, neck and shoulders.",
    "Walk around for a few minutes.",
    "Drink a glass of water.",
    "Look at something about 6 m away for 20 seconds.",
)


def clock_text(seconds: int | None) -> str:
    if seconds is None:
        return "--:--"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class BreakOverlay(tk.Toplevel):
    def __init__(self, master: tk.Misc, *, skippable: bool, on_skip: Any) -> None:
        super().__init__(master)
        self.configure(background=BACKGROUND)
        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", on_skip if skippable else self._ignore)
        self._remaining = tk.StringVar(value="")
        label = {"bg": BACKGROUND, "fg": "#f2f2f2"}
        tk.Label(self, text="Break time", font=("Segoe UI", 40, "bold"), **label).pack(
            pady=(140, 16)
        )
        tk.Label(
            self, textvariable=self._remaining, font=("Segoe UI", 64), bg=BACKGROUND, fg=ACCENT
        ).pack()
        for tip in TIPS:
            tk.Label(self, text=tip, font=("Segoe UI", 16), **label).pack(pady=4)
        if skippable:
            tk.Button(self, text="Skip break", font=("Segoe UI", 12), command=on_skip).pack(pady=40)
            self.bind("<Escape>", lambda _event: on_skip())
        else:
            tk.Label(
                self, text="Strict mode: this break cannot be skipped.", font=("Segoe UI", 12),
                **label,
            ).pack(pady=40)  # fmt: skip
        self.focus_force()

    def _ignore(self) -> None:
        return None

    def set_remaining(self, seconds: int | None) -> None:
        self._remaining.set(clock_text(seconds))


class FocusLockWindow:
    def __init__(self, app: FocusLockApp) -> None:
        self.app = app
        self.root = tk.Tk()
        self.root.title("FocusLock")
        self.root.geometry("580x480")
        self.root.minsize(500, 420)
        self.events: queue.Queue[Event] = queue.Queue()
        self._unsubscribe = app.bus.subscribe(EventBus.WILDCARD, self.events.put)
        self.overlay: BreakOverlay | None = None
        self.status_text = tk.StringVar(value="Ready")
        self._shown_domains: list[str] = []
        self._ticks = 0

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self._build_focus_tab()
        self._build_blocklist_tab()
        self._build_rewards_tab()
        self._build_stats_tab()
        ttk.Label(self.root, textvariable=self.status_text, anchor="w").pack(
            fill="x", padx=12, pady=(0, 8)
        )
        self.notebook.bind("<<NotebookTabChanged>>", lambda _event: self._draw_stats())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._tick)

    # tabs ---------------------------------------------------------------------------------

    def _build_focus_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(frame, text="Focus")
        self.mode = tk.StringVar(value=Mode.TIMER.value)
        modes = ttk.Frame(frame)
        modes.pack(anchor="w")
        for mode, label in ((Mode.TIMER, "Timer"), (Mode.FLOW, "Flow"), (Mode.GOAL, "Goal")):
            ttk.Radiobutton(modes, text=label, value=mode.value, variable=self.mode).pack(
                side="left", padx=(0, 14)
            )
        form = ttk.Frame(frame)
        form.pack(anchor="w", pady=10)
        ttk.Label(form, text="Minutes").grid(row=0, column=0, sticky="w")
        self.minutes = tk.StringVar(value=str(self.app.settings.focus_minutes))
        ttk.Spinbox(form, from_=1, to=600, textvariable=self.minutes, width=6).grid(
            row=0, column=1, sticky="w", padx=8
        )
        ttk.Label(form, text="Goal").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.goal = tk.StringVar()
        ttk.Entry(form, textvariable=self.goal, width=42).grid(
            row=1, column=1, sticky="w", padx=8, pady=(8, 0)
        )
        self.clock_var = tk.StringVar(value="00:00")
        ttk.Label(frame, textvariable=self.clock_var, font=("Segoe UI", 40)).pack(pady=(14, 4))
        self.detail_var = tk.StringVar(value="Pick a mode and press Start")
        ttk.Label(frame, textvariable=self.detail_var).pack()
        row = ttk.Frame(frame)
        row.pack(pady=14)
        self.buttons: dict[str, ttk.Button] = {}
        for key, label, command in (
            ("start", "Start", self._start),
            ("break", "Take break", lambda: self._run(self.app.take_break)),
            ("complete", "Goal done", lambda: self._run(self.app.complete_goal)),
            ("stop", "Stop", self._stop),
        ):
            button = ttk.Button(row, text=label, command=command)
            button.pack(side="left", padx=3)
            self.buttons[key] = button

    def _build_blocklist_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(frame, text="Blocklist")
        entry_row = ttk.Frame(frame)
        entry_row.pack(fill="x")
        self.new_domain = tk.StringVar()
        entry = ttk.Entry(entry_row, textvariable=self.new_domain)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self._add_domain())
        self.include_www = tk.BooleanVar(value=True)
        ttk.Checkbutton(entry_row, text="+ www.", variable=self.include_www).pack(
            side="left", padx=6
        )
        ttk.Button(entry_row, text="Add", command=self._add_domain).pack(side="left")
        list_row = ttk.Frame(frame)
        list_row.pack(fill="both", expand=True, pady=10)
        self.domain_list = tk.Listbox(list_row, selectmode="extended", activestyle="none")
        scrollbar = ttk.Scrollbar(list_row, orient="vertical", command=self.domain_list.yview)
        self.domain_list.configure(yscrollcommand=scrollbar.set)
        self.domain_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")
        ttk.Button(frame, text="Remove selected", command=self._remove_domains).pack(anchor="e")

    def _build_rewards_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(frame, text="Rewards")
        self.reward_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.reward_var, justify="left", font=("Segoe UI", 11)).pack(
            anchor="w"
        )
        row = ttk.Frame(frame)
        row.pack(anchor="w", pady=14)
        ttk.Label(row, text="Spend minutes").pack(side="left")
        self.spend_minutes = tk.StringVar(value="10")
        ttk.Spinbox(row, from_=1, to=600, textvariable=self.spend_minutes, width=6).pack(
            side="left", padx=8
        )
        ttk.Button(row, text="Spend", command=self._spend).pack(side="left")
        ttk.Button(
            row, text="End early", command=lambda: self._run(self.app.end_reward_window)
        ).pack(side="left", padx=6)

    def _build_stats_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(frame, text="Stats")
        self.chart = tk.Canvas(frame, height=240, highlightthickness=0)
        self.chart.pack(fill="x")
        self.stats_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.stats_var).pack(anchor="w", pady=8)
        ttk.Button(frame, text="Refresh", command=self._draw_stats).pack(anchor="e")

    # actions ------------------------------------------------------------------------------

    def _run(self, action: Any, *args: Any) -> None:
        try:
            action(*args)
        except PasswordError as exc:
            messagebox.showerror(
                "FocusLock", f"{exc}. This attempt is in the cheat log.", parent=self.root
            )
        except (FocusLockError, ValueError, OSError) as exc:
            messagebox.showerror("FocusLock", str(exc), parent=self.root)
        self._drain_events()
        self._refresh()

    def _start(self) -> None:
        mode = self.mode.get()
        try:
            minutes = int(self.minutes.get()) if mode == Mode.TIMER else None
        except ValueError:
            messagebox.showerror("FocusLock", "Minutes must be a whole number", parent=self.root)
            return
        self._run(lambda: self.app.start_session(mode, minutes=minutes, goal=self.goal.get()))

    def _add_domain(self) -> None:
        text = self.new_domain.get().strip()
        if text:
            self._run(lambda: self.app.add_domains([text], include_www=self.include_www.get()))
            self.new_domain.set("")

    def _ask_password(self, action: str) -> str | None:
        """None means 'no password needed'; '' means the user cancelled."""
        if not self.app.password_required():
            return None
        answer = simpledialog.askstring(
            "FocusLock", f"Password to {action}:", show="*", parent=self.root
        )
        return answer or ""

    def _remove_domains(self) -> None:
        chosen = [self.domain_list.get(index) for index in self.domain_list.curselection()]
        if not chosen:
            return
        password = self._ask_password("remove sites")
        if password == "":
            return
        self._run(lambda: self.app.remove_domains(chosen, include_www=False, password=password))

    def _stop(self) -> None:
        password = None
        if self.app.engine.stop_is_early:
            password = self._ask_password("end this session early")
            if password == "":
                return
        self._run(lambda: self.app.stop(password))

    def _spend(self) -> None:
        try:
            minutes = int(self.spend_minutes.get())
        except ValueError:
            messagebox.showerror("FocusLock", "Minutes must be a whole number", parent=self.root)
            return
        self._run(self.app.spend_reward, minutes)

    def _skip_break(self) -> None:
        self._run(self.app.skip_break)

    # loop ---------------------------------------------------------------------------------

    def _tick(self) -> None:
        try:
            self.app.tick()
        except Exception as exc:
            self.status_text.set(f"Error: {exc}")
        self._drain_events()
        self._refresh()
        self._ticks += 1
        if self._ticks % 30 == 0:
            self._draw_stats()
        self.root.after(1000, self._tick)

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            self._handle_event(event)

    def _handle_event(self, event: Event) -> None:
        data = event.data
        if event.type == EventType.BREAK_STARTED:
            skippable = not (data.get("forced") and self.app.settings.strict_mode)
            self._close_overlay()
            self.overlay = BreakOverlay(self.root, skippable=skippable, on_skip=self._skip_break)
        elif event.type == EventType.BREAK_ENDED:
            self._close_overlay()
            self.status_text.set("Break over. Back to work.")
        elif event.type == EventType.BREAK_SUGGESTED:
            self.root.bell()
            self.status_text.set("You've focused a long time. Consider a break (Take break).")
        elif event.type == EventType.SESSION_ENDED:
            self._close_overlay()
            self.root.bell()
            reward = data["reward_seconds"] // 60
            self.status_text.set(
                f"Session {data['status']}: {data['focused_seconds'] // 60} min focused, "
                f"{reward} min reward earned"
            )
            self._draw_stats()
        elif event.type == EventType.HOSTS_UPDATED:
            self.status_text.set(f"Hosts file updated: {data['blocked']} domains blocked")
        elif event.type == EventType.CHEAT_RECORDED:
            self.status_text.set(f"Cheat log: {data['kind'].replace('_', ' ')} {data['detail']}")
        elif event.type == EventType.REWARD_WINDOW_ENDED:
            self.root.bell()
            self.status_text.set("Reward time is over. Sites are blocked again.")

    def _close_overlay(self) -> None:
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None

    def _refresh(self) -> None:
        snap = self.app.engine.snapshot()
        state = snap["state"]
        if state == State.ON_BREAK:
            self.clock_var.set(clock_text(snap["break_remaining_seconds"]))
            self.detail_var.set("On break")
            if self.overlay is not None:
                self.overlay.set_remaining(snap["break_remaining_seconds"])
        elif state == State.IDLE:
            self.clock_var.set("00:00")
            self.detail_var.set("Pick a mode and press Start")
        else:
            shown = snap["remaining_seconds"]
            self.clock_var.set(clock_text(shown if shown is not None else snap["focused_seconds"]))
            label = f"Focusing ({snap['mode']})"
            if snap["goal"]:
                label += f": {snap['goal']}"
            label += f"   next break in {clock_text(snap['next_break_in_seconds'])}"
            self.detail_var.set(label)
        enabled = {
            "start": state == State.IDLE,
            "break": state == State.FOCUSING,
            "complete": state != State.IDLE and snap["mode"] == Mode.GOAL,
            "stop": state != State.IDLE,
        }
        for key, button in self.buttons.items():
            button.state(["!disabled"] if enabled[key] else ["disabled"])
        domains = self.app.blocklist()
        if domains != self._shown_domains:
            self.domain_list.delete(0, "end")
            for domain in domains:
                self.domain_list.insert("end", domain)
            self._shown_domains = domains
        rewards = self.app.rewards()
        window = rewards["window_remaining_seconds"]
        self.reward_var.set(
            f"Balance:      {rewards['balance_seconds'] // 60} min\n"
            f"Spent today:  {rewards['spent_today_seconds'] // 60} of "
            f"{rewards['daily_limit_seconds'] // 60} min\n"
            f"Unblocked:    {clock_text(window) if window else 'no'}\n"
            f"Rate:         {rewards['rate']['focus_minutes']} min focus = "
            f"{rewards['rate']['reward_minutes']} min reward"
        )

    def _draw_stats(self) -> None:
        stats = self.app.stats(7)
        chart = self.chart
        chart.delete("all")
        width = max(chart.winfo_width(), 400)
        height = int(chart.cget("height"))
        days = stats["days"]
        peak = max((day["focused_seconds"] for day in days), default=0) or 1
        slot = (width - 40) / len(days)
        baseline = height - 28
        for index, day in enumerate(days):
            x0 = 20 + index * slot + 8
            x1 = 20 + (index + 1) * slot - 8
            bar = (baseline - 30) * day["focused_seconds"] / peak
            chart.create_rectangle(x0, baseline - bar, x1, baseline, fill=BAR, width=0)
            centre = (x0 + x1) / 2
            chart.create_text(centre, baseline + 12, text=day["day"][5:], font=("Segoe UI", 9))
            minutes = day["focused_seconds"] // 60
            chart.create_text(centre, baseline - bar - 10, text=f"{minutes}m", font=("Segoe UI", 9))
        self.stats_var.set(
            f"Streak {stats['current_streak']} day(s) (best {stats['longest_streak']})   "
            f"Completed {stats['completion_rate']:.0%}   "
            f"Total {stats['total_focused_seconds'] // 3600}h "
            f"{stats['total_focused_seconds'] % 3600 // 60}m   "
            f"Cheats {stats['cheats_total']}"
        )

    def _on_close(self) -> None:
        if self.app.engine.state is not State.IDLE and not messagebox.askyesno(
            "FocusLock",
            "A session is running. If you close FocusLock it stays blocked, the session "
            "continues next time you open it, and the close goes in the cheat log."
            "\n\nClose anyway?",
            parent=self.root,
        ):
            return
        self._unsubscribe()
        self.root.destroy()


def show_error(message: str) -> None:
    """Error dialog for when there is no main window (or no console, in FocusLock.exe)."""
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("FocusLock", message, parent=root)
    root.destroy()


def run_gui(app: FocusLockApp, *, with_api: bool = False) -> None:
    api = None
    if with_api:
        api = ApiServer(app, app.settings.api_host, app.settings.api_port)
        api.start()
    window = FocusLockWindow(app)
    try:
        if app.resume_suspended():
            window.status_text.set("Resumed your unfinished session")
        app.reconcile()
    except OSError as exc:
        window.status_text.set(f"Cannot edit the hosts file: {exc}")
    try:
        window.root.mainloop()
    finally:
        if api is not None:
            api.stop()
