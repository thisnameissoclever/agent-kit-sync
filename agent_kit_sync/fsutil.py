"""File operations that never leave a half-written file and keep permissions."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import Optional

ANY = object()
WINDOWS = sys.platform.startswith("win")


class ChangedUnderneath(Exception):
    """The file changed between reading it and replacing it."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_or_none(path) -> Optional[bytes]:
    try:
        return Path(path).read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None


def real(path) -> Path:
    """Resolve symbolic links, including links in parent folders, without requiring the file to exist."""
    return Path(os.path.realpath(path))


def is_executable(path) -> bool:
    if WINDOWS:
        return False
    try:
        return bool(os.stat(path).st_mode & stat.S_IXUSR)
    except OSError:
        return False


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def _mode_for(existing: Optional[int], executable: Optional[bool]) -> Optional[int]:
    if WINDOWS:
        return None
    mode = existing if existing is not None else (0o666 & ~_umask())
    if executable is True:
        mode |= (mode & 0o444) >> 2
    elif executable is False:
        mode &= ~0o111
    return mode


def replace_with_retry(src: Path, dst: Path, seconds: float = 2.0) -> None:
    """Rename over a file, retrying while Windows reports it open in another process."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def atomic_write(path, data: bytes, *, executable: Optional[bool] = None, expect=ANY) -> Path:
    """Write through any symbolic link to its target, by temporary file and rename.

    `expect` is the SHA-256 the file must still have just before the rename, or None
    when it must not exist; the write raises ChangedUnderneath otherwise.
    """
    target = real(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        existing = None
    tmp = target.with_name("." + target.name + ".aks-" + secrets.token_hex(4) + ".tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        mode = _mode_for(existing, executable)
        if mode is not None:
            os.chmod(tmp, mode)
        if expect is not ANY:
            current = read_or_none(target)
            current_hash = None if current is None else sha256(current)
            if current_hash != expect:
                raise ChangedUnderneath(str(target))
        replace_with_retry(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return target


def remove_file(path, *, expect=ANY) -> bool:
    target = real(path)
    current = read_or_none(target)
    if current is None:
        return False
    if expect is not ANY and sha256(current) != expect:
        raise ChangedUnderneath(str(target))
    target.unlink()
    return True


def prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove empty folders from `start` up to, but not including, `stop`."""
    start, stop = Path(start), Path(stop)
    current = start
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def stat_signature(path) -> Optional[list]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]
