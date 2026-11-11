"""Everything that talks to the operating system lives here, so the rest stays portable."""

import os
import subprocess
import sys
from pathlib import Path

from .errors import FocusLockError

IS_WINDOWS = sys.platform == "win32"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_hosts_path() -> Path:
    if IS_WINDOWS:
        root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        return Path(root) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def data_dir() -> Path:
    override = os.environ.get("FOCUSLOCK_HOME")
    if override:
        return Path(override)
    if IS_WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "FocusLock"
    return Path.home() / ".focuslock"


def is_admin() -> bool:
    if IS_WINDOWS:
        import ctypes

        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return os.geteuid() == 0


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def self_command(args: list[str]) -> list[str]:
    """The command line that runs FocusLock again: the .exe, or `python -m focuslock`."""
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, "-m", "focuslock", *args]


def relaunch_as_admin(args: list[str]) -> bool:
    """Ask UAC to start a new elevated copy of FocusLock. Returns False if that failed.

    ShellExecuteW with the "runas" verb shows the UAC prompt. It returns a value > 32 on
    success. The working folder is set to the project root so `-m focuslock` still finds
    the package (an elevated process otherwise starts in C:\\Windows\\System32).
    """
    if not IS_WINDOWS:
        return False
    import ctypes

    command = self_command(args)
    params = subprocess.list2cmdline(command[1:])
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", command[0], params, str(PROJECT_ROOT), 1
    )
    return int(result) > 32


def flush_dns() -> bool:
    if not IS_WINDOWS:
        return False
    result = subprocess.run(["ipconfig", "/flushdns"], capture_output=True, check=False)
    return result.returncode == 0


def set_autostart(enabled: bool, task_name: str = "FocusLock") -> None:
    """Run `focuslock serve` at logon via Task Scheduler, elevated without a UAC prompt."""
    if not IS_WINDOWS:
        raise FocusLockError("autostart is only supported on Windows")
    if enabled:
        command = [
            "schtasks", "/Create", "/TN", task_name,
            "/TR", subprocess.list2cmdline(self_command(["serve"])),
            "/SC", "ONLOGON", "/RL", "HIGHEST", "/F",
        ]  # fmt: skip
    else:
        command = ["schtasks", "/Delete", "/TN", task_name, "/F"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise FocusLockError((result.stderr or result.stdout).strip() or "schtasks failed")


class InstanceLock:
    """An OS-level lock file so only one FocusLock process owns the database and hosts file.

    The OS releases the lock automatically if the process crashes, unlike a plain
    "pid file" that can be left behind.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - must stay open while locked
        try:
            if IS_WINDOWS:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None
