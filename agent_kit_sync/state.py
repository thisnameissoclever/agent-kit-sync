"""The index of installed copies, the run journal, the lock, alerts, and held changes."""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .fsutil import atomic_write, read_or_none
from .paths import Layout

INDEX_VERSION = 1


def _load(path: Path, default):
    data = read_or_none(path)
    if data is None:
        return default
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return default


def _save(path: Path, data) -> None:
    atomic_write(path, (json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))


class Index:
    """What the tool last wrote, per installed copy and per setting."""

    def __init__(self, layout: Layout):
        self.layout = layout
        self.path = layout.state / "index.json"
        raw = _load(self.path, {})
        self.copies: Dict[str, dict] = dict(raw.get("copies", {}))
        self.settings: Dict[str, dict] = dict(raw.get("settings", {}))
        self.blocks: Dict[str, dict] = dict(raw.get("blocks", {}))
        self.items: Dict[str, dict] = dict(raw.get("items", {}))
        self.registrations: Dict[str, dict] = dict(raw.get("registrations", {}))
        self.hooks: List[dict] = list(raw.get("hooks", []))
        self.instruction_sigs: Dict[str, list] = dict(raw.get("instruction_sigs", {}))
        self.approvals: Dict[str, str] = dict(raw.get("approvals", {}))
        # Config repo paths this machine committed itself, with the committed Git file mode and the
        # SHA-256 of the committed bytes, so a later run can tell its own earlier commit from
        # another machine's.
        self.own_files: Dict[str, str] = dict(raw.get("own_files", {}))
        self.always_restore: List[str] = list(raw.get("always_restore", []))
        self.last_fetch: float = float(raw.get("last_fetch", 0))
        self._dropped_approvals: set = set()
        self._cleared_force: set = set()
        self._dropped_own_files: set = set()

    def drop_approval(self, key: str) -> None:
        self.approvals.pop(key, None)
        self._dropped_approvals.add(key)

    def drop_own_file(self, path: str) -> None:
        self.own_files.pop(path, None)
        self._dropped_own_files.add(path)

    def clear_force(self, key: str) -> None:
        info = self.items.get(key)
        if info is not None:
            info.pop("force", None)
        self._cleared_force.add(key)

    def rendered_path(self, copy_key: str) -> Path:
        import hashlib
        return self.layout.state / "rendered" / hashlib.sha256(copy_key.encode("utf-8")).hexdigest()

    def rendered(self, copy_key: str) -> Optional[bytes]:
        return read_or_none(self.rendered_path(copy_key))

    def put_copy(self, copy_key: str, entry: dict, data: bytes) -> None:
        atomic_write(self.rendered_path(copy_key), data)
        self.copies[copy_key] = entry

    def drop_copy(self, copy_key: str) -> None:
        self.copies.pop(copy_key, None)
        path = self.rendered_path(copy_key)
        if path.exists():
            path.unlink()

    def save(self) -> None:
        # Decisions written by other commands while this run held an older copy are kept.
        on_disk = _load(self.path, {})
        for key, value in dict(on_disk.get("approvals", {})).items():
            if key not in self.approvals and key not in self._dropped_approvals:
                self.approvals[key] = value
        for key, value in dict(on_disk.get("own_files", {})).items():
            if key not in self._dropped_own_files:
                self.own_files.setdefault(key, value)
        for key in on_disk.get("always_restore", []):
            if key not in self.always_restore:
                self.always_restore.append(key)
        for key, info in dict(on_disk.get("items", {})).items():
            mine = self.items.get(key)
            if mine is not None and info.get("force") and not mine.get("force") and key not in self._cleared_force:
                mine["force"] = True
            if mine is not None and info.get("adopted"):
                mine.setdefault("adopted", info["adopted"])
        _save(self.path, {
            "version": INDEX_VERSION,
            "copies": self.copies,
            "settings": self.settings,
            "blocks": self.blocks,
            "items": self.items,
            "registrations": self.registrations,
            "hooks": self.hooks,
            "instruction_sigs": self.instruction_sigs,
            "approvals": self.approvals,
            "own_files": self.own_files,
            "always_restore": self.always_restore,
            "last_fetch": self.last_fetch,
        })


