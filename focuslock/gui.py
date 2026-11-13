"""Tkinter desktop window.

Tkinter is single-threaded: only the main thread may touch widgets. Events can arrive
from the REST API's worker threads, so every event goes into a thread-safe queue.Queue
and the main thread drains it once a second (producer/consumer).
"""

import queue
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from .api import ApiServer
from .app import FocusLockApp
from .errors import FocusLockError, PasswordError
from .events import Event, EventBus, EventType
from .sessions import Mode, State

BACKGROUND = "#15191d"
SURFACE = "#1d2329"
SIDEBAR = "#111519"
TEXT = "#edf1f4"
MUTED = "#a4afb9"
ACCENT = "#83cbbb"
BAR = ACCENT
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
        width = min(1040, self.root.winfo_screenwidth() - 80)
        height = min(740, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{max(820, width)}x{max(600, height)}")
        self.root.minsize(820, 600)
        self.root.configure(background=BACKGROUND)
        self.events: queue.Queue[Event] = queue.Queue()
        self._unsubscribe = app.bus.subscribe(EventBus.WILDCARD, self.events.put)
        self.overlay: BreakOverlay | None = None
        self.status_text = tk.StringVar(value="Ready when you are.")
        self._shown_domains: list[str] = []
        self._ticks = 0
        self._transition_job: str | None = None
        self._tick_job: str | None = None
        self._current_page = ""
        self.reduce_motion = tk.BooleanVar(value=False)
        self._configure_style()
        self._build_shell()
        self._build_focus_tab()
        self._build_blocklist_tab()
        self._build_rewards_tab()
        self._build_stats_tab()
        self._build_preferences()
        self._build_menu()
        self._show_page("Focus", animate=False)
        self._refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick_job = self.root.after(200, self._tick)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            ".",
            font=("Segoe UI", 10),
            background=BACKGROUND,
            foreground=TEXT,
            fieldbackground=SURFACE,
            bordercolor="#38424b",
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            troughcolor=SIDEBAR,
        )
        style.configure("TFrame", background=BACKGROUND)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("TLabel", background=BACKGROUND, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI", 25, "bold"))
        style.configure("Card.TLabel", background=SURFACE)
        style.configure("Clock.TLabel", background=SURFACE, font=("Segoe UI", 52))
        style.configure("Caption.TLabel", background=SURFACE, foreground=MUTED)
        style.configure("Metric.TLabel", background=SURFACE, font=("Segoe UI", 28, "bold"))
        style.configure("TButton", padding=(16, 10), background="#2b343d", borderwidth=0)
        style.map(
            "TButton",
            background=[("active", "#37444e"), ("disabled", "#22292f")],
            foreground=[("disabled", "#687680")],
        )
        style.configure("Accent.TButton", background=ACCENT, foreground=SIDEBAR)
        style.map(
            "Accent.TButton",
            background=[("disabled", "#293b38"), ("active", "#a2dece")],
            foreground=[("disabled", "#82958f"), ("!disabled", SIDEBAR)],
        )
        style.configure(
            "Nav.TButton", background=SIDEBAR, foreground=MUTED, anchor="w", padding=(18, 13)
        )
        style.map("Nav.TButton", background=[("active", SURFACE)])
        style.configure("Selected.Nav.TButton", background="#243630", foreground=ACCENT)
        style.configure("Mode.TRadiobutton", padding=(16, 10), background=SURFACE)
        style.map(
            "Mode.TRadiobutton",
            background=[("selected", "#293d37")],
            foreground=[("disabled", "#687680"), ("selected", ACCENT)],
        )
        for widget in ("TEntry", "TSpinbox"):
            style.configure(widget, padding=9, insertcolor=TEXT, foreground=TEXT)
            style.map(
                widget,
                fieldbackground=[("disabled", "#22292f")],
                foreground=[("disabled", "#687680")],
            )
        style.configure("TCheckbutton", padding=6)
        style.map("TCheckbutton", background=[("active", BACKGROUND)])
        style.configure(
            "Horizontal.TProgressbar",
            background=ACCENT,
            troughcolor="#303a42",
            borderwidth=0,
            thickness=4,
        )
        style.configure(
            "Treeview",
            background=SURFACE,
            fieldbackground=SURFACE,
            foreground=TEXT,
            rowheight=30,
            borderwidth=0,
        )
        style.configure("Treeview.Heading", background="#29323a", padding=8)
        style.map("Treeview", background=[("selected", "#34564d")])

    def _build_shell(self) -> None:
        sidebar = tk.Frame(self.root, bg=SIDEBAR, width=188)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(
            sidebar,
            text="FocusLock",
            bg=SIDEBAR,
            fg=TEXT,
            font=("Segoe UI", 19, "bold"),
            anchor="w",
        ).pack(fill="x", padx=22, pady=(30, 4))
        tk.Label(
            sidebar,
            text="FOCUS & BLOCKING",
            bg=SIDEBAR,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", padx=22, pady=(0, 32))
        self.nav: dict[str, ttk.Button] = {}
        for index, name in enumerate(("Focus", "Blocklist", "Rewards", "Stats", "Preferences"), 1):
            button = ttk.Button(
                sidebar,
                text=name,
                style="Nav.TButton",
                command=lambda name=name: self._show_page(name),
            )
            button.pack(fill="x", padx=10, pady=3)
            self.nav[name] = button
            self.root.bind(f"<Control-Key-{index}>", lambda event, name=name: self._show_page(name))
        tk.Label(
            sidebar,
            text="Your time. Your attention.\nStored on this device.",
            bg=SIDEBAR,
            fg=MUTED,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(side="bottom", anchor="w", padx=22, pady=24)
        body = ttk.Frame(self.root)
        body.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(body)
        self.content.pack(fill="both", expand=True, padx=32, pady=(24, 12))
        self.pages: dict[str, ttk.Frame] = {}
        footer = ttk.Frame(body, padding=(32, 12))
        footer.pack(fill="x")
        self.status_label = ttk.Label(footer, textvariable=self.status_text, style="Muted.TLabel")
        self.status_label.pack(fill="x")
        footer.bind(
            "<Configure>", lambda e: self.status_label.configure(wraplength=max(100, e.width - 64))
        )

    def _page(self, name: str, title: str, subtitle: str) -> ttk.Frame:
        page = ttk.Frame(self.content)
        self.pages[name] = page
        ttk.Label(page, text=title, style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        caption = ttk.Label(page, text=subtitle, style="Muted.TLabel", wraplength=580)
        caption.pack(anchor="w", pady=(0, 18))
        page.bind("<Configure>", lambda e: caption.configure(wraplength=max(100, e.width)))
        return page

    def _show_page(self, name: str, *, animate: bool = True) -> None:
        if name == self._current_page:
            return
        if self._transition_job is not None:
            self.root.after_cancel(self._transition_job)
            self._transition_job = None
        for page in self.pages.values():
            page.place_forget()
        self._current_page = name
        for label, button in self.nav.items():
            button.configure(style="Selected.Nav.TButton" if label == name else "Nav.TButton")
        page = self.pages[name]
        page.place(x=0, y=0, relwidth=1, relheight=1)
        if name == "Stats":
            self.root.after_idle(self._draw_stats)
        if animate and not self.reduce_motion.get():
            self._slide_page(page, 0)

    def _slide_page(self, page: ttk.Frame, step: int) -> None:
        # A small, 144 ms eased slide. Cancelled when navigation changes.
        offset = round(12 * (1 - step / 9) ** 3)
        page.place_configure(y=offset)
        self._transition_job = (
            self.root.after(16, self._slide_page, page, step + 1) if step < 9 else None
        )

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        app_menu = tk.Menu(menu, tearoff=False)
        app_menu.add_command(label="Preferences", command=lambda: self._show_page("Preferences"))
        app_menu.add_separator()
        app_menu.add_command(label="Quit", command=self._on_close)
        menu.add_cascade(label="FocusLock", menu=app_menu)
        view = tk.Menu(menu, tearoff=False)
        for index, name in enumerate(self.pages, 1):
            view.add_command(
                label=name,
                accelerator=f"Ctrl+{index}",
                command=lambda name=name: self._show_page(name),
            )
        menu.add_cascade(label="View", menu=view)
        self.root.configure(menu=menu)

    def _build_focus_tab(self) -> None:
        frame = self._page("Focus", "Time to focus.", "Choose a session that fits the work ahead.")
        self.mode = tk.StringVar(value=Mode.TIMER.value)
        modes = ttk.Frame(frame)
        modes.pack(fill="x")
        self.mode_buttons = []
        for mode, label in ((Mode.TIMER, "Timer"), (Mode.FLOW, "Flow"), (Mode.GOAL, "Goal")):
            button = ttk.Radiobutton(
                modes,
                text=label,
                value=mode.value,
                variable=self.mode,
                style="Mode.TRadiobutton",
                command=self._mode_changed,
            )
            button.pack(side="left", padx=(0, 8))
            self.mode_buttons.append(button)
        self.mode_hint = tk.StringVar()
        ttk.Label(frame, textvariable=self.mode_hint, style="Muted.TLabel", wraplength=570).pack(
            anchor="w", pady=(12, 16)
        )
        self.form = ttk.Frame(frame, height=40)
        self.form.pack(fill="x", pady=(0, 12))
        self.form.pack_propagate(False)
        self.minutes = tk.StringVar(value=str(self.app.settings.focus_minutes))
        self.minutes_row = ttk.Frame(self.form)
        ttk.Label(self.minutes_row, text="Duration").pack(side="left", padx=(0, 12))
        self.minutes_input = ttk.Spinbox(
            self.minutes_row, from_=1, to=600, textvariable=self.minutes, width=6
        )
        self.minutes_input.pack(side="left")
        ttk.Label(self.minutes_row, text="minutes", style="Muted.TLabel").pack(side="left", padx=10)
        self.goal = tk.StringVar()
        self.goal_row = ttk.Frame(self.form)
        ttk.Label(self.goal_row, text="Your goal").pack(side="left", padx=(0, 12))
        self.goal_input = ttk.Entry(self.goal_row, textvariable=self.goal)
        self.goal_input.pack(side="left", fill="x", expand=True)
        card = ttk.Frame(frame, style="Card.TFrame", padding=(24, 14))
        card.pack(fill="x")
        self.clock_caption = tk.StringVar(value="READY TO BEGIN")
        ttk.Label(card, textvariable=self.clock_caption, style="Caption.TLabel").pack()
        self.clock_var = tk.StringVar(value="00:00")
        ttk.Label(card, textvariable=self.clock_var, style="Clock.TLabel").pack(pady=(0, 4))
        self.detail_var = tk.StringVar()
        detail = ttk.Label(
            card,
            textvariable=self.detail_var,
            style="Caption.TLabel",
            wraplength=520,
            justify="center",
        )
        detail.pack()
        card.bind("<Configure>", lambda e: detail.configure(wraplength=max(100, e.width - 48)))
        self.progress = ttk.Progressbar(card, maximum=100)
        self.progress.pack(fill="x", pady=(20, 0))
        self.action_row = ttk.Frame(frame)
        self.action_row.pack(fill="x", pady=12)
        self.buttons: dict[str, ttk.Button] = {}
        for key, label, command in (
            ("start", "Start session", self._start),
            ("break", "Take a break", lambda: self._run(self.app.take_break)),
            ("complete", "Goal completed", lambda: self._run(self.app.complete_goal)),
            ("stop", "End session", self._stop),
        ):
            self.buttons[key] = ttk.Button(
                self.action_row,
                text=label,
                command=command,
                style="Accent.TButton" if key in {"start", "complete"} else "TButton",
            )
        self._mode_changed()

    def _mode_changed(self) -> None:
        self.minutes_row.pack_forget()
        self.goal_row.pack_forget()
        mode = self.mode.get()
        if mode == Mode.TIMER:
            self.minutes_row.pack(fill="x")
            hint = "A fixed duration. Stay with your work until the timer finishes."
        elif mode == Mode.GOAL:
            self.goal_row.pack(fill="x")
            hint = "Work toward one outcome. Finish when your goal is complete."
        else:
            hint = "No time limit. Follow your flow, with optional break reminders."
        self.mode_hint.set(hint)

    def _build_blocklist_tab(self) -> None:
        frame = self._page(
            "Blocklist",
            "Fewer distractions.",
            "Choose the websites to keep out of your focus time.",
        )
        entry_row = ttk.Frame(frame)
        entry_row.pack(fill="x")
        self.new_domain = tk.StringVar()
        entry = ttk.Entry(entry_row, textvariable=self.new_domain)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self._add_domain())
        self.include_www = tk.BooleanVar(value=True)
        ttk.Checkbutton(entry_row, text="Include www", variable=self.include_www).pack(
            side="left", padx=8
        )
        ttk.Button(
            entry_row, text="Add website", command=self._add_domain, style="Accent.TButton"
        ).pack(side="left")
        ttk.Label(frame, text="Enter a domain, such as youtube.com", style="Muted.TLabel").pack(
            anchor="w", pady=(8, 18)
        )
        self.domain_count = tk.StringVar()
        ttk.Label(frame, textvariable=self.domain_count, style="Muted.TLabel").pack(
            anchor="w", pady=(0, 10)
        )
        list_row = ttk.Frame(frame)
        list_row.pack(fill="both", expand=True)
        self.domain_list = tk.Listbox(
            list_row,
            selectmode="extended",
            activestyle="none",
            bg=SURFACE,
            fg=TEXT,
            selectbackground="#34564d",
            selectforeground=TEXT,
            highlightthickness=1,
            highlightbackground="#38424b",
            highlightcolor=ACCENT,
            relief="flat",
            font=("Segoe UI", 12),
            borderwidth=12,
            exportselection=False,
        )
        scrollbar = ttk.Scrollbar(list_row, orient="vertical", command=self.domain_list.yview)
        self.domain_list.configure(yscrollcommand=scrollbar.set)
        self.domain_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")
        ttk.Button(frame, text="Remove selected", command=self._remove_domains).pack(
            anchor="e", pady=(16, 0)
        )

    def _build_rewards_tab(self) -> None:
        frame = self._page(
            "Rewards",
            "Take time for yourself.",
            "Spend earned minutes to temporarily open your blocked websites.",
        )
        card = ttk.Frame(frame, style="Card.TFrame", padding=24)
        card.pack(fill="x")
        ttk.Label(card, text="AVAILABLE REWARD TIME", style="Caption.TLabel").pack(anchor="w")
        self.balance_var = tk.StringVar()
        ttk.Label(card, textvariable=self.balance_var, style="Metric.TLabel").pack(
            anchor="w", pady=(6, 16)
        )
        self.reward_var = tk.StringVar()
        ttk.Label(card, textvariable=self.reward_var, style="Card.TLabel", justify="left").pack(
            anchor="w"
        )
        row = ttk.Frame(frame)
        row.pack(anchor="w", pady=24)
        ttk.Label(row, text="Use").pack(side="left")
        self.spend_minutes = tk.StringVar(value="10")
        ttk.Spinbox(row, from_=1, to=600, textvariable=self.spend_minutes, width=6).pack(
            side="left", padx=10
        )
        ttk.Label(row, text="minutes").pack(side="left", padx=(0, 16))
        self.spend_button = ttk.Button(
            row, text="Start reward time", command=self._spend, style="Accent.TButton"
        )
        self.spend_button.pack(side="left")
        self.end_reward_button = ttk.Button(
            row, text="End early", command=lambda: self._run(self.app.end_reward_window)
        )
        self.end_reward_button.pack(side="left", padx=8)
        self.reward_hint = tk.StringVar()
        ttk.Label(frame, textvariable=self.reward_hint, style="Muted.TLabel", wraplength=570).pack(
            anchor="w"
        )

    def _build_stats_tab(self) -> None:
        frame = self._page(
            "Stats", "See your progress.", "Small sessions add up. Here is your last seven days."
        )
        self.stats_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.stats_var, wraplength=570).pack(anchor="w", pady=(0, 16))
        self.chart = tk.Canvas(frame, height=170, bg=SURFACE, highlightthickness=0)
        self.chart.pack(fill="x")
        self.chart.bind("<Configure>", lambda e: self._draw_stats())
        ttk.Label(frame, text="Recent protection activity", font=("Segoe UI", 12, "bold")).pack(
            anchor="w", pady=(22, 10)
        )
        self.cheat_table = ttk.Treeview(frame, columns=("time", "event"), show="headings", height=2)
        self.cheat_table.heading("time", text="Time")
        self.cheat_table.heading("event", text="Activity")
        self.cheat_table.column("time", width=160, stretch=False)
        self.cheat_table.column("event", width=300)
        self.cheat_table.pack(fill="both", expand=True)
        self.activity_hint = tk.StringVar()
        ttk.Label(frame, textvariable=self.activity_hint, style="Muted.TLabel").pack(
            anchor="w", pady=(8, 0)
        )

    def _build_preferences(self) -> None:
        frame = self._page(
            "Preferences",
            "Make it your space.",
            "Interface preferences and your current session rules.",
        )
        ttk.Checkbutton(
            frame, text="Reduce motion (disable page transitions)", variable=self.reduce_motion
        ).pack(anchor="w")
        ttk.Label(frame, text="Applies to this window.", style="Muted.TLabel").pack(
            anchor="w", padx=6, pady=(0, 24)
        )
        card = ttk.Frame(frame, style="Card.TFrame", padding=24)
        card.pack(fill="x")
        settings = self.app.settings
        for title, value in (
            (
                "Break schedule",
                f"{settings.break_minutes} min every {settings.break_interval_minutes} min",
            ),
            ("Flow mode", "Optional break reminders only"),
            (
                "Reward rate",
                f"{settings.reward_minutes} min per {settings.reward_focus_minutes} min focused",
            ),
            ("Minimum reward session", f"{settings.min_reward_session_minutes} minutes"),
            ("Daily reward limit", f"{settings.daily_reward_limit_minutes} minutes"),
            ("Blocking while idle", "On" if settings.block_outside_sessions else "Off"),
            ("Strict breaks", "On" if settings.strict_mode else "Off"),
        ):
            row = ttk.Frame(card, style="Card.TFrame")
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=title, style="Caption.TLabel").pack(side="left")
            ttk.Label(row, text=value, style="Card.TLabel").pack(side="right")
        ttk.Label(
            frame,
            text=(
                "Session rules are shown here for reference. Change them in settings.json, "
                "then restart FocusLock."
            ),
            style="Muted.TLabel",
            wraplength=570,
        ).pack(anchor="w", pady=20)

    # actions ------------------------------------------------------------------------------

    def _run(self, action: Any, *args: Any) -> bool:
        succeeded = False
        try:
            action(*args)
            succeeded = True
        except PasswordError as exc:
            messagebox.showerror(
                "FocusLock", f"{exc}. This attempt is in the cheat log.", parent=self.root
            )
        except (FocusLockError, ValueError, OSError) as exc:
            messagebox.showerror("FocusLock", str(exc), parent=self.root)
        self._drain_events()
        self._refresh()
        return succeeded

    def _start(self) -> None:
        mode = self.mode.get()
        try:
            minutes = int(self.minutes.get()) if mode == Mode.TIMER else None
        except ValueError:
            messagebox.showerror("FocusLock", "Minutes must be a whole number", parent=self.root)
            return
        self._run(
            lambda: self.app.start_session(
                mode, minutes=minutes, goal=self.goal.get() if mode == Mode.GOAL else None
            )
        )

    def _add_domain(self) -> None:
        text = self.new_domain.get().strip()
        if text and self._run(
            lambda: self.app.add_domains([text], include_www=self.include_www.get())
        ):
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
        self._tick_job = self.root.after(1000, self._tick)

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
        active = state != State.IDLE
        if active and self.mode.get() != snap["mode"]:
            self.mode.set(snap["mode"])
            self._mode_changed()
        if active:
            self.goal.set(snap["goal"] or "")
            if snap["planned_seconds"] is not None:
                self.minutes.set(str(snap["planned_seconds"] // 60))
        for widget in (*self.mode_buttons, self.minutes_input, self.goal_input):
            widget.state(["disabled"] if active else ["!disabled"])
        self.progress.configure(value=0)
        if state == State.ON_BREAK:
            self.clock_caption.set("BREAK TIME")
            self.clock_var.set(clock_text(snap["break_remaining_seconds"]))
            self.detail_var.set("Step away for a moment. Your websites stay blocked.")
            if self.overlay is not None:
                self.overlay.set_remaining(snap["break_remaining_seconds"])
        elif state == State.IDLE:
            self.clock_caption.set("READY TO BEGIN")
            self.clock_var.set("00:00")
            self.detail_var.set("Start when you are ready.")
        else:
            shown = snap["remaining_seconds"]
            self.clock_caption.set("TIME REMAINING" if shown is not None else "TIME FOCUSED")
            self.clock_var.set(clock_text(shown if shown is not None else snap["focused_seconds"]))
            if snap["mode"] == Mode.FLOW:
                remaining = snap["next_break_in_seconds"]
                label = (
                    f"Optional break reminder in {clock_text(remaining)}"
                    if remaining
                    else "A break is available whenever you need one."
                )
            else:
                label = f"Next break in {clock_text(snap['next_break_in_seconds'])}"
            if snap["mode"] == Mode.GOAL and snap["goal"]:
                label = f"{snap['goal']}\n{label}"
            self.detail_var.set(label)
            if shown is not None:
                total = shown + snap["focused_seconds"]
                self.progress.configure(value=100 * snap["focused_seconds"] / max(1, total))
        if active and snap["mode"] == Mode.TIMER:
            if not self.progress.winfo_manager():
                self.progress.pack(fill="x", pady=(20, 0))
        else:
            self.progress.pack_forget()
        enabled = {
            "start": not active,
            "break": state == State.FOCUSING,
            "complete": active and snap["mode"] == Mode.GOAL,
            "stop": active,
        }
        for key, button in self.buttons.items():
            if enabled[key]:
                if not button.winfo_manager():
                    button.pack(side="left", padx=(0, 8))
            else:
                button.pack_forget()
        domains = self.app.blocklist()
        if domains != self._shown_domains:
            self.domain_list.delete(0, "end")
            for domain in domains:
                self.domain_list.insert("end", domain)
            self._shown_domains = domains
        rewards = self.app.rewards()
        window = rewards["window_remaining_seconds"]
        self.domain_count.set(f"{len(domains)} domains on your blocklist")
        self.balance_var.set(f"{rewards['balance_seconds'] // 60} minutes")
        self.reward_var.set(
            f"Used today: {rewards['spent_today_seconds'] // 60} of "
            f"{rewards['daily_limit_seconds'] // 60} minutes\n\n"
            + (
                f"Reward time remaining: {clock_text(window)}"
                if window
                else "No reward time running"
            )
        )
        available = min(
            rewards["balance_seconds"],
            rewards["daily_limit_seconds"] - rewards["spent_today_seconds"],
        )
        can_spend = not active and not window and available >= 60
        self.spend_button.state(["!disabled"] if can_spend else ["disabled"])
        self.end_reward_button.state(["!disabled"] if window else ["disabled"])
        if active:
            hint = "Finish your focus session before using reward time."
        elif window:
            hint = "Your websites will be blocked again when reward time ends."
        elif available < 60:
            hint = "Earn more time or wait for your daily spending limit to reset."
        else:
            hint = f"You can use up to {available // 60} minutes now."
        self.reward_hint.set(hint)

    def _draw_stats(self) -> None:
        stats = self.app.stats(7)
        chart = self.chart
        chart.delete("all")
        width = max(chart.winfo_width(), 400)
        height = int(chart.cget("height"))
        days = stats["days"]
        peak = max((day["focused_seconds"] for day in days), default=0) or 1
        slot = (width - 40) / max(1, len(days))
        baseline = height - 28
        for index, day in enumerate(days):
            x0 = 20 + index * slot + 8
            x1 = 20 + (index + 1) * slot - 8
            bar = (baseline - 30) * day["focused_seconds"] / peak
            chart.create_rectangle(x0, baseline - bar, x1, baseline, fill=BAR, width=0)
            centre = (x0 + x1) / 2
            chart.create_text(
                centre, baseline + 12, text=day["day"][5:], fill=MUTED, font=("Segoe UI", 9)
            )
            minutes = day["focused_seconds"] // 60
            chart.create_text(
                centre, baseline - bar - 10, text=f"{minutes}m", fill=TEXT, font=("Segoe UI", 9)
            )
        if not any(day["focused_seconds"] for day in days):
            chart.create_text(
                width / 2,
                65,
                text="No focus sessions this week.",
                fill=MUTED,
                font=("Segoe UI", 12),
            )
        self.stats_var.set(
            f"Streak {stats['current_streak']} day(s) (best {stats['longest_streak']})   "
            f"Completed {stats['completion_rate']:.0%}   "
            f"Total {stats['total_focused_seconds'] // 3600}h "
            f"{stats['total_focused_seconds'] % 3600 // 60}m   "
            f"Cheats {stats['cheats_total']}"
        )

        rows = self.app.cheats(50)
        children = self.cheat_table.get_children()
        if children:
            self.cheat_table.delete(*children)
        for entry in rows:
            self.cheat_table.insert(
                "",
                "end",
                values=(
                    datetime.fromisoformat(entry["created_at"])
                    .astimezone()
                    .strftime("%b %d, %H:%M"),
                    entry["kind"].replace("_", " ").capitalize(),
                ),
            )
        self.activity_hint.set(
            "Up to 50 recent events. Times are local." if rows else "No protection events recorded."
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
        for job in (self._transition_job, self._tick_job):
            if job is not None:
                self.root.after_cancel(job)
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
