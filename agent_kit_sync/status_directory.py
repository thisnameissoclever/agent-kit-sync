"""Stage status data, then retain whole legacy directories after references switch."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
import unicodedata
from pathlib import Path

from . import toml
from .fsutil import atomic_write, private_dir, sha256
from .paths import Layout
from .state import LockBusy, run_lock

MAX_ENTRIES = 512
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024


class PendingMigration(Exception):
    pass


def migration_enabled(layout):
    path = layout.config / "kit.toml"
    if not path.is_file():
        return False
    data = toml.load_file(path)
    return data.get("format", 1) >= 2 and data.get("migrations", {}).get("project_status_directory") == 1


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _inside(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _contained(path, root):
    if not _inside(path, root):
        raise PendingMigration(f"Path leaves its approved directory: {path}")
    current = path
    while True:
        if (current.exists() or current.is_symlink()) and _linked(current):
            raise PendingMigration(f"Link or reparse point needs manual review: {current}")
        if current == root:
            break
        current = current.parent


def _read(path):
    before = path.stat()
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    after = path.stat()
    if len(data) > MAX_FILE_BYTES:
        raise PendingMigration(f"File exceeds the bounded migration limit: {path}")
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise PendingMigration(f"File changed while being read: {path}")
    return data


def legacy_directories(projects):
    names = {projects.name + "_status", projects.name.lower() + "_status", projects.name.upper() + "_status"}
    candidates = [projects / "_status"]
    candidates += [parent / name for parent in (projects, projects.parent) for name in sorted(names)]
    return _unique(candidates)


def _unique(paths):
    seen, result = set(), []
    for path in paths:
        key = str(path.absolute())
        if key not in seen:
            seen.add(key)
            if path.exists() and not _linked(path) and any(p.exists() and not _linked(p) and os.path.samefile(path, p) for p in result):
                continue
            result.append(path)
    return result


def _snapshot(root):
    if _linked(root) or not root.is_dir():
        raise PendingMigration(f"Historical status path is not an ordinary directory: {root}")
    identity = root.stat()
    pending, directories, files = [root], [], {}
    total = count = 0
    while pending:
        for path in pending.pop().iterdir():
            count += 1
            if count > MAX_ENTRIES:
                raise PendingMigration(f"Directory exceeds the {MAX_ENTRIES}-entry migration limit: {root}")
            _contained(path, root)
            if path.name == ".git":
                raise PendingMigration(f"Repository data needs manual review: {root}")
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > MAX_TOTAL_BYTES:
                    raise PendingMigration(f"Directory exceeds the bounded migration byte limit: {root}")
                data = _read(path)
                files[relative] = {"hash": sha256(data), "size": len(data), "modified": info.st_mtime_ns, "identity": [info.st_dev, info.st_ino]}
            else:
                raise PendingMigration(f"Special file needs manual review: {path}")
    return {"identity": [identity.st_dev, identity.st_ino], "directories": sorted(directories), "files": files}


def _exclusive_copy(source, target, digest):
    data = _read(source)
    if sha256(data) != digest:
        raise PendingMigration(f"Source changed before copying: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as handle:
            _copy_permissions(source, target)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if not target.is_file() or sha256(_read(target)) != digest:
            raise PendingMigration(f"Destination changed or conflicts: {target}") from None
    if sha256(_read(target)) != digest:
        raise PendingMigration(f"Copy verification failed: {target}")


def _copy_permissions(source, target):
    if os.name != "nt":
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
        return
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    get = api.GetFileSecurityW
    get.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    get.restype = wintypes.BOOL
    size = wintypes.DWORD()
    get(str(source), 4, None, 0, ctypes.byref(size))
    if not size.value:
        error = ctypes.get_last_error()
        if error in (1, 50):
            return  # The source filesystem has no persistent access-control list to preserve.
        raise ctypes.WinError(error)
    descriptor = ctypes.create_string_buffer(size.value)
    if not get(str(source), 4, descriptor, size.value, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    set_security = api.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    if not set_security(str(target), 4 | 0x80000000, descriptor):
        raise ctypes.WinError(ctypes.get_last_error())


def _validate_sources(layout, canonical, sources):
    for source in sources:
        if not source.is_absolute() or not (source.name.lower() in ("status", "_status") or source.name.lower().endswith("_status")):
            raise PendingMigration(f"Explicit source must name an absolute status directory: {source}")
        if _inside(source, canonical) or _inside(canonical, source) or _inside(source, layout.root) or _inside(layout.root, source):
            raise PendingMigration(f"Source overlaps canonical data or the sync kit: {source}")
        for other in sources:
            if source != other and (_inside(source, other) or _inside(other, source)):
                raise PendingMigration(f"Historical sources overlap: {source}, {other}")


def _verify_canonical(canonical, snapshot):
    for relative, entry in snapshot["files"].items():
        target = canonical / relative
        _contained(target, canonical)
        if not target.is_file() or sha256(_read(target)) != entry["hash"]:
            raise PendingMigration(f"Canonical data changed or is incomplete: {target}")


def _preflight(canonical, snapshots):
    virtual = {"state": ("directory", None)}
    for source, snapshot in snapshots.items():
        entries = [(p, "directory", None) for p in snapshot["directories"]]
        entries += [(p, "file", e["hash"]) for p, e in snapshot["files"].items()]
        for relative, kind, digest in entries:
            target = canonical / relative
            _contained(target, canonical)
            key = unicodedata.normalize("NFC", relative).casefold()
            if key in virtual and virtual[key] != (kind, digest):
                raise PendingMigration(f"Historical sources contain conflicting data: {target}")
            virtual[key] = (kind, digest)
            if target.exists():
                if kind == "directory" and not target.is_dir():
                    raise PendingMigration(f"Directory conflicts with a file: {target}")
                if kind == "file" and (not target.is_file() or sha256(_read(target)) != digest):
                    raise PendingMigration(f"Different destination data is preserved: {target}")


def installed_paths(layout, machine, index):
    paths = {Path(e["path"]) for e in index.copies.values()}
    for app, home in machine.app_homes.items():
        names = ("AGENTS.md", "hooks.json", "config.toml") if app == "codex" else ("CLAUDE.md", "settings.json") if app == "claude" else ("hooks.json",)
        paths.update(home / name for name in names)
        if app == "cursor" and (home / "rules").is_dir():
            paths.update((home / "rules").glob("agent-kit-sync*.mdc"))
    return paths


def _old_references(paths, sources):
    needles = [str(p).replace("\\", "/") for p in sources]
    for path in paths:
        if not path.is_file():
            continue
        try:
            raw = _read(path)
            text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeError:
            continue
        views = [text.replace("\\", "/"), text.replace("\\\\", "\\").replace("\\", "/")]
        if os.name == "nt":
            views = [view.lower() for view in views]
        for needle in needles:
            if os.name == "nt":
                needle = needle.lower()
            if any(re.search(re.escape(needle) + r"(?=$|[/\s`\"',;:)\]}>?#]|\.(?=$|\s))", view) for view in views):
                raise PendingMigration(f"Installed content still references a historical path: {path}")


def _load_receipt(path, layout):
    if not path.exists():
        return {"version": 2, "staged": {}, "retired": [], "explicit_sources": []}
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ValueError("receipt exceeds its bounded size")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 2 or not isinstance(data.get("staged"), dict) or not isinstance(data.get("retired"), list) or not isinstance(data.get("explicit_sources"), list):
            raise ValueError("invalid migration receipt structure")
        if not all(isinstance(p, str) and "\0" not in p for p in data["explicit_sources"]):
            raise ValueError("invalid explicit source list")
        for entry in list(data["staged"].values()) + data["retired"]:
            snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("identity"), list) or not isinstance(snapshot.get("files"), dict) or not isinstance(snapshot.get("directories"), list):
                raise ValueError("invalid source snapshot")
            from .model import safe_rel
            if not all(safe_rel(p) for p in list(snapshot["files"]) + snapshot["directories"]):
                raise ValueError("unsafe snapshot relative path")
            if not all(isinstance(e, dict) and isinstance(e.get("hash"), str) and re.fullmatch(r"[0-9a-f]{64}", e["hash"]) for e in snapshot["files"].values()):
                raise ValueError("invalid snapshot file hashes")
            if not isinstance(entry.get("backup"), str) or not _inside(Path(entry["backup"]), layout.state / "backups/status-directory-migration"):
                raise ValueError("unsafe backup path")
        for entry in data["retired"]:
            source, retained = Path(entry["source"]), Path(entry["retained"])
            if source.parent != retained.parent or not retained.name.startswith("." + source.name + ".agent-kit-sync-backup-"):
                raise ValueError("unsafe retained-directory path")
        return data
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise PendingMigration(f"Existing receipt is preserved and needs review: {path}: {error}") from None


def _recover_retirements(record):
    for entry in list(record["retired"]):
        source, retained = Path(entry["source"]), Path(entry["retained"])
        if entry.get("phase") == "rename-pending" and source.exists() and not retained.exists():
            if _snapshot(source) != entry["snapshot"]:
                raise PendingMigration(f"An interrupted retirement has changed source data: {source}")
            record["retired"].remove(entry)  # A failed rename left the source intact; a later sync can retry.
            continue
        if not retained.is_dir() or _snapshot(retained) != entry["snapshot"]:
            raise PendingMigration(f"Retained data changed and needs reconciliation; both versions are preserved: {retained}")
        entry["phase"] = "retained"
        record["staged"].pop(entry["source"], None)


def _save_receipt(path, record):
    raw = json.dumps(record, indent=2).encode()
    if len(raw) > MAX_RECEIPT_BYTES:
        raise PendingMigration("Migration receipt exceeds its bounded size; existing data and receipt are preserved.")
    atomic_write(path, raw)


def migrate_status(layout, projects, required=False, explicit_sources=(), finalize=False, active_paths=(), plan_id=None):
    """Prepare copies first. Finalization retains whole source trees; it never deletes files."""
    if projects is None:
        return {"state": "not-configured", "pending": [], "staged": {}, "retired": []}
    projects = Path(projects)
    canonical = projects / "status"
    receipt_path = layout.state / "status-directory-migration.json"
    result = {"state": "pending", "pending": []}
    try:
        with run_lock(Layout(layout.state / "status-directory-migration")):
            record = _load_receipt(receipt_path, layout)
            try:
                if not projects.is_dir():
                    raise PendingMigration(f"Configured projects directory is unavailable: {projects}")
                _contained(canonical, canonical)
                if canonical.exists() and not canonical.is_dir():
                    raise PendingMigration(f"Canonical status path is not a directory: {canonical}")
                record["canonical"] = str(canonical)
                proposed_sources = list(dict.fromkeys(record["explicit_sources"] + [str(p) for p in explicit_sources]))
                _validate_sources(layout, canonical, [Path(p) for p in proposed_sources])
                record["explicit_sources"] = proposed_sources
                _recover_retirements(record)
                for explicit in proposed_sources:
                    if not Path(explicit).exists() and not any(e["source"] == explicit for e in record["retired"]):
                        raise PendingMigration(f"Explicit historical source is unavailable: {explicit}")
                candidates = _unique(legacy_directories(projects) + [Path(p) for p in proposed_sources])
                sources = [p for p in candidates if p.exists() or p.is_symlink()]
                _validate_sources(layout, canonical, sources)
                snapshots = {str(p): _snapshot(p) for p in sources}
                proposed_id = sha256(json.dumps({"canonical": str(canonical), "sources": snapshots}, sort_keys=True).encode())
                if finalize:
                    if plan_id is None or plan_id != record.get("plan_id") or proposed_id != plan_id:
                        raise PendingMigration("Prepared plan changed; prepare and verify again before finalization.")
                    _old_references(active_paths, candidates)
                    for source in sources:
                        prepared = record["staged"].get(str(source))
                        if prepared is None or prepared["snapshot"] != snapshots[str(source)]:
                            raise PendingMigration(f"Historical data changed after preparation: {source}")
                        _verify_canonical(canonical, prepared["snapshot"])
                    for source in sources:
                        prepared = record["staged"][str(source)]
                        if _snapshot(source) != prepared["snapshot"]:
                            raise PendingMigration(f"Source changed before retirement: {source}")
                        retained = source.with_name("." + source.name + ".agent-kit-sync-backup-" + secrets.token_hex(12))
                        _contained(source, source)
                        if retained.parent.resolve() != source.parent.resolve() or retained.exists():
                            raise PendingMigration(f"Unsafe retained-directory destination: {retained}")
                        entry = {"source": str(source), "retained": str(retained), "snapshot": prepared["snapshot"], "backup": prepared["backup"], "phase": "rename-pending"}
                        record["retired"].append(entry)
                        _save_receipt(receipt_path, record)
                        source.rename(retained)
                        if _snapshot(retained) != prepared["snapshot"]:
                            raise PendingMigration(f"Concurrent edits are retained for reconciliation: {retained}")
                        entry["phase"] = "retained"
                        del record["staged"][str(source)]
                    record["state"] = "complete"
                else:
                    _preflight(canonical, snapshots)
                    if sources or required:
                        created = not canonical.exists()
                        canonical.mkdir(exist_ok=True)
                        if created:
                            private_dir(canonical)
                        _contained(canonical / "state", canonical)
                        (canonical / "state").mkdir(exist_ok=True)
                    for source in sources:
                        snapshot = snapshots[str(source)]
                        prepared = record["staged"].get(str(source))
                        if prepared is None:
                            backup = layout.state / "backups/status-directory-migration" / secrets.token_hex(12)
                            backup.mkdir(parents=True, exist_ok=False)
                            private_dir(backup)
                            prepared = {"snapshot": snapshot, "backup": str(backup)}
                            record["staged"][str(source)] = prepared
                            _save_receipt(receipt_path, record)
                        elif prepared["snapshot"] != snapshot:
                            raise PendingMigration(f"Source changed since its earlier preparation: {source}")
                        for relative in snapshot["directories"]:
                            _contained(canonical / relative, canonical)
                            _contained(Path(prepared["backup"]) / relative, layout.state / "backups/status-directory-migration")
                            (canonical / relative).mkdir(parents=True, exist_ok=True)
                            (Path(prepared["backup"]) / relative).mkdir(parents=True, exist_ok=True)
                        for relative, entry in snapshot["files"].items():
                            _contained(Path(prepared["backup"]) / relative, layout.state / "backups/status-directory-migration")
                            _exclusive_copy(source / relative, Path(prepared["backup"]) / relative, entry["hash"])
                            _contained(canonical / relative, canonical)
                            _exclusive_copy(source / relative, canonical / relative, entry["hash"])
                        if _snapshot(source) != snapshot:
                            raise PendingMigration(f"Source changed during preparation and is preserved: {source}")
                    record["plan_id"] = proposed_id
                    record["state"] = "prepared" if sources else "complete" if required else "not-needed"
                record["pending"] = []
            except (PendingMigration, OSError) as error:
                record["state"] = "pending"
                record["pending"] = [str(error)]
            record["checked_at"] = time.time()
            _save_receipt(receipt_path, record)
            result = record
    except (PendingMigration, OSError, LockBusy) as error:
        result["pending"] = [str(error)]
    return result
