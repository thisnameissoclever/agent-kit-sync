"""Applying the user's choice to a held change."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePosixPath

from . import gitops, toml
from .fsutil import atomic_write, prune_empty_dirs, read_or_none
from .model import load_machine
from .paths import Layout
from .state import Alerts, Held, Index


def _confirm_in_terminal(summary: str) -> bool:
    if os.environ.get("CLAUDECODE") or os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_THREAD_ID") or not sys.stdin.isatty():
        return False
    sys.stdout.write(summary + "\nType yes to approve: ")
    sys.stdout.flush()
    return sys.stdin.readline().strip().lower() == "yes"


def resolve(layout: Layout, held_id: str, choice: str) -> int:
    held = Held(layout)
    entry = held.find(held_id or "")
    if entry is None:
        print(f"No held change with the identifier {held_id}. Run agent-kit-sync held to list them.")
        return 2
    if choice not in entry["choices"]:
        print(f"{choice} is not a choice for this change. Choices: {', '.join(entry['choices'])}")
        return 2
    machine = load_machine(layout)
    approving = choice in ("approve", "apply", "keep-and-sync", "use-this", "config-replaces-local", "local-replaces-config", "delete-everywhere", "retry")
    if approving and machine.policy.get("approval") == "terminal" and not _confirm_in_terminal(entry["summary"]):
        print("This machine requires approval typed in an interactive terminal outside any agent session. Run the same command there.")
        return 3
    from .state import run_lock
    with run_lock(layout, wait=60):
        return _resolve_locked(layout, machine, held, entry, choice)


# The alert the engine raises with each kind of held change. Several held deletions share one alert.
ALERT_KEYS = {
    "edit": "held:{item_key}", "outgoing": "held:{item_key}", "setting": "setting:{setting_key}",
    "incoming": "incoming:{item_key}", "new-item": "incoming:{item_key}", "removal": "incoming:{item_key}",
    "collision": "collision:{app}|{item_key}", "deletion": "deletions", "checkout-edit": "checkout:{path}",
}


def alert_key(entry: dict):
    template = ALERT_KEYS.get(entry.get("kind", ""))
    if template is None:
        return None
    return template.format(item_key=entry.get("item_key", ""), setting_key=entry.get("setting_key", ""), app=entry.get("app", ""), path=entry.get("path", ""))


def _resolve_locked(layout, machine, held, entry, choice) -> int:
    index = Index(layout)
    alerts = Alerts(layout)
    kind = entry["kind"]
    item_key = entry.get("item_key", "")
    handler = HANDLERS.get(kind)
    if handler is None:
        print(f"agent-kit-sync cannot resolve a held change of kind {kind}.")
        return 2
    message = handler(layout, machine, index, entry, choice)
    held.remove(entry["id"])
    info = index.items.get(item_key)
    if info and info.get("frozen") == entry["id"]:
        info.pop("frozen", None)
    index.save()
    key = alert_key(entry)
    if key and not any(alert_key(other) == key for other in held.all()):
        alerts.close(key)
    print(message + " Run agent-kit-sync sync to apply it now, or wait for the next sync.")
    return 0


# ---------------------------------------------------------------- handlers


def _edit(layout, machine, index, entry, choice):
    """A local edit held because it failed the checks, could not be carried back, or waits on outgoing policy."""
    item_key = entry["item_key"]
    module, _item = item_key.split("/", 1)
    root = _module_root(layout, module)
    if choice in ("approve",):
        for number, rel in entry.get("sources", {}).items():
            blob = Held(layout).blob(entry, number)
            target = root / Path(*PurePosixPath(rel).parts)
            if blob is None:
                if target.exists():
                    target.unlink()
                    prune_empty_dirs(target.parent, root)
            else:
                atomic_write(target, blob)
        # Rewrite this machine's installed copies from the approved source and refresh the index,
        # so the approved edit is not seen as a new edit on the next run.
        index.items.setdefault(item_key, {})["force"] = True
        index.approvals["outgoing:" + item_key] = "1"
        return f"Approved the edit to {item_key}; it is committed and pushed at the next sync."
    if choice == "retry":
        return f"agent-kit-sync will try to carry the edit to {item_key} back again; fix the problem in the installed copy first."
    # Discard: the next run overwrites the installed copies and blocks with the config repo's version.
    index.items.setdefault(item_key, {})["force"] = True
    return f"Discarded the edit to {item_key}; its installed copies go back to the config repo's version."


def _setting(layout, machine, index, entry, choice):
    key = entry["setting_key"]
    record = index.settings.get(key)
    if record is None:
        return "That setting is no longer synced."
    record.pop("held", None)
    if choice == "keep-and-sync":
        value = json.loads(Held(layout).blob(entry, "value").decode("utf-8"))
        if record.get("mode") == "contains" and isinstance(value, list):
            value = [v for v in (record.get("value") or []) if v in value]
        module, item = entry["item_key"].split("/", 1)
        path = _module_root(layout, module) / "module.toml"
        from . import tomledit
        text = path.read_text(encoding="utf-8")
        atomic_write(path, tomledit.set_field(text, "settings", item, "value", value).encode("utf-8"))
        record["written"] = value
        return f"Kept the new value of {record['key']} and wrote it to the config repo."
    if choice == "always-restore":
        if key not in index.always_restore:
            index.always_restore.append(key)
    record["force"] = True
    return f"agent-kit-sync will put back the synced value of {record['key']}."


def _incoming(layout, machine, index, entry, choice):
    item_key = entry["item_key"]
    if choice == "apply":
        from .engine import Engine, item_fingerprint
        engine = Engine(layout)
        _inv, modules = engine.load_modules()
        module = next((m for m in modules if m.name == item_key.split("/", 1)[0]), None)
        item = next((i for i in module.items if i.key == item_key), None) if module else None
        if item is not None:
            from .tree import Tree
            index.approvals[item_key] = item_fingerprint(module, item, Tree(module.root))
        else:
            index.items.pop(item_key, None)  # A removal: forget the item so it is removed.
        return f"Approved the change to {item_key}."
    data = toml.load_file(layout.machine_file)
    overrides = data.setdefault("overrides", {})
    if entry["kind"] == "new-item":
        overrides[item_key] = {"kind": "disable"}
        message = f"Declined {item_key}; it is disabled on this machine."
    else:
        commit = index.items.get(item_key, {}).get("commit") or gitops.head(layout.config)
        overrides[item_key] = {"kind": "pin", "commit": commit}
        message = f"Declined the change to {item_key}; this machine keeps the earlier version (pinned at {commit[:12]})."
    atomic_write(layout.machine_file, toml.dumps(data).encode("utf-8"))
    return message


def _deletion(layout, machine, index, entry, choice):
    item_key = entry["item_key"]
    if choice == "restore":
        for key in [k for k, e in index.copies.items() if e["item_key"] == item_key]:
            index.drop_copy(key)
        for key in [k for k, e in index.blocks.items() if e["item_key"] == item_key]:
            del index.blocks[key]
        return f"{item_key} will be installed again."
    if choice == "disable-here":
        data = toml.load_file(layout.machine_file)
        data.setdefault("overrides", {})[item_key] = {"kind": "disable"}
        atomic_write(layout.machine_file, toml.dumps(data).encode("utf-8"))
        return f"{item_key} is disabled on this machine and stays everywhere else."
    from .engine import Engine
    engine = Engine(layout)
    _inv, modules = engine.load_modules()
    module, item = engine._module_item(modules, item_key)
    if module is not None and item is not None:
        engine._delete_item(module, item)
    return f"{item_key} is deleted from the config repo; every machine removes it at its next sync."


def _merge(layout, machine, index, entry, choice):
    path = entry.get("path")
    if choice == "use-this" and path:
        blob = Held(layout).blob(entry, "ours")
        if blob is not None:
            atomic_write(layout.config / path, blob)
            return f"This machine's version of {path} replaces the other machine's at the next sync."
    return f"Kept the other machine's version of {path}."


def _checkout_edit(layout, machine, index, entry, choice):
    path = entry.get("path")
    if choice == "use-this" and path:
        blob = Held(layout).blob(entry, "ours")
        if blob is not None:
            atomic_write(layout.config / path, blob)
            return f"The saved version of {path} is back in the config repo checkout. Fix what the checks reported before the next sync, or it is set back again."
    return f"Discarded the saved version of {path}."


def _edit_conflict(layout, machine, index, entry, choice):
    if choice == "use-this":
        return "Copy the saved version back into the installed copy you want, then sync; agent-kit-sync carries it back."
    return "Discarded the other edit."


def _collision(layout, machine, index, entry, choice):
    group = f"{entry['app']}|{entry['item_key']}"
    if choice == "config-replaces-local":
        index.approvals["replace:" + group] = "1"
        return f"The config repo's {entry['item_key']} replaces the one already in that app, after a backup."
    if choice == "skip":
        data = toml.load_file(layout.machine_file)
        data.setdefault("overrides", {})[entry["item_key"]] = {"kind": "disable"}
        atomic_write(layout.machine_file, toml.dumps(data).encode("utf-8"))
        return f"{entry['item_key']} is disabled on this machine; the existing one stays."
    return "Use agent-kit-sync add with the existing file to replace or merge the config repo's version, then resolve this again with config-replaces-local."


HANDLERS = {
    "edit": _edit, "outgoing": _edit, "setting": _setting, "incoming": _incoming, "new-item": _incoming, "removal": _incoming,
    "deletion": _deletion, "merge-conflict": _merge, "edit-conflict": _edit_conflict, "collision": _collision,
    "checkout-edit": _checkout_edit,
}


def _module_root(layout: Layout, module: str) -> Path:
    local = layout.local_modules / module
    return local if local.is_dir() else layout.config / "modules" / module
