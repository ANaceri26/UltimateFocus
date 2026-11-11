import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_BARE = re.compile(r"[A-Za-z0-9_.:/@+,-]+")
_PAIR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S*)')
_ESCAPE = re.compile(r"\\(.)")
_RESERVED = {"ts", "src", "event"}


def quote(value: Any) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif value is None:
        text = ""
    else:
        text = str(value)
    if text and _BARE.fullmatch(text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def format_line(
    event: str, fields: dict[str, Any], at: datetime | None = None, source: str = "app"
) -> str:
    """One event per line as key=value pairs ("logfmt"): easy for humans, grep and Perl."""
    moment = (at or datetime.now(UTC)).astimezone(UTC)
    parts = [f"ts={moment:%Y-%m-%dT%H:%M:%SZ}", f"src={quote(source)}", f"event={quote(event)}"]
    parts += [f"{key}={quote(value)}" for key, value in fields.items() if key not in _RESERVED]
    return " ".join(parts)


def _unescape(match: re.Match[str]) -> str:
    char = match.group(1)
    return "\n" if char == "n" else char


def parse_line(line: str) -> dict[str, str]:
    result = {}
    for key, raw in _PAIR.findall(line):
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = _ESCAPE.sub(_unescape, raw[1:-1])
        result[key] = raw
    return result


class EventLog:
    """Append-only event log; rotates at max_bytes and keeps `backups` old files."""

    def __init__(self, path: str | Path, max_bytes: int = 1_000_000, backups: int = 5) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(
            self.path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def write(self, event: str, fields: dict[str, Any], at: datetime | None = None) -> None:
        record = logging.makeLogRecord({"msg": format_line(event, fields, at), "levelno": 20})
        self._handler.handle(record)

    def close(self) -> None:
        self._handler.close()
