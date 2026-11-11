from pathlib import Path

import pytest

from focuslock.errors import HostsCorruptError
from focuslock.hosts import BEGIN, END, HostsDocument, HostsManager
from tests.conftest import ORIGINAL_HOSTS


def test_parse_and_render_round_trip_without_block() -> None:
    doc = HostsDocument.parse(ORIGINAL_HOSTS)
    assert doc.managed == []
    assert doc.render() == ORIGINAL_HOSTS


def test_block_adds_marked_section_and_keeps_crlf(hosts: HostsManager, hosts_file: Path) -> None:
    assert hosts.block(["youtube.com", "youtu.be"]) == ["youtube.com", "youtu.be"]
    text = hosts_file.read_bytes().decode()
    assert text.startswith(ORIGINAL_HOSTS)
    assert f"{BEGIN}\r\n0.0.0.0 youtube.com\r\n0.0.0.0 youtu.be\r\n{END}\r\n" in text
    assert "\n" not in text.replace("\r\n", "")


def test_block_is_idempotent(hosts: HostsManager, hosts_file: Path) -> None:
    hosts.block(["youtube.com"])
    first = hosts_file.read_bytes()
    assert hosts.block(["youtube.com"]) == []
    assert hosts_file.read_bytes() == first


def test_unblock_all_restores_original_bytes(hosts: HostsManager, hosts_file: Path) -> None:
    hosts.block(["youtube.com", "netflix.com"])
    assert sorted(hosts.unblock()) == ["netflix.com", "youtube.com"]
    assert hosts_file.read_bytes().decode() == ORIGINAL_HOSTS


def test_unblock_some(hosts: HostsManager) -> None:
    hosts.block(["a.com", "b.com", "c.com"])
    assert hosts.unblock(["b.com", "zzz.com"]) == ["b.com"]
    assert hosts.blocked() == ["a.com", "c.com"]


def test_set_managed_reports_difference(hosts: HostsManager) -> None:
    hosts.block(["a.com", "b.com"])
    added, removed = hosts.set_managed(["b.com", "c.com"])
    assert (added, removed) == (["c.com"], ["a.com"])
    assert hosts.blocked() == ["b.com", "c.com"]


def test_flush_runs_only_when_file_changes(hosts_file: Path, tmp_path: Path) -> None:
    calls = []
    manager = HostsManager(hosts_file, tmp_path / "b", flush=lambda: calls.append(1))
    manager.block(["a.com"])
    manager.block(["a.com"])
    assert len(calls) == 1


def test_original_backup_is_written_once(hosts: HostsManager, hosts_file: Path) -> None:
    hosts.block(["a.com"])
    hosts.block(["b.com"])
    assert hosts.original_backup.read_bytes().decode() == ORIGINAL_HOSTS
    assert "a.com" in hosts.previous_backup.read_text()
    assert hosts.restore_original()
    assert hosts_file.read_bytes().decode() == ORIGINAL_HOSTS


def test_adopt_moves_legacy_lines_from_block_youtube(hosts_file: Path, tmp_path: Path) -> None:
    legacy = ORIGINAL_HOSTS + "0.0.0.0 youtube.com\n0.0.0.0 youtu.be\n0.0.0.0 keep.me other.com\n"
    hosts_file.write_bytes(legacy.encode())
    manager = HostsManager(hosts_file, tmp_path / "b")
    assert manager.adopt({"youtube.com", "youtu.be", "other.com"}) == ["youtube.com", "youtu.be"]
    doc = manager.read()
    assert doc.managed == ["youtube.com", "youtu.be"]
    assert "0.0.0.0 keep.me other.com" in doc.head


def test_damaged_markers_raise(hosts_file: Path, hosts: HostsManager) -> None:
    hosts_file.write_bytes(f"{BEGIN}\n0.0.0.0 a.com\n".encode())
    with pytest.raises(HostsCorruptError):
        hosts.blocked()


def test_non_utf8_bytes_survive_a_rewrite(hosts_file: Path, hosts: HostsManager) -> None:
    hosts_file.write_bytes(b"# caf\xe9 (latin-1 byte)\n127.0.0.1 localhost\n")
    hosts.block(["a.com"])
    hosts.unblock()
    assert hosts_file.read_bytes() == b"# caf\xe9 (latin-1 byte)\n127.0.0.1 localhost\n"


def test_missing_file_is_treated_as_empty(tmp_path: Path) -> None:
    manager = HostsManager(tmp_path / "nope", tmp_path / "b")
    assert manager.blocked() == []
    manager.block(["a.com"])
    assert manager.blocked() == ["a.com"]
