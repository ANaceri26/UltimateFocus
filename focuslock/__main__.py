import argparse
import getpass
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import __version__, system
from .api import TOKEN_FILE, ApiServer, load_or_create_token
from .app import FocusLockApp
from .client import ApiClient
from .config import SETTINGS_FILE, Settings, load_settings, save_settings
from .errors import AlreadyRunningError, FocusLockError, PasswordError
from .events import Event, EventBus, EventType
from .sessions import Mode, State

PRIVILEGED = {
    "add", "remove", "block", "unblock", "migrate", "restore",
    "session", "stop", "serve", "gui", "spend", "autostart",
}  # fmt: skip
LEGACY_DOMAINS = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")


def clock_text(seconds: int | None) -> str:
    if seconds is None:
        return "--:--"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def duration_text(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}h {rest // 60:02d}m" if hours else f"{rest // 60}m"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="focuslock", description="Block distractions, run focus sessions, earn reward time."
    )
    parser.add_argument("--version", action="version", version=f"FocusLock {__version__}")
    parser.add_argument(
        "--home", type=Path, help="data folder (default: %%LOCALAPPDATA%%\\FocusLock)"
    )
    parser.add_argument("--hosts", type=Path, help="hosts file to manage (default: the system one)")
    parser.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("init", help="write a default settings.json to the data folder")
    sub.add_parser("status", help="show session, blocking and reward status")
    sub.add_parser("list", help="show the blocklist")
    for name, text in (("add", "add domains to the blocklist"), ("remove", "remove domains")):
        command = sub.add_parser(name, help=text)
        command.add_argument("domains", nargs="+", help="e.g. youtube.com or a full URL")
        command.add_argument("--no-www", action="store_true", help="don't add/remove www. too")
    sub.add_parser("block", help="apply the blocklist to the hosts file now")
    sub.add_parser("unblock", help="remove FocusLock's block from the hosts file (emergency off)")
    sub.add_parser("migrate", help="adopt lines written by block_youtube.py into the managed block")
    sub.add_parser("restore", help="put back the hosts file saved before FocusLock first edited it")

    session = sub.add_parser(
        "session", help="run a focus session in this terminal (no mode: resume an unfinished one)"
    )
    session.add_argument("mode", nargs="?", choices=[mode.value for mode in Mode])
    session.add_argument("goal", nargs="?", help="what you want to finish (goal mode)")
    session.add_argument("--minutes", type=int, help="length of a timer session")

    sub.add_parser("stop", help="end the current or unfinished session (password if early)")
    password = sub.add_parser("password", help="set, change or remove the FocusLock password")
    password.add_argument("action", choices=["set", "clear", "status"])
    cheats = sub.add_parser("cheats", help="show the cheat log")
    cheats.add_argument("--limit", type=int, default=20)

    serve = sub.add_parser("serve", help="run in the background with the REST API")
    serve.add_argument("--port", type=int, help="override api_port from settings")
    gui = sub.add_parser("gui", help="open the desktop window")
    gui.add_argument("--api", action="store_true", help="also start the REST API")

    stats = sub.add_parser("stats", help="show focus statistics")
    stats.add_argument("--days", type=int, default=7)
    sub.add_parser("rewards", help="show the reward bank")
    spend = sub.add_parser("spend", help="spend reward minutes (unblocks sites, then re-blocks)")
    spend.add_argument("minutes", type=int)
    sub.add_parser("token", help="print the REST API token")
    export = sub.add_parser("export-blocklist", help="write the blocklist to a text file")
    export.add_argument("path", type=Path)
    autostart = sub.add_parser("autostart", help="start `focuslock serve` at Windows logon")
    autostart.add_argument("state", choices=["on", "off"])
    return parser


def home_of(args: argparse.Namespace) -> Path:
    return args.home or system.data_dir()


def settings_of(args: argparse.Namespace) -> Settings:
    return load_settings(home_of(args) / SETTINGS_FILE)


def needs_elevation(args: argparse.Namespace) -> bool:
    if args.command not in PRIVILEGED or args.hosts is not None:
        return False
    if settings_of(args).hosts_path is not None:
        return False
    return not system.is_admin()


@contextmanager
def opened(args: argparse.Namespace) -> Iterator[FocusLockApp]:
    app = FocusLockApp.open(home=args.home, hosts_path=args.hosts)
    try:
        yield app
    finally:
        app.close()


def read_only(args: argparse.Namespace, local: Callable[[FocusLockApp], Any], path: str) -> Any:
    """Use the database directly, or ask the running instance over HTTP if one owns it."""
    try:
        with opened(args) as app:
            return local(app)
    except AlreadyRunningError:
        settings = settings_of(args)
        client = ApiClient.from_home(home_of(args), settings.api_host, settings.api_port)
        return client.get(path)