# ---------------------------------------------------------------- journal


class Journal:
    """The writes a run is about to make, so a run that stops halfway can be finished.

    Each entry holds a path, the SHA-256 the tool is about to write there, and the index
    update that goes with it. After a crash, an entry whose file already has that hash is
    completed in the index; any other entry did not happen and is dropped.
    """

    def __init__(self, layout: Layout):
        self.path = layout.state / "journal.json"

    def pending(self) -> Optional[dict]:
        return _load(self.path, None)

    def begin(self, entries: List[dict]) -> None:
        _save(self.path, {"entries": entries})

    def finish(self) -> None:
        if self.path.exists():
            self.path.unlink()


class RunRecord:
    """What one run changed, with old bytes, so `undo` can reverse it."""

    KEEP = 10

    def __init__(self, layout: Layout, run_id: Optional[str] = None):
        self.root = layout.state / "runs"
        self.run_id = run_id or new_id()
        self.dir = self.root / self.run_id
        self.data = {"run": self.run_id, "time": time.time(), "files": [], "commits": [], "pushed": False, "incoming": {}}

    def file(self, path: Path, old: Optional[bytes], kind: str = "copy", backup: Optional[Path] = None) -> None:
        """Note a file this run changed and its bytes before. `backup` is the backup of a file the
        user had before agent-kit-sync, which this run put back at `path` after removing the copy."""
        for entry in self.data["files"]:
            if entry["path"] == str(path):
                # Recorded already, with its bytes from before the run, when this run wrote the path first.
                if backup is not None:
                    entry["backup"] = str(backup)
                return
        saved = None
        if old is not None:
            saved = str(self.dir / "files" / str(len(self.data["files"])))
            atomic_write(Path(saved), old)
        entry = {"path": str(path), "saved": saved, "kind": kind}
        if backup is not None:
            entry["backup"] = str(backup)
        self.data["files"].append(entry)

    @property
    def changed(self) -> bool:
        return bool(self.data["files"] or self.data["commits"] or self.data["incoming"])

    def save(self) -> None:
        if not self.changed:
            return
        _save(self.dir / "record.json", self.data)
        runs = self._in_order(self.root)
        for old in runs[: max(0, len(runs) - self.KEEP)]:
            _remove_tree(old)

    @staticmethod
    def _in_order(root: Path) -> List[Path]:
        """Run folders, oldest first. A run's name holds its start time only to the second, followed by a
        random part, so two runs in one second are ordered by the time saved in each record."""
        if not root.is_dir():
            return []

        def started(folder: Path) -> tuple:
            data = _load(folder / "record.json", None)
            when = data.get("time") if isinstance(data, dict) else None
            return (float(when) if isinstance(when, (int, float)) else folder.stat().st_mtime, folder.name)

        return sorted((p for p in root.iterdir() if p.is_dir()), key=started)

    @classmethod
    def latest(cls, layout: Layout) -> Optional[dict]:
        for folder in reversed(cls._in_order(layout.state / "runs")):
            data = _load(folder / "record.json", None)
            if data and not data.get("undone"):
                data["_dir"] = str(folder)
                return data
        return None


def _remove_tree(folder: Path) -> None:
    for path in sorted(folder.rglob("*"), reverse=True):
        path.unlink() if path.is_file() or path.is_symlink() else path.rmdir()
    folder.rmdir()


# ---------------------------------------------------------------- lock


class LockBusy(Exception):
    """Another sync run holds the lock."""


