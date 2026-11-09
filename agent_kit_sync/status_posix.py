"""Move one POSIX status tree through atomic directory/link exchanges.

The directory keeps its inode, access controls, extended attributes and contents.
Prepared references share the original tree. Final exchange can interrupt a path
lookup already in progress. Retained links contain no backup data.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

from .fsutil import sha256


def _pending(message):
    from .status_directory import PendingMigration
    raise PendingMigration(message)


def _lstat(path):
    return path.lstat()


def _readlink(path):
    return os.readlink(path)


def _symlink(target, path):
    os.symlink(target, path, target_is_directory=True)


def _hardlink(source, target):
    os.link(source, target, follow_symlinks=False)


def _unlink(path):
    path.unlink()


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _native_rename(source, target, *, exchange):
    """Use documented platform flags; unsupported libraries fail closed."""
    library = ctypes.CDLL(None, use_errno=True)
    first, second = os.fsencode(source), os.fsencode(target)
    if sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        if function is None:
            _pending("The native C library does not provide renameat2; status data is preserved.")
        function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        function.restype = ctypes.c_int
        result = function(-100, first, -100, second, 2 if exchange else 1)
    elif sys.platform == "darwin":
        function = getattr(library, "renamex_np", None)
        if function is None:
            _pending("The native C library does not provide renamex_np; status data is preserved.")
        function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        function.restype = ctypes.c_int
        result = function(first, second, 2 if exchange else 4)
    else:
        _pending("Automatic whole-directory status migration requires Linux or macOS native rename support.")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(source), None, str(target))


def _exchange(source, target):
    _native_rename(source, target, exchange=True)


def _rename_exclusive(source, target):
    _native_rename(source, target, exchange=False)


def _identity(info):
    return [info.st_dev, info.st_ino]


def _info(path):
    try:
        return _lstat(path)
    except FileNotFoundError:
        return None


def _directory(path, identity=None):
    info = _info(path)
    return info is not None and stat.S_ISDIR(info.st_mode) and (identity is None or _identity(info) == identity)


def _link(path, identity, target):
    info = _info(path)
    return (info is not None and stat.S_ISLNK(info.st_mode)
            and _identity(info) == identity and _readlink(path) == target)


def _parent_identity(parent):
    for path in (parent,) + tuple(parent.parents):
        if not _directory(path):
            _pending(f"Projects ancestors must be ordinary directories: {path}")
    return _identity(_lstat(parent))


def _probe(parent):
    """Check exchange, symlink hard links and exclusive rename on owned nodes."""
    root = Path(tempfile.mkdtemp(prefix=".agent-kit-sync-status-probe-", dir=parent))
    root_identity = _identity(_lstat(root))
    directory, link, alias, retired = (root / name for name in ("directory", "link", "alias", "retired"))
    owned = {}
    target = str(directory)
    try:
        directory.mkdir(mode=0o700)
        owned["directory"] = _identity(_lstat(directory))
        _symlink(target, link)
        owned["link"] = _identity(_lstat(link))
        _hardlink(link, alias)
        if not _link(alias, owned["link"], target):
            _pending("The projects filesystem cannot preserve a symlink through a hard link.")
        _exchange(directory, link)
        if not _directory(link, owned["directory"]) or not _link(directory, owned["link"], target):
            _pending("The projects filesystem did not preserve identities during atomic exchange.")
        try:
            _rename_exclusive(directory, link)
        except FileExistsError:
            pass
        else:
            _pending("The projects filesystem does not enforce exclusive rename.")
        _rename_exclusive(directory, retired)
        if _info(directory) is not None or not _link(retired, owned["link"], target):
            _pending("The projects filesystem did not preserve the retired symlink.")
        _sync_directory(root)
        _sync_directory(parent)
    finally:
        # Inspect every possible location after an interrupted exchange. Never
        # recurse or follow links while removing this bounded empty fixture.
        if _directory(root, root_identity):
            for path in (directory, link, alias, retired):
                if "link" in owned and _link(path, owned["link"], target):
                    _unlink(path)
                elif "directory" in owned and _directory(path, owned["directory"]):
                    path.rmdir()
            root.rmdir()
            _sync_directory(parent)


def _implicit_sources(projects):
    names = {"_status", projects.name + "_status", projects.name.lower() + "_status", projects.name.upper() + "_status"}
    return {projects / name for name in names}


def _legacy_paths(projects):
    names = {projects.name + "_status", projects.name.lower() + "_status", projects.name.upper() + "_status"}
    return _implicit_sources(projects) | {projects.parent / name for name in names}


def _path(value):
    if not isinstance(value, str) or not value or "\0" in value:
        _pending("The POSIX migration receipt has an invalid path; all entries are preserved.")
    path = Path(value)
    if not path.is_absolute() or path != Path(os.path.abspath(path)) or str(path) != value:
        _pending("The POSIX migration receipt has a non-normalized path; all entries are preserved.")
    return path


def _valid_identity(value):
    return (isinstance(value, list) and len(value) == 2
            and all(type(item) is int and item >= 0 for item in value) and value[1] > 0)


def _plan_id(entry):
    keys = ("version", "parent", "canonical", "source", "source_identity", "parent_identity", "seed", "artifact", "token", "target", "link_identity")
    return sha256(json.dumps({key: entry[key] for key in keys}, sort_keys=True).encode())


def _validate(record, projects, layout, sources):
    entry = record.get("posix_move")
    if not isinstance(entry, dict) or entry.get("version") != 1:
        _pending("The POSIX status-move receipt is invalid; all entries are preserved.")
    try:
        source, target, parent, seed, artifact = (_path(entry[key]) for key in ("source", "canonical", "parent", "seed", "artifact"))
        token = entry["token"]
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{24}", token):
            _pending("The POSIX status-move ownership token is invalid.")
        if (parent != projects or target != projects / "status" or source not in _implicit_sources(projects)
                or source.parent != projects or entry["target"] != str(source)
                or seed != projects / (".status.agent-kit-sync-seed-" + token)
                or artifact != projects / ("." + source.name + ".agent-kit-sync-link-" + token)):
            _pending("The POSIX status-move receipt does not match the approved projects directory.")
        if any(not _valid_identity(entry[key]) for key in ("source_identity", "parent_identity")):
            _pending("The POSIX status-move receipt has invalid directory identities.")
        if entry["link_identity"] is not None and not _valid_identity(entry["link_identity"]):
            _pending("The POSIX status-move receipt has an invalid symlink identity.")
        if entry["phase"] not in ("seed-intent", "link-intent", "prepared", "exchange-intent", "retire-intent", "complete"):
            _pending("The POSIX status-move receipt has an invalid phase.")
        if record.get("plan_id") != _plan_id(entry):
            _pending("The POSIX status-move plan changed; prepare and verify it again.")
    except (KeyError, TypeError, ValueError):
        _pending("The POSIX status-move receipt is incomplete; all entries are preserved.")
    if (record.get("explicit_sources") or record.get("staged") or record.get("retired")
            or any(Path(path) != source for path in sources)):
        _pending("Additional status sources need reconciliation before the POSIX move can continue.")
    current_parent = _parent_identity(projects)
    if entry["phase"] != "complete" and current_parent != entry["parent_identity"]:
        _pending("The projects directory identity changed; all status entries are preserved.")
    if layout.root == projects or projects in layout.root.parents or layout.root in projects.parents:
        _pending("Projects and the sync installation overlap; status data is preserved.")
    if entry["phase"] != "complete" and _directory(source) and _info(source / ".git") is not None:
        _pending("The historical status root contains a repository marker; its directory is preserved.")
    return entry, source, target, seed, artifact


def _persist(record, layout, save):
    save(record)
    _sync_directory(layout.state)


def _state(entry, source, target, seed, artifact):
    source_info, target_info, artifact_info, seed_info = (_info(path) for path in (source, target, artifact, seed))
    identity, link_identity = entry["source_identity"], entry["link_identity"]
    if seed_info is not None and not _link(seed, link_identity, entry["target"]):
        _pending(f"An unowned migration seed is preserved for reconciliation: {seed}")
    if _directory(source, identity) and artifact_info is None:
        if target_info is None:
            return "original"
        if _link(target, link_identity, entry["target"]):
            return "prepared"
    elif _directory(target, identity):
        if artifact_info is None and _link(source, link_identity, entry["target"]):
            return "exchanged"
        if source_info is None and _link(artifact, link_identity, entry["target"]):
            return "retired"
    _pending("Status entries differ from the recorded POSIX move; every entry is preserved for reconciliation.")


def run(record, projects, layout, sources, finalize=False, plan_id=None, active_paths=(), save=None):
    """Run under the caller's migration lock with its durable receipt callback.

    Raises PendingMigration or OSError so the caller can preserve pending state.
    The caller must invoke this backend before rejecting its owned status link.
    """
    if save is None:
        _pending("A durable migration receipt callback is required before moving status data.")
    projects = Path(projects)
    sources = list(dict.fromkeys(Path(source) for source in sources))
    if layout.root == projects or projects in layout.root.parents or layout.root in projects.parents:
        _pending("Projects and the sync installation overlap; status data is preserved.")
    if "posix_move" not in record:
        if finalize:
            _pending("Prepare the POSIX status move before finalization.")
        if (len(sources) != 1 or sources[0] not in _implicit_sources(projects)
                or record.get("explicit_sources") or record.get("staged") or record.get("retired")):
            _pending("Automatic POSIX migration requires one implicit status directory inside projects.")
        source = sources[0]
        parent_identity = _parent_identity(projects)
        if _info(projects / "status") is not None or not _directory(source):
            _pending("Existing status entries need reconciliation before the POSIX move.")
        if _info(source / ".git") is not None:
            _pending("The historical status root contains a repository marker; its directory is preserved.")
        source_identity = _identity(_lstat(source))
        if source_identity[0] != parent_identity[0]:
            _pending("The historical status directory is on a different filesystem; its data is preserved.")
        _probe(projects)
        token = secrets.token_hex(12)
        entry = {
            "version": 1, "parent": str(projects), "canonical": str(projects / "status"),
            "source": str(source), "source_identity": source_identity, "parent_identity": parent_identity,
            "token": token, "seed": str(projects / (".status.agent-kit-sync-seed-" + token)),
            "artifact": str(projects / ("." + source.name + ".agent-kit-sync-link-" + token)),
            "target": str(source), "link_identity": None, "phase": "seed-intent",
            "artifact_contains_backup_data": False, "data_location": str(projects / "status"),
        }
        record["posix_move"] = entry
        record["plan_id"] = _plan_id(entry)
        _persist(record, layout, save)
    entry, source, target, seed, artifact = _validate(record, projects, layout, sources)
    if entry["phase"] == "complete":
        if any(_info(path) is not None for path in _legacy_paths(projects) | set(sources)):
            _pending("A historical status root reappeared after completion; every entry is preserved.")
        if not _directory(target):
            _pending("The active status path must be an ordinary directory; every entry is preserved.")
        for path in (seed, artifact):
            if _info(path) is not None and not _link(path, entry["link_identity"], entry["target"]):
                _pending(f"An unexpected migration artifact is preserved for reconciliation: {path}")
        # Completed migration receipts describe a finished path transition.
        # Ordinary use and restoration can replace the active directory inode.
        record["state"] = "complete"
        record["pending"] = []
        return record
    if finalize and (plan_id is None or plan_id != record["plan_id"]):
        _pending("The finalization plan does not match the prepared POSIX status move.")
    if entry["link_identity"] is None:
        if entry["phase"] != "seed-intent" or _info(seed) is not None:
            _pending("An interrupted symlink creation needs ownership reconciliation; status data is preserved.")
        if _state(entry, source, target, seed, artifact) != "original":
            _pending("The original status directory must be intact before preparing references.")
        _symlink(entry["target"], seed)
        _sync_directory(projects)
        entry["link_identity"] = _identity(_lstat(seed))
        if not _link(seed, entry["link_identity"], entry["target"]):
            _pending("The migration seed changed during creation; every entry is preserved.")
        entry["phase"] = "link-intent"
        record["plan_id"] = _plan_id(entry)
        _persist(record, layout, save)
    state = _state(entry, source, target, seed, artifact)
    if state == "original":
        if not _link(seed, entry["link_identity"], entry["target"]):
            _pending("The migration ownership seed is missing; the original directory is preserved.")
        _hardlink(seed, target)
        _sync_directory(projects)
        state = _state(entry, source, target, seed, artifact)
    if state == "prepared":
        entry["phase"] = "prepared"
        record["state"] = "prepared"
        record["pending"] = []
        _persist(record, layout, save)
        if not finalize:
            return record
        from .status_directory import _old_references
        _old_references(active_paths, [source])
        entry["phase"] = "exchange-intent"
        _persist(record, layout, save)
        _validate(record, projects, layout, sources)
        if _state(entry, source, target, seed, artifact) != "prepared":
            _pending("Status entries changed before the final exchange; all entries are preserved.")
        _exchange(source, target)
        _sync_directory(projects)
        state = _state(entry, source, target, seed, artifact)
    if state == "exchanged":
        if not finalize:
            record["state"] = "prepared"
            record["pending"] = []
            _persist(record, layout, save)
            return record
        from .status_directory import _old_references
        _old_references(active_paths, [source])
        entry["phase"] = "retire-intent"
        _persist(record, layout, save)
        _validate(record, projects, layout, sources)
        if _state(entry, source, target, seed, artifact) != "exchanged":
            _pending("Status entries changed before retiring the owned link; all entries are preserved.")
        _rename_exclusive(source, artifact)
        _sync_directory(projects)
        state = _state(entry, source, target, seed, artifact)
    if state != "retired":
        _pending("The POSIX status move has an unexpected state; every entry is preserved.")
    if _info(seed) is not None:
        if not _link(seed, entry["link_identity"], entry["target"]):
            _pending("The migration seed identity changed; its entry is preserved.")
        _unlink(seed)
        _sync_directory(projects)
    entry["phase"] = "complete"
    record["state"] = "complete"
    record["pending"] = []
    _persist(record, layout, save)
    return record
