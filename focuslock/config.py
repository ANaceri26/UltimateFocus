import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .domains import normalize
from .errors import ConfigError, InvalidDomainError
from .fsutil import atomic_write_bytes

SETTINGS_FILE = "settings.json"
DEFAULT_BLOCKLIST = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")

_INT_LIMITS = {
    "focus_minutes": (1, 600),
    "break_interval_minutes": (5, 600),
    "break_minutes": (1, 120),
    "reward_focus_minutes": (1, 600),
    "reward_minutes": (0, 600),
    "min_reward_session_minutes": (0, 600),
    "daily_reward_limit_minutes": (0, 1440),
    "api_port": (1, 65535),
}


@dataclass(frozen=True)
class Settings:
    default_blocklist: tuple[str, ...] = DEFAULT_BLOCKLIST
    focus_minutes: int = 90
    break_interval_minutes: int = 90
    break_minutes: int = 10
    reward_focus_minutes: int = 90
    reward_minutes: int = 10
    min_reward_session_minutes: int = 25
    daily_reward_limit_minutes: int = 60
    block_outside_sessions: bool = True
    strict_mode: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    hosts_path: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "Settings":
        if not isinstance(data, dict):
            raise ConfigError("settings must be a JSON object")
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(f"unknown setting(s): {', '.join(unknown)}")
        return cls(**{name: _validate(name, value) for name, value in data.items()})

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["default_blocklist"] = list(self.default_blocklist)
        return data


def _validate(name: str, value: Any) -> Any:
    if name in _INT_LIMITS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{name} must be a whole number")
        low, high = _INT_LIMITS[name]
        if not low <= value <= high:
            raise ConfigError(f"{name} must be between {low} and {high}")
        return value
    if name in {"block_outside_sessions", "strict_mode"}:
        if not isinstance(value, bool):
            raise ConfigError(f"{name} must be true or false")
        return value
    if name == "default_blocklist":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError("default_blocklist must be a list of domain names")
        try:
            return tuple(normalize(item) for item in value)
        except InvalidDomainError as exc:
            raise ConfigError(f"default_blocklist: {exc}") from exc
    if name == "hosts_path":
        if value is not None and not isinstance(value, str):
            raise ConfigError("hosts_path must be a path or null")
        return value
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    return Settings.from_dict(data)


def save_settings(settings: Settings, path: str | Path) -> None:
    text = json.dumps(settings.to_dict(), indent=2) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"))