@contextmanager
def run_lock(layout: Layout, wait: float = 0.0) -> Iterator[None]:
    """Hold the machine's sync lock. The operating system releases it when the process ends."""
    layout.state.mkdir(parents=True, exist_ok=True)
    path = layout.state / "lock"
    handle = open(path, "a+b")
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                _lock(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockBusy(str(path)) from None
                time.sleep(0.2)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


if sys.platform.startswith("win"):
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def request_rerun(layout: Layout) -> None:
    layout.state.mkdir(parents=True, exist_ok=True)
    (layout.state / "rerun").write_bytes(b"1")


def take_rerun(layout: Layout) -> bool:
    path = layout.state / "rerun"
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------- alerts


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class Alerts:
    """Problems the user must see. Each session is shown each alert once."""

    def __init__(self, layout: Layout):
        self.path = layout.state / "alerts.json"
        self.seen_dir = layout.state / "seen"

    def all(self) -> List[dict]:
        return list(_load(self.path, []))

    def open(self) -> List[dict]:
        return [alert for alert in self.all() if not alert.get("closed")]

    def raise_(self, key: str, message: str, command: str = "", detail: str = "") -> dict:
        """Raise an alert, or refresh the existing open alert with the same key."""
        alerts = self.all()
        for alert in alerts:
            if alert.get("key") == key and not alert.get("closed"):
                alert.setdefault("since", alert.get("time", time.time()))
                alert.update(message=message, command=command, detail=detail, time=time.time())
                _save(self.path, alerts)
                return alert
        now = time.time()
        alert = {"id": new_id(), "key": key, "message": message, "command": command, "detail": detail, "time": now, "since": now}
        alerts.append(alert)
        _save(self.path, alerts)
        return alert

    def close(self, key: str) -> None:
        alerts = self.all()
        changed = False
        for alert in alerts:
            if alert.get("key") == key and not alert.get("closed"):
                alert["closed"] = time.time()
                changed = True
        if changed:
            _save(self.path, alerts)

    def unseen(self, session: str) -> List[dict]:
        if not session:
            return []
        seen_path = self.seen_dir / (session.replace("/", "_")[:120] + ".json")
        seen = set(_load(seen_path, []))
        fresh = [alert for alert in self.open() if alert["id"] not in seen]
        if fresh:
            _save(seen_path, sorted(seen | {alert["id"] for alert in fresh}))
        return fresh


# ---------------------------------------------------------------- held changes


class Held:
    """Changes found on this machine that wait for the user's decision."""

    def __init__(self, layout: Layout):
        self.path = layout.state / "held.json"
        self.dir = layout.state / "held"

    def all(self) -> List[dict]:
        return list(_load(self.path, []))

    def find(self, held_id: str) -> Optional[dict]:
        return next((entry for entry in self.all() if entry["id"] == held_id), None)

    def by_key(self, key: str) -> Optional[dict]:
        return next((entry for entry in self.all() if entry.get("key") == key), None)

    def add(self, key: str, kind: str, summary: str, choices: List[str], data: Optional[Dict[str, bytes]] = None, **extra) -> dict:
        """Hold a change. Holding the same key again updates that entry and keeps its identifier."""
        entries = self.all()
        previous = next((entry for entry in entries if entry.get("key") == key), None)
        entries = [entry for entry in entries if entry.get("key") != key]
        entry = {"id": previous["id"] if previous else new_id(), "key": key, "kind": kind, "summary": summary, "choices": choices, "time": time.time(), "files": {}}
        entry.update(extra)
        for name, blob in (data or {}).items():
            path = self.dir / entry["id"] / name
            atomic_write(path, blob)
            entry["files"][name] = str(path)
        entries.append(entry)
        _save(self.path, entries)
        return entry

    def blob(self, entry: dict, name: str) -> Optional[bytes]:
        path = entry.get("files", {}).get(name)
        return read_or_none(Path(path)) if path else None

    def remove(self, held_id: str) -> None:
        entries = [entry for entry in self.all() if entry["id"] != held_id]
        _save(self.path, entries)
        folder = self.dir / held_id
        if folder.is_dir():
            _remove_tree(folder)
