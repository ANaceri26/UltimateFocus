import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .domains import unique
from .errors import HostsCorruptError
from .fsutil import atomic_write_bytes

BEGIN = "# >>> FocusLock BEGIN - managed by FocusLock, do not edit inside this block >>>"
END = "# <<< FocusLock END <<<"
SINK = "0.0.0.0"
SINK_ADDRESSES = frozenset({"0.0.0.0", "127.0.0.1", "::", "::1"})


def _fields(line: str) -> list[str]:
    return line.split("#", 1)[0].split()


def decode(raw: bytes) -> str:
    """surrogateescape keeps bytes that are not valid UTF-8, so a rewrite never corrupts them."""
    return raw.decode("utf-8", errors="surrogateescape")


def encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


@dataclass
class HostsDocument:
    """The hosts file split into: lines before our block, our domains, lines after it.

    Everything outside the BEGIN/END markers belongs to the user or other programs and is
    written back exactly as it was read.
    """

    head: list[str] = field(default_factory=list)
    managed: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)
    newline: str = "\n"

    @classmethod
    def parse(cls, text: str) -> "HostsDocument":
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()
        begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
        ends = [i for i, line in enumerate(lines) if line.strip() == END]
        if not begins and not ends:
            return cls(head=lines, newline=newline)
        if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
            raise HostsCorruptError(
                "the FocusLock markers in the hosts file are damaged; "
                "fix them by hand or run `focuslock restore`"
            )
        start, stop = begins[0], ends[0]
        managed = []
        for line in lines[start + 1 : stop]:
            parts = _fields(line)
            if len(parts) >= 2 and parts[0] in SINK_ADDRESSES:
                managed.extend(part.lower() for part in parts[1:])
        return cls(lines[:start], unique(managed), lines[stop + 1 :], newline)

    def render(self) -> str:
        lines = list(self.head)
        if self.managed:
            lines += [BEGIN, *(f"{SINK} {domain}" for domain in self.managed), END]
        lines += self.tail
        return self.newline.join(lines) + self.newline if lines else ""

    def unmanaged_blocks(self) -> list[str]:
        found = []
        for line in (*self.head, *self.tail):
            parts = _fields(line)
            if len(parts) >= 2 and parts[0] in SINK_ADDRESSES:
                found.extend(part.lower() for part in parts[1:])
        return unique(found)


class HostsManager:
    def __init__(
        self,
        path: str | Path,
        backup_dir: str | Path,
        flush: Callable[[], object] | None = None,
    ) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir)
        self._flush = flush
        self._lock = threading.Lock()

    @property
    def original_backup(self) -> Path:
        return self.backup_dir / "hosts.original"

    @property
    def previous_backup(self) -> Path:
        return self.backup_dir / "hosts.previous"

    def _read_raw(self) -> bytes:
        return self.path.read_bytes() if self.path.exists() else b""

    def read(self) -> HostsDocument:
        return HostsDocument.parse(decode(self._read_raw()))

    def blocked(self) -> list[str]:
        return list(self.read().managed)

    def set_managed(self, domains: Iterable[str]) -> tuple[list[str], list[str]]:
        wanted = unique(domains)
        wanted_set = set(wanted)

        def compute(current: list[str]) -> list[str]:
            present = set(current)
            kept = [domain for domain in current if domain in wanted_set]
            return kept + [domain for domain in wanted if domain not in present]

        return self._update(compute)

    def block(self, domains: Iterable[str]) -> list[str]:
        new = unique(domains)
        added, _ = self._update(lambda current: unique([*current, *new]))
        return added

    def unblock(self, domains: Iterable[str] | None = None) -> list[str]:
        if domains is None:
            _, removed = self._update(lambda current: [])
            return removed
        drop = set(domains)
        _, removed = self._update(lambda current: [d for d in current if d not in drop])
        return removed

    def adopt(self, domains: Iterable[str]) -> list[str]:
        """Move lines like '0.0.0.0 youtube.com' (from block_youtube.py) into our block."""
        targets = set(domains)
        with self._lock:
            raw = self._read_raw()
            doc = HostsDocument.parse(decode(raw))
            adopted: list[str] = []

            def strip(lines: list[str]) -> list[str]:
                kept = []
                for line in lines:
                    parts = _fields(line)
                    names = [part.lower() for part in parts[1:]]
                    if len(parts) >= 2 and parts[0] in SINK_ADDRESSES and set(names) <= targets:
                        adopted.extend(names)
                    else:
                        kept.append(line)
                return kept

            doc.head, doc.tail = strip(doc.head), strip(doc.tail)
            if adopted:
                doc.managed = unique([*doc.managed, *adopted])
                self._commit(raw, doc)
            return unique(adopted)

    def restore_original(self) -> bool:
        if not self.original_backup.exists():
            return False
        with self._lock:
            atomic_write_bytes(self.path, self.original_backup.read_bytes())
            if self._flush:
                self._flush()
        return True

    def _update(self, compute: Callable[[list[str]], list[str]]) -> tuple[list[str], list[str]]:
        with self._lock:
            raw = self._read_raw()
            doc = HostsDocument.parse(decode(raw))
            before = list(doc.managed)
            after = unique(compute(before))
            before_set, after_set = set(before), set(after)
            added = [domain for domain in after if domain not in before_set]
            removed = [domain for domain in before if domain not in after_set]
            if added or removed:
                doc.managed = after
                self._commit(raw, doc)
            return added, removed

    def _commit(self, raw_before: bytes, doc: HostsDocument) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if not self.original_backup.exists():
            self.original_backup.write_bytes(raw_before)
        self.previous_backup.write_bytes(raw_before)
        atomic_write_bytes(self.path, encode(doc.render()))
        if self._flush:
            self._flush()
