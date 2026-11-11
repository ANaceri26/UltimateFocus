from collections.abc import Iterator
from pathlib import Path

import pytest

from focuslock.app import FocusLockApp
from focuslock.clock import FakeClock
from focuslock.config import Settings
from focuslock.db import Database
from focuslock.events import Event, EventBus
from focuslock.hosts import HostsManager

ORIGINAL_HOSTS = "# Copyright (c) Microsoft Corp.\r\n127.0.0.1 localhost\r\n::1 localhost\r\n"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def hosts_file(tmp_path: Path) -> Path:
    path = tmp_path / "hosts"
    path.write_bytes(ORIGINAL_HOSTS.encode())
    return path


@pytest.fixture
def hosts(hosts_file: Path, tmp_path: Path) -> HostsManager:
    return HostsManager(hosts_file, tmp_path / "backups")


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def events() -> list[Event]:
    return []


def make_app(
    tmp_path: Path, hosts: HostsManager, clock: FakeClock, events: list[Event], **overrides
) -> FocusLockApp:
    settings = Settings(**{"break_interval_minutes": 50, "break_minutes": 5, **overrides})
    bus = EventBus()
    bus.subscribe(EventBus.WILDCARD, events.append)
    return FocusLockApp(
        settings, Database(tmp_path / "test.db"), hosts, home=tmp_path, clock=clock, bus=bus
    )


@pytest.fixture
def app(tmp_path, hosts, clock, events) -> Iterator[FocusLockApp]:
    instance = make_app(tmp_path, hosts, clock, events, block_outside_sessions=False)
    yield instance
    instance.close()


def run_for(app: FocusLockApp, clock: FakeClock, seconds: int, step: int = 1) -> None:
    for _ in range(0, seconds, step):
        clock.advance(step)
        app.tick()
