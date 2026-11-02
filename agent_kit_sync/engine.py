"""The sync run: find local edits, carry them back, merge, apply policy, install, and push."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from . import checks, gitops, render, sections, settings, tomledit
from .fsutil import ChangedUnderneath, atomic_write, prune_empty_dirs, read_or_none, real, remove_file, sha256, stat_signature
from .model import ConfigError, Inventory, Item, Machine, Module, load_inventory, load_local_modules, load_machine, load_module
from .paths import Layout
from .plan import Copy, Plan, Planner, SectionBlock, SettingWrite, INSTRUCTION_FILES, SETTINGS_FILES, is_code_path, values_for
from .state import Alerts, Held, Index, Journal, RunRecord
from .tree import CommitTree, Tree

SIZE_LIMIT = 32 * 1024
DELETION_GUARD = 3
QUIET_SECONDS = 15


@dataclass
class Report:
    edits: List[str] = field(default_factory=list)
    deletions: List[str] = field(default_factory=list)
    installed: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    held: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    pushed: bool = False
    merged: bool = False


@dataclass
class LocalChange:
    kind: str                     # edit, delete, section-edit, section-delete, setting, added
    item_key: str
    module: str
    item: str
    app: str
    copy_key: str = ""
    path: Optional[Path] = None
    new: Optional[bytes] = None


def item_fingerprint(module: Module, item: Item, tree: Tree) -> str:
    digest = hashlib.sha256(json.dumps(item.data, sort_keys=True, default=str).encode("utf-8"))
    for rel in item_source_files(module, item, tree):
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(tree.read(rel) or b"")
        digest.update(b"\1" if tree.executable(rel) else b"\0")
    return digest.hexdigest()


def item_source_files(module: Module, item: Item, tree: Tree) -> List[str]:
    data = item.data
    if item.kind == "section":
        return [data["file"]] + sorted(dict(data.get("variants", {})).values())
    if item.kind == "skill":
        base = data["dir"].rstrip("/")
        return [f"{base}/{rel}" for rel, _x in tree.files(base, module.ignore)]
    if item.kind == "agent":
        return [data["file"]]
    if item.kind == "hook":
        return [data["file"]] + list(data.get("tests", []))
    if item.kind == "file":
        base = data["path"].rstrip("/")
        found = tree.files(base, module.ignore)
        return [f"{base}/{rel}" for rel, _x in found] if found else [base]
    return []


def item_is_code(module: Module, item: Item, tree: Tree) -> bool:
    from .plan import is_code_setting
    if item.kind == "hook":
        return True
    if item.kind == "setting":
        return is_code_setting(item.data.get("app", ""), item.data.get("key", ""))
    if item.kind == "agent":
        raw = tree.read(item.data["file"]) or b""
        return b"{{kit.hook " in raw
    if item.kind in ("skill", "file"):
        return any(is_code_path(rel, item.kind) for rel in item_source_files(module, item, tree))
    return False


class Engine:
    def __init__(self, layout: Layout):
        self.layout = layout
        self.index = Index(layout)
        self.alerts = Alerts(layout)
        self.held = Held(layout)
        self.journal = Journal(layout)
        self.machine: Machine = load_machine(layout)
        self.blocklist = checks.read_terms(layout.blocklist)
        self.checker = checks.Checker(self.blocklist)
        self.record = RunRecord(layout)
        self.report = Report()
        self.approve_new: Set[str] = set()      # item keys approved during setup
        self.replace_existing: Set[str] = set() # "app|module/item" allowed to overwrite unmanaged files
        self.carried: Dict[str, Optional[str]] = {}  # copy or block keys carried back, with their hash when found
        self.restoring = False                  # uninstall: put back files the tool replaced

    # ------------------------------------------------------------ loading

    @property
    def checkout(self) -> Path:
        return self.layout.config

    def load_modules(self) -> Tuple[Optional[Inventory], List[Module]]:
        inventory = load_inventory(self.checkout) if (self.checkout / "kit.toml").is_file() else None
        modules = list(inventory.modules) if inventory else []
        modules += load_local_modules(self.layout)
        names = [module.name for module in modules]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ConfigError(f"module names used by both the config repo and local modules: {', '.join(sorted(duplicates))}")
        return inventory, modules

    def tree(self, module: Module) -> Tree:
        return Tree(module.root)

    # ------------------------------------------------------------ the run

    def run(self, network: bool = True) -> Report:
        self.recover()
        try:
            inventory, modules = self.load_modules()
        except ConfigError as error:
            self.alerts.raise_("config", f"agent-kit-sync cannot read the config repo: {error}", "agent-kit-sync status")
            self.report.errors.append(str(error))
            return self.report
        self.alerts.close("config")
        changes = self.find_local_changes(modules)
        if changes:
            self.carry_back(modules, changes)
        self.commit_local("Sync edits from " + self.machine.name)
        inventory, modules = self.load_modules()
        self.install(modules, incoming=False)
        if network and gitops.has_remote(self.checkout):
            self.merge_remote()
            inventory, modules = self.load_modules()
        self.install(modules, incoming=True)
        if network and gitops.has_remote(self.checkout) and not self.machine.cloud:
            self.push()
        self.index.save()
        self.record.save()
        return self.report

    # ------------------------------------------------------------ crash recovery

    def recover(self) -> None:
        pending = self.journal.pending()
        if not pending:
            return
        for entry in pending.get("entries", []):
            current = read_or_none(Path(entry["path"]))
            if current is not None and sha256(current) == entry["hash"] and entry.get("copy_key"):
                index_entry = entry["index"]
                self.index.copies[entry["copy_key"]] = index_entry
                if entry.get("rendered") is not None:
                    atomic_write(self.index.rendered_path(entry["copy_key"]), current)
        self.index.save()
        self.journal.finish()

    # ------------------------------------------------------------ finding local edits

    def frozen(self, item_key: str) -> bool:
        info = self.index.items.get(item_key, {})
        frozen = info.get("frozen")
        if frozen and self.held.find(frozen) is None:
            info.pop("frozen", None)
            return False
        return bool(frozen)

    def find_local_changes(self, modules: List[Module]) -> List[LocalChange]:
        known = {module.name: module for module in modules}
        changes: List[LocalChange] = []
        for copy_key, entry in sorted(self.index.copies.items()):
            item_key = entry["item_key"]
            if self.frozen(item_key):
                continue
            path = Path(entry["path"])
            current = read_or_none(path)
            module, item = item_key.split("/", 1)
            if current is None:
                changes.append(LocalChange("delete", item_key, module, item, entry["app"], copy_key, path))
            elif sha256(current) != entry["hash"]:
                changes.append(LocalChange("edit", item_key, module, item, entry["app"], copy_key, path, current))
        changes += self._new_skill_files(known)
        for app, path in self._instruction_paths().items():
            text = read_or_none(path)
            try:
                blocks = sections.extract(text.decode("utf-8")) if text is not None else {}
            except sections.MarkerError as error:
                self.alerts.raise_(f"markers:{app}", f"The agent-kit-sync markers in {path} do not pair up: {error}. Fix the markers by hand.", "agent-kit-sync status")
                continue
            self.alerts.close(f"markers:{app}")
            for block_key, entry in sorted(self.index.blocks.items()):
                if entry["app"] != app or self.frozen(entry["item_key"]):
                    continue
                module, item = entry["item_key"].split("/", 1)
                if entry["key"] not in blocks:
                    if text is not None:
                        changes.append(LocalChange("section-delete", entry["item_key"], module, item, app, block_key, path))
                elif sha256(blocks[entry["key"]].encode("utf-8")) != entry["hash"]:
                    changes.append(LocalChange("section-edit", entry["item_key"], module, item, app, block_key, path, blocks[entry["key"]].encode("utf-8")))
        for setting_key, entry in sorted(self.index.settings.items()):
            path = Path(entry["path"])
            try:
                current = settings.read_value(path, entry["key"])
            except settings.SettingsError:
                continue
            written = entry.get("written", settings.MISSING)
            if current is settings.MISSING and "written" not in entry:
                continue
            if json.dumps(current, sort_keys=True, default=str) != json.dumps(written, sort_keys=True, default=str):
                module, item = entry["item_key"].split("/", 1)
                change = LocalChange("setting", entry["item_key"], module, item, entry["app"], setting_key, path)
                change.new = json.dumps(None if current is settings.MISSING else current).encode("utf-8")
                changes.append(change)
        return self._guard_deletions(changes)

    def _instruction_paths(self) -> Dict[str, Path]:
        return {app: self.machine.app_homes[app] / name for app, name in INSTRUCTION_FILES.items() if app in self.machine.apps}

    def _new_skill_files(self, modules: Dict[str, Module]) -> List[LocalChange]:
        """Files an agent added inside an installed skill folder."""
        found: List[LocalChange] = []
        folders: Dict[Tuple[str, str, str], Path] = {}
        for copy_key, entry in self.index.copies.items():
            if entry.get("kind") != "skill":
                continue
            rel = entry["source_rel"]
            module = modules.get(entry["item_key"].split("/", 1)[0])
            if module is None:
                continue
            item = next((i for i in module.of("skill") if i.key == entry["item_key"]), None)
            if item is None:
                continue
            base = item.data["dir"].rstrip("/")
            inner = rel[len(base) + 1:]
            root = Path(entry["path"])
            for _ in PurePosixPath(inner).parts:
                root = root.parent
            folders[(entry["app"], entry["item_key"], base)] = root
        indexed = {real(Path(entry["path"])) for entry in self.index.copies.values()}
        for (app, item_key, base), root in sorted(folders.items()):
            if self.frozen(item_key) or not root.is_dir():
                continue
            module_name = item_key.split("/", 1)[0]
            ignore = modules[module_name].ignore
            for rel, _x in Tree(root).files("", ignore):
                path = root / Path(*PurePosixPath(rel).parts)
                if real(path) not in indexed:
                    data = read_or_none(path)
                    if data is not None:
                        found.append(LocalChange("added", item_key, module_name, item_key.split("/", 1)[1], app, f"{app}|{item_key}|{base}/{rel}", path, data))
        return found

    def _guard_deletions(self, changes: List[LocalChange]) -> List[LocalChange]:
        deleted_items = {change.item_key for change in changes if change.kind in ("delete", "section-delete")}
        vanished_apps = {app for app in self.machine.apps if not self.machine.app_homes[app].is_dir()}
        guarded: Set[str] = set()
        if len(deleted_items) > DELETION_GUARD:
            guarded = deleted_items
            reason = f"{len(deleted_items)} items vanished in one run"
        else:
            guarded = {change.item_key for change in changes if change.kind in ("delete", "section-delete") and change.app in vanished_apps}
            reason = "an agent app's whole folder is missing"
        if not guarded:
            return changes
        for item_key in sorted(guarded):
            entry = self.held.add(
                f"deletion:{item_key}", "deletion", f"{item_key} is missing on this machine, and {reason}, which usually means an app was removed or reset rather than a choice to delete it everywhere.",
                ["restore", "delete-everywhere", "disable-here"], item_key=item_key)
            self.index.items.setdefault(item_key, {})["frozen"] = entry["id"]
            self.report.held.append(entry["id"])
        self.alerts.raise_("deletions", f"agent-kit-sync held {len(guarded)} deletions because {reason}.", "agent-kit-sync held")
        return [change for change in changes if change.item_key not in guarded]

    # ------------------------------------------------------------ carrying edits back

    def _module_item(self, modules: List[Module], item_key: str) -> Tuple[Optional[Module], Optional[Item]]:
        name, item_name = item_key.split("/", 1)
        module = next((m for m in modules if m.name == name), None)
        if module is None:
            return None, None
        return module, next((i for i in module.items if i.name == item_name and i.kind != "setting"), None) or next((i for i in module.items if i.name == item_name), None)

    def carry_back(self, modules: List[Module], changes: List[LocalChange]) -> None:
        # The last edit written wins when two copies of one item were edited; the others are held.
        by_item: Dict[str, List[LocalChange]] = {}
        for change in changes:
            by_item.setdefault(change.item_key, []).append(change)
        for item_key, item_changes in sorted(by_item.items()):
            module, item = self._module_item(modules, item_key)
            if module is None or item is None:
                continue
            try:
                self._carry_item(module, item, item_changes)
            except (render.RenderError, sections.MarkerError, settings.SettingsError, UnicodeDecodeError) as error:
                self._hold_edit(module, item, item_changes, f"agent-kit-sync could not carry this edit back exactly: {error}")

    def _carry_item(self, module: Module, item: Item, changes: List[LocalChange]) -> None:
        for change in changes:
            if change.kind != "setting":
                self.carried[change.copy_key] = sha256(change.new) if change.new is not None else None
        settings_changes = [c for c in changes if c.kind == "setting"]
        for change in settings_changes:
            self._hold_setting(module, item, change)
        changes = [c for c in changes if c.kind != "setting"]
        if not changes:
            return
        deletes = [c for c in changes if c.kind in ("delete", "section-delete")]
        edits = [c for c in changes if c.kind not in ("delete", "section-delete")]
        whole = self._deletes_whole_item(item, deletes, edits)
        if whole:
            if not module.local and self.machine.policy.get("outgoing") == "ask":
                self._hold_edit(module, item, changes, "This machine's policy holds every edit, including deletions, before it leaves this machine.", {}, outgoing=True)
                return
            self._delete_item(module, item)
            self.report.deletions.append(item.key)
            return
        tree = Tree(module.root)
        new_files: Dict[str, Optional[bytes]] = {}
        conflicts: List[LocalChange] = []
        by_source: Dict[str, List[LocalChange]] = {}
        for change in edits + deletes:
            source_rel = self._source_rel(change)
            by_source.setdefault(source_rel, []).append(change)
        for source_rel, group in by_source.items():
            group.sort(key=lambda c: (stat_signature(c.path) or [0, 0])[1] if c.path else 0)
            winner = group[-1]
            results = []
            for change in group:
                results.append(self._source_bytes(module, item, change, tree, source_rel))
            if len({r for r in results}) > 1:
                conflicts.extend(group[:-1])
            new_files[source_rel] = results[-1]
        problems = self._check_sources(module, item, new_files, tree)
        if problems:
            self._hold_edit(module, item, changes, "This edit failed agent-kit-sync's checks: " + "; ".join(str(p) for p in problems[:5]), new_files)
            return
        if not module.local and self._outgoing_held(module, item, tree, new_files):
            self._hold_edit(module, item, changes, "This machine's policy holds edits like this one before they leave it.", new_files, outgoing=True)
            return
        for source_rel, data in new_files.items():
            target = module.root / Path(*PurePosixPath(source_rel).parts)
            if data is None:
                if target.exists():
                    target.unlink()
                    prune_empty_dirs(target.parent, module.root)
            else:
                atomic_write(target, data, executable=self._executable_for(changes, source_rel))
        for change in conflicts:
            self.held.add(f"edit-conflict:{change.copy_key}", "edit-conflict", f"Two copies of {item.key} on this machine were edited differently before a sync. The {self._app_label(changes[-1].app)} edit was kept; this one, from {self._app_label(change.app)}, is saved here.", ["discard", "use-this"], {"edited": change.new or b""}, item_key=item.key)
        self.report.edits.append(item.key)

    def _app_label(self, app: str) -> str:
        return {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "kit": "hook"}.get(app, app)

    def _executable_for(self, changes: List[LocalChange], source_rel: str) -> Optional[bool]:
        from .fsutil import is_executable
        for change in changes:
            if change.path is not None and self._source_rel(change) == source_rel and change.path.exists():
                return is_executable(change.path) or None
        return None

    def _source_rel(self, change: LocalChange) -> str:
        if change.kind in ("section-edit", "section-delete"):
            return self.index.blocks[change.copy_key]["source_rel"]
        if change.kind == "added":
            return change.copy_key.split("|", 2)[2]
        return self.index.copies[change.copy_key]["source_rel"]

    def _deletes_whole_item(self, item: Item, deletes: List[LocalChange], edits: List[LocalChange]) -> bool:
        if not deletes or edits:
            return False
        if item.kind == "section":
            return True
        if item.kind == "skill":
            # The whole skill folder is gone in at least one app.
            apps = {d.app for d in deletes}
            for app in apps:
                entries = [e for e in self.index.copies.values() if e["item_key"] == item.key and e["app"] == app]
                if entries and all(read_or_none(Path(e["path"])) is None for e in entries):
                    return True
            return False
        return item.kind in ("agent", "hook", "file") and any(
            self.index.copies[d.copy_key]["source_rel"] == item.data.get("file", item.data.get("path")) for d in deletes if d.copy_key in self.index.copies)

    def _source_bytes(self, module: Module, item: Item, change: LocalChange, tree: Tree, source_rel: str) -> Optional[bytes]:
        if change.kind in ("delete", "section-delete"):
            return None
        source = tree.read(source_rel)
        if change.kind == "added":
            return change.new
        if change.kind == "section-edit":
            entry = self.index.blocks[change.copy_key]
            old_render = entry["content"]
            edited = change.new.decode("utf-8")
            prepend, edited_base, append = sections.split_local(edited)
            self._update_local_text(module.name, item.name, prepend, append)
            _p, old_base, _a = sections.split_local(old_render)
            values = values_for(self.layout, self.machine, module.name, change.app, {})
            return render.carry_back_text(source.decode("utf-8"), old_base, edited_base, values).encode("utf-8")
        entry = self.index.copies[change.copy_key]
        old_render = self.index.rendered(change.copy_key)
        if old_render is None or source is None:
            raise render.RenderError("the copy this edit started from is not on record")
        kind = entry["render_kind"]
        values = values_for(self.layout, self.machine, module.name, change.app, self._hook_commands())
        if kind == "copy":
            if source != old_render:
                raise render.RenderError("the source changed since this copy was written")
            return change.new
        edited = change.new.decode("utf-8")
        if kind == "text":
            return render.carry_back_text(source.decode("utf-8"), old_render.decode("utf-8"), edited, values).encode("utf-8")
        if kind == "agent-md":
            return render.carry_back_agent_md(source.decode("utf-8"), change.app, old_render.decode("utf-8"), edited, values).encode("utf-8")
        if kind == "agent-codex":
            return render.carry_back_agent_codex(source.decode("utf-8"), old_render.decode("utf-8"), edited, values).encode("utf-8")
        raise render.RenderError(f"unknown copy kind {kind}")

    def _update_local_text(self, module: str, item: str, prepend: Optional[str], append: Optional[str]) -> None:
        for kind, text in (("prepend", prepend), ("append", append)):
            override = self.machine.overrides.get(f"{module}/{item}")
            if text is None or not override or override.get("kind") != kind or not override.get("file"):
                continue
            target = Path(override["file"])
            if not target.is_absolute():
                target = self.layout.root / target
            atomic_write(target, text.encode("utf-8"))

    def _hook_commands(self) -> Dict[str, str]:
        from .plan import hook_command
        commands = {}
        for entry in self.index.hooks:
            key = f"{entry['module']}/{entry['name']}"
            commands[key] = hook_command(self.layout, self.machine, entry["module"], entry["name"])
        return commands

    def _check_sources(self, module: Module, item: Item, new_files: Dict[str, Optional[bytes]], tree: Tree) -> List[checks.Finding]:
        problems: List[checks.Finding] = []
        templated = set()
        if item.kind in ("section", "agent"):
            templated = set(new_files)
        elif item.kind == "skill":
            base = item.data["dir"].rstrip("/")
            templated = {f"{base}/{t}" for t in item.data.get("templates", [])}
        for source_rel, data in new_files.items():
            if data is None:
                continue
            old = tree.read(source_rel)
            problems += self.checker.file(source_rel, data, own=True, templated=source_rel in templated, added_lines=checks.added_line_numbers(old, data))
        return problems

    def _outgoing_held(self, module: Module, item: Item, tree: Tree, new_files: Dict[str, Optional[bytes]]) -> bool:
        policy = self.machine.policy.get("outgoing", "push")
        if policy == "ask":
            return True
        if policy == "ask-code":
            if item.kind == "hook" or item_is_code(module, item, tree):
                return True
            return any(data is not None and is_code_path(rel, item.kind) for rel, data in new_files.items())
        return False

    def _hold_edit(self, module: Module, item: Item, changes: List[LocalChange], summary: str, new_files: Optional[Dict[str, Optional[bytes]]] = None, outgoing: bool = False) -> None:
        data = {}
        for number, (rel, blob) in enumerate(sorted((new_files or {}).items())):
            if blob is not None:
                data[f"{number}"] = blob
        files = {f"{number}": rel for number, (rel, _b) in enumerate(sorted((new_files or {}).items()))}
        entry = self.held.add(f"edit:{item.key}", "outgoing" if outgoing else "edit", summary, ["approve", "discard"] if outgoing else ["discard", "retry"], data, item_key=item.key, sources=files, deleted=[rel for rel, blob in (new_files or {}).items() if blob is None])
        self.index.items.setdefault(item.key, {})["frozen"] = entry["id"]
        self.report.held.append(entry["id"])
        self.alerts.raise_(f"held:{item.key}", f"agent-kit-sync held an edit to {item.key}. {summary}", "agent-kit-sync held")

    def _hold_setting(self, module: Module, item: Item, change: LocalChange) -> None:
        entry = self.index.settings[change.copy_key]
        current = json.loads(change.new.decode("utf-8"))
        if change.copy_key in self.index.always_restore:
            entry["force"] = True
            return
        held = self.held.add(
            f"setting:{change.copy_key}", "setting",
            f"{entry['key']} in {self._app_label(entry['app'])}'s settings now holds {json.dumps(current)}, but agent-kit-sync last wrote {json.dumps(entry.get('written'))}. Another program or a person changed it.",
            ["keep-and-sync", "restore", "always-restore"], {"value": change.new}, item_key=item.key, setting_key=change.copy_key)
        entry["held"] = held["id"]
        self.report.held.append(held["id"])
        self.alerts.raise_(f"setting:{change.copy_key}", f"agent-kit-sync held a change to the setting {entry['key']}, which another program changed.", "agent-kit-sync held")

    def _delete_item(self, module: Module, item: Item) -> None:
        array = item.kind + "s"
        manifest = module.root / "module.toml"
        text = manifest.read_text(encoding="utf-8")
        atomic_write(manifest, tomledit.remove_entry(text, array, item.name).encode("utf-8"))
        tree = Tree(module.root)
        for rel in item_source_files(module, item, tree):
            target = module.root / Path(*PurePosixPath(rel).parts)
            if target.exists():
                target.unlink()
                prune_empty_dirs(target.parent, module.root)
        if item.kind == "skill":
            folder = module.root / item.data["dir"]
            if folder.is_dir():
                shutil.rmtree(folder)

    # ------------------------------------------------------------ git

    def commit_local(self, message: str) -> None:
        """Commit changed files in the checkout that pass the checks; leave the rest uncommitted."""
        if not (self.checkout / ".git").exists():
            return
        paths, failed = [], []
        for entry in gitops.status_porcelain(self.checkout):
            path = entry[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            target = self.checkout / path
            data = read_or_none(target)
            if data is not None:
                old = gitops.show(self.checkout, "HEAD", path) if gitops.head(self.checkout) else None
                templated = path.endswith(".md") and "/sections/" in path or "/agents/" in path
                problems = self.checker.file(path, data, own=True, templated=templated, added_lines=checks.added_line_numbers(old, data))
                if problems:
                    failed.append((path, problems))
                    continue
            paths.append(path)
        for path, problems in failed:
            self.alerts.raise_(f"checkout:{path}", f"agent-kit-sync did not commit {path} in the config repo checkout, because it failed the checks: " + "; ".join(str(p) for p in problems[:3]), "agent-kit-sync test")
        for path in paths:
            self.alerts.close(f"checkout:{path}")
        commit = gitops.commit_all(self.checkout, message, self.machine.git_identity, paths=paths)
        if commit:
            self.record.data["commits"].append(commit)

    def merge_remote(self) -> None:
        try:
            gitops.fetch(self.checkout)
            self.index.last_fetch = time.time()
        except gitops.GitError as error:
            self.alerts.raise_("fetch", f"agent-kit-sync could not fetch the config repo: {error}", "agent-kit-sync status")
            self.report.errors.append(str(error))
            return
        self.alerts.close("fetch")
        remote = gitops.rev(self.checkout, "origin/main")
        local = gitops.head(self.checkout)
        if remote is None or (local and gitops.is_ancestor(self.checkout, remote, local)):
            return
        if local is None:
            gitops.run(self.checkout, "reset", "-q", "--hard", remote)
            self.report.merged = True
            return
        env = gitops.identity_env(self.machine.git_identity)
        result = gitops.run(self.checkout, "merge", "--no-edit", "--no-ff", "-q", "origin/main", check=False, env=env)
        if result.returncode != 0:
            self._resolve_conflicts()
            gitops.run(self.checkout, "commit", "-q", "--no-verify", "--no-edit", env=env)
        self.report.merged = True

    def _resolve_conflicts(self) -> None:
        for path, code in sorted(gitops.unmerged(self.checkout).items()):
            target = self.checkout / path
            if code in ("UU", "AA"):
                ours = gitops.run(self.checkout, "show", f":2:{path}", check=False).stdout
                self.held.add(f"merge:{path}", "merge-conflict", f"This machine and another changed {path} in the same place. The other machine's version was kept; this machine's version is saved here.", ["discard", "use-this"], {"ours": ours}, path=path)
                gitops.run(self.checkout, "checkout", "--theirs", "--", path)
                gitops.run(self.checkout, "add", "--", path)
                self.report.held.append(path)
            elif code == "UD":
                gitops.run(self.checkout, "add", "--", path)
                self.alerts.raise_(f"kept:{path}", f"Another machine deleted {path}, which this machine had edited. The edit was kept.", "agent-kit-sync status")
            elif code == "DU":
                gitops.run(self.checkout, "add", "--", path)
                self.alerts.raise_(f"kept:{path}", f"This machine deleted {path}, which another machine had edited. The edit was kept.", "agent-kit-sync status")
            else:
                if target.exists():
                    gitops.run(self.checkout, "rm", "-q", "--", path)
                else:
                    gitops.run(self.checkout, "rm", "-q", "--cached", "--", path, check=False)

    def push(self) -> None:
        local = gitops.head(self.checkout)
        remote = gitops.rev(self.checkout, "origin/main")
        if local is None or local == remote:
            return
        for attempt in range(2):
            try:
                if gitops.push(self.checkout):
                    self.report.pushed = True
                    self.record.data["pushed"] = True
                    self.alerts.close("push")
                    return
            except gitops.GitError as error:
                self.alerts.raise_("push", f"agent-kit-sync could not push to the config repo: {error}", "agent-kit-sync status")
                return
            if attempt == 0:
                self.merge_remote()
                modules = self.load_modules()[1]
                self.install(modules, incoming=True)
        self.alerts.raise_("push", "agent-kit-sync could not push because another machine keeps pushing first. The next sync will try again.", "agent-kit-sync status")

    # ------------------------------------------------------------ policy for incoming changes

    def _effective_modules(self, modules: List[Module], incoming: bool) -> Tuple[List[Module], Dict[str, Tree]]:
        """Replace held or pinned items with the version this machine last approved."""
        trees: Dict[str, Tree] = {}
        head = gitops.head(self.checkout) if (self.checkout / ".git").exists() else None
        result = []
        present = set()
        for module in modules:
            kept = []
            tree = Tree(module.root)
            for item in module.items:
                present.add(item.key)
                info = self.index.items.get(item.key)
                if module.local or head is None:
                    kept.append(item)
                    continue
                pin = self.machine.overrides.get(item.key) or self.machine.overrides.get(module.name)
                if pin and pin.get("kind") == "pin" and pin.get("commit"):
                    old = self._item_at(module, item.name, pin["commit"])
                    if old is not None:
                        kept.append(old[0])
                        trees[item.key] = old[1]
                    continue
                fingerprint = item_fingerprint(module, item, tree)
                if item.key in self.report.edits:
                    kept.append(item)  # This machine's own edit, carried back in this run.
                    continue
                if info is None or "fp" not in info:
                    if self.machine.policy["new_items"] == "apply" or item.key in self.approve_new:
                        kept.append(item)
                        continue
                    self._hold_incoming(item, "new-item", f"{item.key} is new from the config repo.", head, item_is_code(module, item, tree))
                    continue
                if info["fp"] == fingerprint:
                    kept.append(item)
                    continue
                code = item_is_code(module, item, tree)
                policy = self.machine.policy["code_changes" if code else "other_changes"]
                if policy == "apply" or info.get("approved") == fingerprint:
                    kept.append(item)
                    continue
                old = self._item_at(module, item.name, info["commit"])
                self._hold_incoming(item, "incoming", f"{item.key} changed in the config repo.", head, code)
                if old is not None:
                    kept.append(old[0])
                    trees[item.key] = old[1]
            clone = Module(module.name, module.root, module.local, module.description, module.apps, module.requires, module.ignore, kept, module.source)
            result.append(clone)
        self._incoming_removals(modules, present, head, result, trees)
        return result, trees

    def _incoming_removals(self, modules, present, head, result, trees) -> None:
        for item_key, info in list(self.index.items.items()):
            if item_key in present or "commit" not in info or info.get("local") or item_key in self.report.deletions:
                continue
            policy = self.machine.policy["other_changes"]
            if policy == "apply":
                continue
            module_name, item_name = item_key.split("/", 1)
            module = next((m for m in result if m.name == module_name), None)
            old = self._item_at(Module(module_name, self.checkout / "modules" / module_name, False), item_name, info["commit"])
            if old is None:
                continue
            self._hold_incoming_raw(item_key, "removal", f"{item_key} was removed from the config repo.", head, False)
            if module is None:
                module = Module(module_name, self.checkout / "modules" / module_name, False)
                result.append(module)
            module.items.append(old[0])
            trees[item_key] = old[1]

    def _item_at(self, module: Module, item_name: str, commit: str) -> Optional[Tuple[Item, Tree]]:
        prefix = module.root.relative_to(self.checkout).as_posix()
        tree = CommitTree(self.checkout, commit, prefix)
        try:
            old_module = load_module(module.root, module.name, False, tree=tree)
        except ConfigError:
            return None
        item = next((i for i in old_module.items if i.name == item_name), None)
        return (item, tree) if item is not None else None

    def _hold_incoming(self, item: Item, kind: str, summary: str, head: str, code: bool) -> None:
        self._hold_incoming_raw(item.key, kind, summary, head, code)

    def _hold_incoming_raw(self, item_key: str, kind: str, summary: str, head: str, code: bool) -> None:
        what = "code" if code else "text"
        entry = self.held.add(f"incoming:{item_key}", kind, summary + f" This machine's policy asks before installing {'new items' if kind == 'new-item' else what + ' changes'}.", ["apply", "decline"], item_key=item_key, commit=head)
        if entry["id"] not in self.report.held:
            self.report.held.append(entry["id"])
        self.alerts.raise_(f"incoming:{item_key}", f"agent-kit-sync is holding a change to {item_key} from the config repo for your approval.", "agent-kit-sync held")

    # ------------------------------------------------------------ installing

    def install(self, modules: List[Module], incoming: bool) -> None:
        effective, trees = self._effective_modules(modules, incoming)

        def tree_for(module: Module, item: Item) -> Tree:
            return trees.get(item.key) or Tree(module.root)

        plan = Planner(self.layout, self.machine, effective, tree_for).build()
        for error in plan.errors:
            self.report.errors.append(error)
        self.index.hooks = [
            {"module": h.module, "name": h.name, "path": str(h.path), "events": h.events, "tools": h.tools, "protocol": h.protocol,
             "args": h.args, "runtime": h.runtime, "timeout": h.timeout, "on_failure": h.on_failure, "apps": h.apps, "order": h.order}
            for h in plan.hooks]
        self._write_copies(plan)
        self._write_sections(plan)
        self._write_settings(plan)
        head = gitops.head(self.checkout) if (self.checkout / ".git").exists() else None
        for module in effective:
            for item in module.items:
                tree = tree_for(module, item)
                info = self.index.items.setdefault(item.key, {})
                if item.key in trees and not module.local:
                    continue
                info["fp"] = item_fingerprint(module, item, tree)
                info["commit"] = head
                info["local"] = module.local
                info.pop("approved", None)
        present = {item.key for module in effective for item in module.items}
        for item_key in list(self.index.items):
            if item_key not in present and not self.index.items[item_key].get("frozen"):
                del self.index.items[item_key]
        self.index.save()

    def _write_copies(self, plan: Plan) -> None:
        wanted: Dict[str, Copy] = {}
        for copy in plan.copies:
            if self.frozen(f"{copy.module}/{copy.item}"):
                continue
            wanted[copy.key] = copy
        collisions = self._collisions(wanted)
        for copy_key in list(wanted):
            if f"{wanted[copy_key].app}|{wanted[copy_key].module}/{wanted[copy_key].item}" in collisions:
                del wanted[copy_key]
        seen_targets: Dict[Path, str] = {}
        journal = []
        for copy in wanted.values():
            target = real(copy.path)
            if target in seen_targets:
                continue
            seen_targets[target] = copy.key
            entry = self.index.copies.get(copy.key)
            current = read_or_none(copy.path)
            expected = entry["hash"] if entry else None
            if copy.key in self.carried:
                expected = self.carried[copy.key]
            if entry is not None and current is not None and sha256(current) != expected:
                continue  # Edited since this run started; the next run carries it back.
            if current == copy.data:
                if entry is None or entry["hash"] != sha256(copy.data):
                    self.index.put_copy(copy.key, self._copy_entry(copy), copy.data)
                continue
            journal.append({"path": str(target), "hash": sha256(copy.data), "copy_key": copy.key, "index": self._copy_entry(copy), "rendered": True})
        self.journal.begin(journal)
        for record in journal:
            copy = wanted[record["copy_key"]]
            entry = self.index.copies.get(copy.key)
            current = read_or_none(copy.path)
            self._backup_unmanaged(copy, current, entry)
            self.record.file(real(copy.path), current)
            try:
                atomic_write(copy.path, copy.data, executable=copy.executable, expect=sha256(current) if current is not None else None)
            except ChangedUnderneath:
                continue
            self.index.put_copy(copy.key, record["index"], copy.data)
            self.report.installed.append(copy.key)
        self.index.save()
        self.journal.finish()
        self._remove_stale(set(wanted), plan)

    def _copy_entry(self, copy: Copy) -> dict:
        return {"path": str(copy.path), "app": copy.app, "item_key": f"{copy.module}/{copy.item}", "kind": copy.kind,
                "source_rel": copy.source_rel, "render_kind": copy.render_kind, "hash": sha256(copy.data),
                "executable": copy.executable, "local": copy.local, "sig": None}

    def _collisions(self, wanted: Dict[str, Copy]) -> Set[str]:
        """App items whose destination holds a file the tool did not write."""
        found: Set[str] = set()
        for copy in wanted.values():
            group = f"{copy.app}|{copy.module}/{copy.item}"
            if copy.key in self.index.copies or group in self.replace_existing or copy.app == "kit":
                continue
            current = read_or_none(copy.path)
            if current is not None and current != copy.data:
                found.add(group)
        for group in sorted(found):
            app, item_key = group.split("|", 1)
            entry = self.held.add(f"collision:{group}", "collision", f"{self._app_label(app)} already has a {item_key.split('/', 1)[1]} that agent-kit-sync did not install, and the config repo has one with the same name. Nothing was overwritten.", ["config-replaces-local", "local-replaces-config", "merge", "skip"], item_key=item_key, app=app)
            if entry["id"] not in self.report.held:
                self.report.held.append(entry["id"])
            self.alerts.raise_(f"collision:{group}", f"agent-kit-sync did not overwrite {item_key} in {self._app_label(app)}, because a different one is already there.", "agent-kit-sync held")
        return found

    def _backup_unmanaged(self, copy: Copy, current: Optional[bytes], entry: Optional[dict]) -> None:
        if entry is not None or current is None:
            return
        target = real(copy.path)
        try:
            rel = target.relative_to(real(Path.home()))
        except ValueError:
            rel = Path(*target.parts[1:])
        backup = self.layout.state / "backups" / rel
        if not backup.exists():
            atomic_write(backup, current)

    def _remove_stale(self, wanted: Set[str], plan: Plan) -> None:
        stale = [key for key in self.index.copies if key not in wanted and not self.frozen(self.index.copies[key]["item_key"])]
        for copy_key in stale:
            entry = self.index.copies[copy_key]
            path = Path(entry["path"])
            current = read_or_none(path)
            expected = self.carried.get(copy_key, entry["hash"]) if copy_key in self.carried else entry["hash"]
            if current is not None and sha256(current) != expected:
                continue  # Edited by someone; leave it for the next run to carry back.
            self.record.file(real(path), current, kind="remove")
            if current is not None:
                try:
                    remove_file(path, expect=entry["hash"])
                except ChangedUnderneath:
                    continue
                if entry.get("kind") == "skill":
                    prune_empty_dirs(path.parent, path.parent.parent.parent)
                else:
                    prune_empty_dirs(path.parent, self.layout.installed if entry["app"] == "kit" else path.parent.parent)
            backup = self._restore_backup(path)
            self.index.drop_copy(copy_key)
            self.report.removed.append(copy_key)
        self.index.save()

    def _restore_backup(self, path: Path) -> bool:
        try:
            rel = real(path).relative_to(real(Path.home()))
        except ValueError:
            return False
        backup = self.layout.state / "backups" / rel
        if backup.exists():
            atomic_write(path, backup.read_bytes())
            backup.unlink()
            return True
        return False

    def _write_sections(self, plan: Plan) -> None:
        by_app: Dict[str, List[SectionBlock]] = {}
        for block in plan.sections:
            if not self.frozen(f"{block.module}/{block.item}"):
                by_app.setdefault(block.app, []).append(block)
        frozen_keys = {entry["key"] for entry in self.index.blocks.values() if self.frozen(entry["item_key"])}
        apps = set(by_app) | {entry["app"] for entry in self.index.blocks.values()}
        for app in sorted(apps):
            if app not in self.machine.apps:
                continue
            path = self.machine.app_homes[app] / INSTRUCTION_FILES[app]
            raw = read_or_none(path)
            text = raw.decode("utf-8") if raw is not None else ""
            try:
                present = sections.extract(text)
            except sections.MarkerError:
                continue
            desired = {block.key: block.content for block in by_app.get(app, [])}
            for key in frozen_keys:
                if key in present:
                    desired.setdefault(key, present[key])
            skip = set()
            for block_key, entry in self.index.blocks.items():
                if entry["app"] != app or entry["key"] not in present:
                    continue
                found = sha256(present[entry["key"]].encode("utf-8"))
                expected = self.carried.get(block_key, entry["hash"]) if block_key in self.carried else entry["hash"]
                if found != expected:
                    desired[entry["key"]] = present[entry["key"]]
                    skip.add(entry["key"])
            managed = [entry["key"] for entry in self.index.blocks.values() if entry["app"] == app] + list(desired)
            new_text = sections.apply(text, desired, managed)
            if new_text != text:
                self.record.file(real(path), raw, kind="instructions")
                try:
                    atomic_write(path, new_text.encode("utf-8"), expect=sha256(raw) if raw is not None else None)
                except ChangedUnderneath:
                    continue
            for key in list(self.index.blocks):
                if self.index.blocks[key]["app"] == app and self.index.blocks[key]["key"] not in desired:
                    del self.index.blocks[key]
            for block in by_app.get(app, []):
                if block.key in skip:
                    continue
                self.index.blocks[f"{app}|{block.key}"] = {"app": app, "key": block.key, "item_key": f"{block.module}/{block.item}", "source_rel": block.source_rel, "hash": sha256(block.content.encode("utf-8")), "content": block.content, "local": block.local}
            self._size_alert(app, path, new_text)

    def _size_alert(self, app: str, path: Path, text: str) -> None:
        size = len(text.encode("utf-8"))
        key = f"size:{app}"
        if size > SIZE_LIMIT and key not in self.index.alerted_sizes:
            blocks = sorted(((len(content.encode("utf-8")), block_key) for block_key, content in sections.extract(text).items()), reverse=True)[:5]
            largest = ", ".join(f"{name} ({count:,} bytes)" for count, name in blocks)
            self.alerts.raise_(key, f"{path} is {size:,} bytes, past 32 KiB. Codex cuts off instruction files it finds in project folders at that size. Largest synced sections: {largest or 'none'}.", "agent-kit-sync status")
            self.index.alerted_sizes.append(key)
        elif size <= SIZE_LIMIT and key in self.index.alerted_sizes:
            self.index.alerted_sizes.remove(key)
            self.alerts.close(key)

    def _write_settings(self, plan: Plan) -> None:
        wanted = {}
        for write in plan.settings:
            if self.frozen(write.item_key):
                continue
            wanted[f"{write.app}|{write.key}"] = write
        for setting_key, write in sorted(wanted.items()):
            entry = self.index.settings.get(setting_key)
            if entry and entry.get("held") and self.held.find(entry["held"]):
                continue
            path = self.machine.app_homes[write.app] / SETTINGS_FILES[write.app]
            self._apply_setting(path, write.key, write.value, write.mode, setting_key, write.item_key, write.app, entry)
        for setting_key in list(self.index.settings):
            if setting_key in wanted:
                continue
            entry = self.index.settings[setting_key]
            path = Path(entry["path"])
            current = settings.read_value(path, entry["key"])
            if json.dumps(current, default=str) == json.dumps(entry.get("written"), default=str):
                replaced = entry.get("replaced", None)
                self._change_setting(path, entry["key"], settings.MISSING if entry.get("was_missing") else replaced)
            del self.index.settings[setting_key]

    def _apply_setting(self, path: Path, key: str, value, mode: str, setting_key: str, item_key: str, app: str, entry: Optional[dict]) -> None:
        try:
            current = settings.read_value(path, key)
        except settings.SettingsError as error:
            self.alerts.raise_(f"settings:{path}", f"agent-kit-sync cannot read {path}: {error}", "agent-kit-sync status")
            return
        desired = settings.desired_value(None if current is settings.MISSING else current, value, mode)
        if current is not settings.MISSING and settings.satisfied(current, value, mode):
            if entry is None:
                self.index.settings[setting_key] = {"app": app, "key": key, "path": str(path), "item_key": item_key, "written": current, "was_missing": False, "replaced": current}
            else:
                entry["written"] = current
            return
        if not self._change_setting(path, key, desired):
            return
        if entry is None:
            entry = self.index.settings[setting_key] = {"app": app, "key": key, "path": str(path), "item_key": item_key,
                                                        "was_missing": current is settings.MISSING, "replaced": None if current is settings.MISSING else current}
        entry["written"] = desired
        entry.pop("force", None)
        self.report.installed.append(f"setting {setting_key}")

    def _change_setting(self, path: Path, key: str, value) -> bool:
        fmt = settings.file_format(path)
        for _attempt in range(3):
            raw = read_or_none(path)
            text = raw.decode("utf-8") if raw is not None else ""
            try:
                new_text = settings.change(text, fmt, key, value)
            except settings.SettingsError as error:
                self.alerts.raise_(f"settings:{path}:{key}", f"agent-kit-sync did not change {key} in {path}: {error}", "agent-kit-sync status")
                return False
            if new_text == text:
                return True
            self.record.file(real(path), raw, kind="settings")
            try:
                atomic_write(path, new_text.encode("utf-8"), expect=sha256(raw) if raw is not None else None)
                return True
            except ChangedUnderneath:
                time.sleep(0.05)
        self.alerts.raise_(f"settings:{path}:{key}", f"agent-kit-sync did not change {key} in {path}, because the file kept changing while it wrote. The next sync tries again.", "agent-kit-sync status")
        return False