def ask_password(app: FocusLockApp, action: str) -> str | None:
    """Prompt for the password only if one is set. FOCUSLOCK_PASSWORD is for scripts/tests."""
    if not app.password_required():
        return None
    return os.environ.get("FOCUSLOCK_PASSWORD") or getpass.getpass(
        f"FocusLock password to {action}: "
    )


def print_status(status: dict[str, Any]) -> None:
    session = status["session"]
    print(f"Session:   {session['state'].replace('_', ' ')}", end="")
    if session["state"] != State.IDLE:
        print(f" ({session['mode']}, focused {clock_text(session['focused_seconds'])}", end="")
        if session["remaining_seconds"] is not None:
            print(f", {clock_text(session['remaining_seconds'])} left", end="")
        print(")", end="")
    print()
    if session["state"] == "suspended":
        print("           unfinished: run `focuslock session` or open the GUI to continue it")
    print(f"Blocking:  {'ON' if status['enforcing'] else 'off'}  ({status['hosts_path']})")
    print(f"Password:  {'set' if status['password_set'] else 'not set (nothing is protected)'}")
    print(
        f"Blocklist: {len(status['blocklist'])} domains, {len(status['blocked_in_hosts'])} in hosts"
    )
    rewards = status["rewards"]
    print(f"Rewards:   {duration_text(rewards['balance_seconds'])} banked", end="")
    if rewards["window_remaining_seconds"]:
        print(f", unblocked for {clock_text(rewards['window_remaining_seconds'])}", end="")
    print()


def print_rewards(rewards: dict[str, Any]) -> None:
    rate = rewards["rate"]
    print(f"Balance:     {duration_text(rewards['balance_seconds'])}")
    used, limit = rewards["spent_today_seconds"], rewards["daily_limit_seconds"]
    print(f"Spent today: {duration_text(used)} of {duration_text(limit)}")
    print(
        f"Rate:        {rate['focus_minutes']} min focus -> {rate['reward_minutes']} min reward "
        f"(sessions of {rate['minimum_session_minutes']}+ min)"
    )


def print_stats(stats: dict[str, Any]) -> None:
    days = stats["days"]
    peak = max((day["focused_seconds"] for day in days), default=0) or 1
    for day in days:
        bar = "#" * round(30 * day["focused_seconds"] / peak)
        print(f"{day['day']}  {bar:<30} {duration_text(day['focused_seconds'])}")
    print()
    print(f"Sessions:       {stats['sessions']} ({stats['completion_rate']:.0%} completed)")
    print(f"Total focused:  {duration_text(stats['total_focused_seconds'])}")
    print(f"Streak:         {stats['current_streak']} day(s), best {stats['longest_streak']}")
    cheats = {kind: n for kind, n in stats["cheats"].items() if n}
    detail = ", ".join(f"{kind.replace('_', ' ')} {n}" for kind, n in cheats.items())
    print(f"Cheats:         {stats['cheats_total']}" + (f" ({detail})" if detail else ""))


def print_cheats(cheats: list[dict[str, Any]]) -> None:
    if not cheats:
        print("No cheats recorded. Nice.")
        return
    for cheat in cheats:
        when = cheat["created_at"][:16].replace("T", " ")
        print(f"{when}  {cheat['kind']:<24} {cheat['detail']}")


def print_event(event: Event) -> None:
    data = event.data
    if event.type == EventType.BREAK_STARTED:
        print(
            f"\n== BREAK ({clock_text(data['seconds'])}): stand up, stretch, walk, drink water =="
        )
    elif event.type == EventType.BREAK_ENDED:
        print("\n== Break over, back to work ==")
    elif event.type == EventType.BREAK_SUGGESTED:
        print("\n== You've focused a long time: consider a break ==")
    elif event.type == EventType.SESSION_ENDED:
        reward = (
            f", earned {duration_text(data['reward_seconds'])}" if data["reward_seconds"] else ""
        )
        print(
            f"\nSession {data['status']}: {duration_text(data['focused_seconds'])} focused{reward}"
        )
    elif event.type == EventType.HOSTS_UPDATED:
        print(f"\n[hosts] blocking {data['blocked']} domains")
    elif event.type == EventType.SESSION_RESUMED:
        print(f"\nResuming your unfinished {data['mode']} session")
    elif event.type == EventType.CHEAT_RECORDED:
        print(f"\n[cheat log] {data['kind'].replace('_', ' ')} {data['detail']}")


