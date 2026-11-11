import json
from pathlib import Path

import pytest

from focuslock.__main__ import main
from focuslock.config import Settings, load_settings, save_settings
from focuslock.errors import ConfigError
from focuslock.logfmt import format_line, parse_line
from tests.conftest import ORIGINAL_HOSTS


def test_settings_defaults_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert load_settings(path) == Settings()
    custom = Settings(focus_minutes=50, strict_mode=True, default_blocklist=("twitch.tv",))
    save_settings(custom, path)
    assert load_settings(path) == custom


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"focus_minuets": 50}, "unknown setting"),
        ({"focus_minutes": 0}, "between"),
        ({"focus_minutes": True}, "whole number"),
        ({"strict_mode": "yes"}, "true or false"),
        ({"default_blocklist": ["ok.com", "not valid"]}, "default_blocklist"),
    ],
)
def test_settings_validation(tmp_path: Path, data: dict, message: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match=message):
        load_settings(path)


def test_settings_bad_json_reports_line(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{\n  "focus_minutes": 50,\n}')
    with pytest.raises(ConfigError, match=r"line \d+"):
        load_settings(path)


def test_logfmt_round_trip() -> None:
    line = format_line("session_ended", {"goal": 'lab "2" report', "ok": True, "n": 5})
    fields = parse_line(line)
    assert fields["event"] == "session_ended"
    assert fields["goal"] == 'lab "2" report'
    assert fields["ok"] == "true" and fields["n"] == "5"


def test_cli_end_to_end(tmp_path: Path, capsys) -> None:
    hosts = tmp_path / "hosts"
    hosts.write_bytes((ORIGINAL_HOSTS + "0.0.0.0 youtube.com\r\n").encode())
    base = ["--home", str(tmp_path / "home"), "--hosts", str(hosts)]
    assert main([*base, "init"]) == 0
    assert main([*base, "migrate"]) == 0
    assert main([*base, "add", "reddit.com"]) == 0
    assert main([*base, "block"]) == 0
    assert "0.0.0.0 reddit.com" in hosts.read_text()
    assert main([*base, "unblock"]) == 0
    assert "reddit.com" not in hosts.read_text()
    assert main([*base, "status"]) == 0
    assert main([*base, "stats", "--days", "3"]) == 0
    assert main([*base, "export-blocklist", str(tmp_path / "list.txt")]) == 0
    assert "youtube.com" in (tmp_path / "list.txt").read_text()
    assert main([*base, "add", "bad domain"]) == 1
    out = capsys.readouterr()
    assert "Moved 1 old entries" in out.out and "error:" in out.err


def test_cli_password_and_cheats(tmp_path: Path, capsys, monkeypatch) -> None:
    hosts = tmp_path / "hosts"
    hosts.write_bytes(ORIGINAL_HOSTS.encode())
    base = ["--home", str(tmp_path / "home"), "--hosts", str(hosts)]
    monkeypatch.setenv("FOCUSLOCK_NEW_PASSWORD", "friend-holds-this")
    assert main([*base, "password", "set"]) == 0
    assert main([*base, "block"]) == 0
    monkeypatch.setenv("FOCUSLOCK_PASSWORD", "guess")
    assert main([*base, "unblock"]) == 1
    assert "0.0.0.0 youtube.com" in hosts.read_text()
    monkeypatch.setenv("FOCUSLOCK_PASSWORD", "friend-holds-this")
    assert main([*base, "unblock"]) == 0
    assert main([*base, "cheats"]) == 0
    assert main([*base, "stats"]) == 0
    out = capsys.readouterr()
    assert "wrong password" in out.err
    assert "password_failed" in out.out and "password_override" in out.out
    assert "Cheats:         2" in out.out


def test_no_command_opens_the_window() -> None:
    from focuslock.__main__ import default_argv

    assert default_argv([]) == ["gui"]
    assert default_argv(["status"]) == ["status"]