def cmd_init(args: argparse.Namespace) -> int:
    path = home_of(args) / SETTINGS_FILE
    if path.exists():
        print(f"{path} already exists")
        return 0
    save_settings(Settings(), path)
    print(f"Wrote {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print_status(read_only(args, lambda app: app.status(), "/status"))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    domains = read_only(args, lambda app: {"domains": app.blocklist()}, "/blocklist")["domains"]
    print("\n".join(domains) if domains else "(blocklist is empty)")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    with opened(args) as app:
        added = app.add_domains(args.domains, include_www=not args.no_www)
        print(f"Added: {', '.join(added)}" if added else "Nothing new to add")
        if not app.enforcing():
            print("Note: blocking is off right now; it applies during focus sessions")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    with opened(args) as app:
        password = ask_password(app, "remove sites")
        removed = app.remove_domains(args.domains, include_www=not args.no_www, password=password)
        print(f"Removed: {', '.join(removed)}" if removed else "None of those were blocked")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    with opened(args) as app:
        if not app.enforcing():
            print("Blocking is off while idle (block_outside_sessions is false).")
            print("Start a session, or set block_outside_sessions to true in settings.json.")
            return 1
        app.reconcile()
        print(f"Blocking {len(app.hosts.blocked())} domains in {app.hosts.path}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    with opened(args) as app:
        removed = app.unblock_now(ask_password(app, "unblock"))
        print(f"Removed {len(removed)} entries from {app.hosts.path}")
        if app.enforcing():
            print("FocusLock will block them again the next time it runs.")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    with opened(args) as app:
        targets = set(app.blocklist()) | set(LEGACY_DOMAINS)
        adopted = app.hosts.adopt(targets)
        if adopted:
            app.db.add_domains(adopted, app.clock.now())
            app.reconcile()
            print(f"Moved {len(adopted)} old entries into FocusLock's managed block")
        else:
            print("No old block_youtube.py entries found")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    with opened(args) as app:
        if not app.restore_hosts(ask_password(app, "restore the hosts file")):
            print("No backup yet: FocusLock has never changed this hosts file")
            return 1
        print(f"Restored {app.hosts.path} from {app.hosts.original_backup}")
        if app.enforcing():
            print("FocusLock will add its block again the next time it runs.")
    return 0


def run_session_loop(app: FocusLockApp) -> None:
    """Show the timer until the session ends. Ctrl+C asks to end early (password for
    timer/goal sessions); leaving without ending keeps the session for next time."""
    unsubscribe = app.bus.subscribe(EventBus.WILDCARD, print_event)
    try:
        while app.engine.state is not State.IDLE:
            app.tick()
            snap = app.engine.snapshot()
            if snap["state"] == State.ON_BREAK:
                line = f"on break      {clock_text(snap['break_remaining_seconds'])} left"
            else:
                line = f"focusing      {clock_text(snap['focused_seconds'])}"
                if snap["remaining_seconds"] is not None:
                    line += f"   {clock_text(snap['remaining_seconds'])} left"
            print(f"\r{line:<60}", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        handle_interrupt(app)
    finally:
        unsubscribe()


def handle_interrupt(app: FocusLockApp) -> None:
    if app.engine.mode is Mode.GOAL and input("Goal done? [y/N] ").strip().lower() == "y":
        app.complete_goal()
        return
    if not app.engine.stop_is_early:
        app.stop()
        return
    if app.password_required():
        prompt = "Password to end early (leave empty to keep the session for later): "
        password = os.environ.get("FOCUSLOCK_PASSWORD") or getpass.getpass(prompt)
        if not password:
            print("Session kept. It continues the next time FocusLock runs.")
            return
        try:
            app.stop(password)
        except PasswordError as exc:
            print(f"{exc}. Session kept for next time.")
        return
    if input("End this session early? [y/N] ").strip().lower() == "y":
        app.stop()
    else:
        print("Session kept. It continues the next time FocusLock runs.")


def cmd_session(args: argparse.Namespace) -> int:
    with opened(args) as app:
        if app.suspended_session() is not None:
            if args.mode:
                print("You have an unfinished session, so FocusLock is resuming it first.")
            app.resume_suspended()
        elif args.mode is None:
            print("Choose a mode: focuslock session timer|flow|goal")
            return 1
        else:
            app.start_session(args.mode, minutes=args.minutes, goal=args.goal)
            print(f"{args.mode} session started. Ctrl+C to end it.")
        run_session_loop(app)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    with opened(args) as app:
        row = app.suspended_session()
        mode = row["mode"] if row else app.engine.mode
        if row is None and mode is None:
            print("No session is running")
            return 1
        password = ask_password(app, "end the session early") if mode != Mode.FLOW else None
        summary = app.stop(password)
        print(f"Session {summary['status']}: {duration_text(summary['focused_seconds'])} focused")
    return 0


def cmd_password(args: argparse.Namespace) -> int:
    with opened(args) as app:
        if args.action == "status":
            print("A password is set." if app.password_required() else "No password is set.")
            return 0
        current = ask_password(app, "confirm it is you")
        if args.action == "clear":
            app.clear_password(current)
            print("Password removed: unblock, remove, restore and early stops are unprotected.")
            return 0
        new = os.environ.get("FOCUSLOCK_NEW_PASSWORD")
        if new is None:
            print("Let the person who will hold the password type it.")
            new = getpass.getpass("New password: ")
            if getpass.getpass("Repeat it: ") != new:
                print("The passwords did not match; nothing changed.")
                return 1
        app.set_password(new, current)
        print("Password set.")
    return 0


def cmd_cheats(args: argparse.Namespace) -> int:
    path = f"/cheats?limit={args.limit}"
    data = read_only(args, lambda app: {"cheats": app.cheats(args.limit)}, path)
    print_cheats(data["cheats"])
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    with opened(args) as app:
        server = ApiServer(app, app.settings.api_host, args.port or app.settings.api_port)
        server.start()
        print(f"FocusLock API on {server.url}  (token: {app.home / TOKEN_FILE})")
        print("Ctrl+C to stop.")
        if app.resume_suspended():
            print("Resumed your unfinished session.")
        app.reconcile()
        try:
            while True:
                app.tick()
                time.sleep(1)
        except KeyboardInterrupt:
            print("Stopping...")
        finally:
            server.stop()
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui import run_gui
    except ImportError as exc:
        print(f"error: the GUI needs tkinter ({exc})", file=sys.stderr)
        return 1
    try:
        with opened(args) as app:
            run_gui(app, with_api=args.api)
    except (FocusLockError, OSError) as exc:
        from .gui import show_error

        show_error(str(exc))
        return 1
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    print_stats(read_only(args, lambda app: app.stats(args.days), f"/stats?days={args.days}"))
    return 0


def cmd_rewards(args: argparse.Namespace) -> int:
    print_rewards(read_only(args, lambda app: app.rewards(), "/rewards"))
    return 0


def cmd_spend(args: argparse.Namespace) -> int:
    with opened(args) as app:
        app.spend_reward(args.minutes)
        print(f"Unblocked for {args.minutes} min. Keep this window open; Ctrl+C ends early.")
        try:
            while app.reward_window_remaining() > 0:
                app.tick()
                print(f"\rreward time   {clock_text(app.reward_window_remaining())} left  ", end="")
                time.sleep(1)
            app.tick()
        except KeyboardInterrupt:
            app.end_reward_window()
        print("\nReward time over: sites are blocked again.")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    print(load_or_create_token(home_of(args)))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    domains = read_only(args, lambda app: {"domains": app.blocklist()}, "/blocklist")["domains"]
    args.path.write_text("".join(f"{domain}\n" for domain in domains), encoding="utf-8")
    print(f"Wrote {len(domains)} domains to {args.path}")
    return 0


def cmd_autostart(args: argparse.Namespace) -> int:
    system.set_autostart(args.state == "on")
    print("Autostart enabled" if args.state == "on" else "Autostart disabled")
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": cmd_init,
    "status": cmd_status,
    "list": cmd_list,
    "add": cmd_add,
    "remove": cmd_remove,
    "block": cmd_block,
    "unblock": cmd_unblock,
    "migrate": cmd_migrate,
    "restore": cmd_restore,
    "session": cmd_session,
    "stop": cmd_stop,
    "password": cmd_password,
    "cheats": cmd_cheats,
    "serve": cmd_serve,
    "gui": cmd_gui,
    "stats": cmd_stats,
    "rewards": cmd_rewards,
    "spend": cmd_spend,
    "token": cmd_token,
    "export-blocklist": cmd_export,
    "autostart": cmd_autostart,
}


def default_argv(argv: list[str] | None) -> list[str]:
    """No command at all (e.g. double-clicking the .exe) opens the window."""
    argv = list(sys.argv[1:] if argv is None else argv)
    return argv or ["gui"]


def main(argv: list[str] | None = None) -> int:
    argv = default_argv(argv)
    args = build_parser().parse_args(argv)
    try:
        if needs_elevation(args):
            if system.relaunch_as_admin(["--pause", *argv]):
                print("Opened an administrator window to finish this command.")
                return 0
            hint = "run your terminal as administrator" if system.IS_WINDOWS else "use sudo"
            print(f"error: this command edits the hosts file; {hint}", file=sys.stderr)
            return 1
        return COMMANDS[args.command](args)
    except (FocusLockError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if args.pause:
            input("Press Enter to exit...")


if __name__ == "__main__":
    raise SystemExit(main())
