"""The sync run: find local edits, carry them back, merge, apply policy, install, and push."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from . import checks, gitops, render, sections, settings, toml, tomledit
from .fsutil import ChangedUnderneath, atomic_write, prune_empty_dirs, read_or_none, real, remove_file, sha256, stat_signature
from .model import ConfigError, Inventory, Item, Machine, Module, load_inventory, load_local_modules, load_machine, load_module
from .paths import Layout
from .plan import Copy, Plan, Planner, SectionBlock, SettingWrite, INSTRUCTION_FILES, SETTINGS_FILES, is_code_path, item_templates, values_for
from .state import Alerts, Held, Index, Journal, RunRecord
from .tree import CommitTree, Tree

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
    found: Optional[str] = None   # SHA-256 of the copy or block as it was on disk when the change was found
    source: Optional[str] = None  # an added file's path in the module


def same_line_breaks(edited: bytes, written: Optional[bytes]) -> bytes:
    """An edited text copy with the line breaks of the copy agent-kit-sync wrote.

    Editors on Windows can save a whole file with CRLF line breaks. When every line of the
    edit ends that way and the written copy used LF, the line breaks are formatting, not
    part of the edit, so they are put back before the edit is compared or carried back.
    """
    if written is None or b"\r\n" in written or b"\r\n" not in edited:
        return edited
    if edited.replace(b"\r\n", b"").count(b"\n") or edited.replace(b"\r\n", b"").count(b"\r"):
        return edited  # Mixed line breaks: keep them, so the edit is compared as the user saved it.
    return edited.replace(b"\r\n", b"\n")


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
        from .plan import agent_is_code
        return agent_is_code(tree.read(item.data["file"]) or b"")
    if item.kind in ("skill", "file"):
        return any(is_code_path(rel, item.kind, tree.read(rel), tree.executable(rel)) for rel in item_source_files(module, item, tree))
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
        self.failed_modules: Set[str] = set()   # modules that could not be rendered this run
        self._retest: Set[str] = set()           # unchanged hooks tested again because their module changed
        self._touched: Set[str] = set()          # modules whose installed files change in this sync
        self.held_back: Dict[str, str] = {}       # modules kept as installed because a new version breaks a hook
        self.throttle_fetch = False             # set for runs started after a file write
        self.approve_new: Set[str] = set()      # item keys approved during setup
        self.carry_modules: List[Module] = []   # the modules edits are carried back to, for {{kit.hook}} commands
        self.replace_existing: Set[str] = set() # "app|module/item" allowed to overwrite unmanaged files
        self.carried: Dict[str, Optional[str]] = {}  # copy or block keys carried back, with their hash when found
        self.carried_text: Dict[str, str] = {}       # the same keys, with the hash of the text that was carried back
        self.restoring = False                  # uninstall: put back files the tool replaced

    # ------------------------------------------------------------ loading

    @property
    def checkout(self) -> Path:
        return self.layout.config

    def load_modules(self) -> Tuple[Optional[Inventory], List[Module]]:
        inventory = load_inventory(self.checkout) if (self.checkout / "kit.toml").is_file() else None
        modules = list(inventory.modules) if inventory else []
        modules += load_local_modules(self.layout)
        builtin = Path(__file__).resolve().parent.parent / "kit"
        if (builtin / "module.toml").is_file():
            modules.append(load_module(builtin, "agent-kit-sync", local=True, source="builtin"))
        names = [module.name for module in modules]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ConfigError(f"module names used by both the config repo and local modules: {', '.join(sorted(duplicates))}")
        return inventory, modules

    def tree(self, module: Module) -> Tree:
        return Tree(module.root)

    @staticmethod
    def module_systems(module: Module) -> Tuple[str, ...]:
        """The operating systems a module installs on, for the portability check; none for a local module."""
        if module.local:
            return ()
        wanted = module.requires.get("os")
        if not wanted:
            return checks.SYSTEMS
        return tuple(system for system in checks.SYSTEMS if system in wanted)

    # ------------------------------------------------------------ the run

    def run(self, network: bool = True) -> Report:
        self.recover()
        # Version 0.1.0 raised an alert for instruction files over 32 KiB, citing a Codex limit that
        # does not apply to the files this tool writes. Close any such alert left open by that version.
        for alert in self.alerts.open():
            if alert.get("key", "").startswith("size:"):
                self.alerts.close(alert["key"])
        modules = self._load_or_alert()
        if modules is None:
            return self.report
        changes = self.find_local_changes(modules)
        if changes:
            self.carry_modules = modules
            self.carry_back(modules, changes)
        self.commit_local("Sync edits from " + self.machine.name)
        modules = self._load_or_alert()
        if modules is None:
            return self._finish()
        self.install(modules, incoming=False)
        if network and gitops.has_remote(self.checkout):
            self.merge_remote()
            # A config repo another machine broke stops the run here; this machine keeps what it has installed.
            modules = self._load_or_alert()
            if modules is None:
                return self._finish()
        self.install(modules, incoming=True)
        if network and gitops.has_remote(self.checkout) and not self.machine.cloud:
            self.push()
        return self._finish()

    def _load_or_alert(self) -> Optional[List[Module]]:
        try:
            _inventory, modules = self.load_modules()
        except ConfigError as error:
            self.alerts.raise_("config", f"agent-kit-sync cannot read the config repo: {error}", "agent-kit-sync status")
            self.report.errors.append(str(error))
            return None
        self.alerts.close("config")
        self.alerts.close("status-directory-migration")
        return modules

    def _finish(self) -> Report:
        self.refresh_signatures()
        self.index.save()
        self.record.data["edits"] = sorted(set(self.report.edits))
        self.record.save()
        return self.report

    def refresh_signatures(self) -> None:
        """Save each installed copy's size and modification time, which the dispatcher compares cheaply."""
        for copy_key, entry in self.index.copies.items():
            before = stat_signature(entry["path"])
            current = read_or_none(Path(entry["path"]))
            same = current is not None and (sha256(current) == entry["hash"] or self._line_breaks_only(copy_key, entry, current))
            # A save between the read and the stat would give the edited file the signature of the one
            # read, and the dispatcher would not start a sync for it.
            after = stat_signature(entry["path"]) if same else None
            entry["sig"] = after if after is not None and after == before else None

    # ------------------------------------------------------------ crash recovery

    def recover(self) -> None:
        pending = self.journal.pending()
        if not pending:
            return
        for entry in pending.get("entries", []):
            current = read_or_none(Path(entry["path"]))
            if entry.get("remove"):
                self._recover_removal(entry, current)
                continue
            if current is not None and sha256(current) == entry["hash"] and entry.get("blocks") is not None:
                # An instruction file was written but the index was not: adopt the written blocks.
                try:
                    present = sections.extract(current.decode("utf-8"))
                except (sections.MarkerError, UnicodeDecodeError):
                    present = {}
                for block_key, block_hash in entry["blocks"].items():
                    key = block_key.split("|", 1)[1]
                    if key not in present or block_key not in self.index.blocks:
                        continue
                    # A file saved with Windows line breaks gets its blocks written the same way; see _write_sections.
                    content = same_line_breaks(present[key].encode("utf-8"), b"").decode("utf-8")
                    if sha256(content.encode("utf-8")) == block_hash:
                        self.index.blocks[block_key]["hash"] = block_hash
                        self.index.blocks[block_key]["content"] = content
            if entry.get("blocks") is not None:
                # Blocks the run was removing from this machine: once they are gone from the file, the
                # index must not keep them, or the next run reads them as deletions for every machine.
                try:
                    present = sections.extract(current.decode("utf-8")) if current is not None else {}
                except (sections.MarkerError, UnicodeDecodeError):
                    present = None
                for block_key in entry.get("removed", []) if present is not None else []:
                    if block_key.split("|", 1)[1] not in present:
                        self.index.blocks.pop(block_key, None)
                continue
            if current is not None and sha256(current) == entry["hash"] and entry.get("copy_key"):
                index_entry = entry["index"]
                self.index.copies[entry["copy_key"]] = index_entry
                if entry.get("rendered") is not None:
                    atomic_write(self.index.rendered_path(entry["copy_key"]), current)
        self.index.save()
        self.journal.finish()

    def _recover_removal(self, entry: dict, current: Optional[bytes]) -> None:
        """Finish a removal a run had started: drop the copy from the index once its file is gone,
        and put back the user's own file that the copy had replaced."""
        copy_key = entry["copy_key"]
        if copy_key not in self.index.copies:
            return
        backup = Path(entry["backup"]) if entry.get("backup") else None
        if current is None:
            if backup is not None and backup.exists():
                atomic_write(Path(entry["path"]), backup.read_bytes())
                backup.unlink()
            self.index.drop_copy(copy_key)
        elif backup is not None and not backup.exists():
            self.index.drop_copy(copy_key)  # The user's file was put back and its backup deleted, the removal's last step.
        elif backup is not None and sha256(current) == entry["restored"]:
            # The file holds what the backup does, whether or not the copy was removed first: finish the removal.
            backup.unlink()
            self.index.drop_copy(copy_key)

    # ------------------------------------------------------------ finding local edits

    def forced(self, item_key: str) -> bool:
        return bool(self.index.items.get(item_key, {}).get("force"))

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
            if self.frozen(item_key) or self.forced(item_key):
                continue
            path = Path(entry["path"])
            current = read_or_none(path)
            module, item = item_key.split("/", 1)
            if current is None:
                changes.append(LocalChange("delete", item_key, module, item, entry["app"], copy_key, path))
            elif sha256(current) != entry["hash"]:
                found = sha256(current)
                if entry.get("render_kind", "copy") != "copy":
                    current = same_line_breaks(current, self.index.rendered(copy_key))
                    if sha256(current) == entry["hash"]:
                        continue  # Only the line breaks changed, as an editor on Windows may do; nothing to carry back.
                changes.append(LocalChange("edit", item_key, module, item, entry["app"], copy_key, path, current, found))
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
                if entry["app"] != app or self.frozen(entry["item_key"]) or self.forced(entry["item_key"]):
                    continue
                module, item = entry["item_key"].split("/", 1)
                if entry["key"] not in blocks:
                    if text is not None:
                        changes.append(LocalChange("section-delete", entry["item_key"], module, item, app, block_key, path))
                elif sha256(blocks[entry["key"]].encode("utf-8")) != entry["hash"]:
                    block = same_line_breaks(blocks[entry["key"]].encode("utf-8"), entry.get("content", "").encode("utf-8"))
                    if sha256(block) != entry["hash"]:
                        changes.append(LocalChange("section-edit", entry["item_key"], module, item, app, block_key, path, block,
                                                   sha256(blocks[entry["key"]].encode("utf-8"))))
        for setting_key, entry in sorted(self.index.settings.items()):
            if entry.get("force") or (entry.get("held") and self.held.find(entry["held"])):
                continue
            path = Path(entry["path"])
            try:
                current = settings.read_value(path, entry["key"])
            except settings.SettingsError:
                continue
            written = entry.get("written", settings.MISSING)
            if current is settings.MISSING and "written" not in entry:
                continue
            if entry.get("mode") == "contains":
                wanted = entry.get("value") or []
                if current is not settings.MISSING and settings.satisfied(current, wanted, "contains"):
                    continue  # Other software or the user added entries; that is not a change to the synced ones.
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
                        # The key the planner gives this file's copy, so the write step finds what was carried back.
                        found.append(LocalChange("added", item_key, module_name, item_key.split("/", 1)[1], app, f"{app}|{item_key}|{rel}", path, data,
                                                 sha256(data), f"{base}/{rel}"))
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
        if module.source == "builtin":
            raise render.RenderError("this item comes with agent-kit-sync itself; put local changes in an override instead")
        for change in changes:
            if change.kind != "setting":
                # The copy as it was on disk when the change was found, which can differ from change.new in
                # its line breaks only. A later read could see an edit saved since, and treat it as carried.
                if change.found is not None:
                    self.carried[change.copy_key] = change.found
                else:
                    self.carried[change.copy_key] = sha256(change.new) if change.new is not None else None
                if change.new is not None:
                    self.carried_text[change.copy_key] = sha256(change.new)
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
        conflicts: List[Tuple[LocalChange, LocalChange]] = []   # (the edit held, the edit kept)
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
                conflicts.extend((change, winner) for change in group[:-1])
            new_files[source_rel] = results[-1]
        for change in edits:
            if change.kind in ("edit", "section-edit"):
                self._verify_render(module, item, change, new_files)
        problems = self._check_sources(module, item, new_files, tree)
        if not problems and item.kind == "hook":
            problems = self._run_hook_tests(module, item, new_files)
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
        for change, kept in conflicts:
            self.held.add(f"edit-conflict:{change.copy_key}", "edit-conflict", f"Two copies of {item.key} on this machine were edited differently before a sync. The {self._app_label(kept.app)} edit was kept; this one, from {self._app_label(change.app)}, is saved here.", ["discard", "use-this"], {"edited": change.new or b""}, item_key=item.key)
        self.report.edits.append(item.key)

    def _verify_render(self, module: Module, item: Item, change: LocalChange, new_files: Dict[str, Optional[bytes]]) -> None:
        """Raise RenderError unless rendering the carried-back source reproduces the edited copy exactly."""
        source_rel = self._source_rel(change)
        source = new_files.get(source_rel)
        if source is None:
            return
        if change.kind == "section-edit":
            entry = self.index.blocks[change.copy_key]
            values = values_for(self.layout, self.machine, module.name, change.app, self._hook_commands(module))
            _p, edited_base, _a = sections.split_local(change.new.decode("utf-8"))
            if render.render_text(source.decode("utf-8"), values).text != edited_base:
                raise render.RenderError("rendering the carried-back section does not reproduce the edit")
            self._renders_for_every_app(module, "text", source.decode("utf-8"))
            return
        entry = self.index.copies[change.copy_key]
        kind = entry["render_kind"]
        if kind.startswith("override-replace-"):
            return  # The edit went to this machine's replacement file; the config repo's version is unchanged.
        if kind.startswith("override-local-"):
            # Compare without this machine's own text, which went to its prepend and append files.
            kind = kind[len("override-local-"):]
            edited_text, _prepend, _append = sections.unwrap_local(change.new.decode("utf-8"))
            change = LocalChange(change.kind, change.item_key, change.module, change.item, change.app, change.copy_key, change.path, edited_text.encode("utf-8"))
        values = values_for(self.layout, self.machine, module.name, change.app, self._hook_commands(module))
        text = source.decode("utf-8") if kind != "copy" else None
        if kind == "copy":
            again = source
        elif kind == "text":
            again = render.render_text(text, values).text.encode("utf-8")
        elif kind == "agent-md":
            again = render.render_agent_md(text, change.app, values).text.encode("utf-8")
        else:
            again = render.render_agent_codex(text, values).encode("utf-8")
            if toml.loads(again.decode("utf-8")) == toml.loads(change.new.decode("utf-8")):
                again = change.new  # Same values; Codex reads TOML, so formatting differences do not matter.
        if again != change.new:
            raise render.RenderError("rendering the carried-back source does not reproduce the edited file exactly")
        if text is not None:
            header = item.kind in ("skill", "file") and source_rel.endswith(".md")
            self._renders_for_every_app(module, "text" if kind == "text" else "agent", text, header)

    def _renders_for_every_app(self, module: Module, kind: str, text: str, header: bool = False) -> None:
        """Raise RenderError unless the carried-back source still renders for every app the module installs to, such as a header Codex can hold."""
        from .paths import SUPPORTED_APPS
        for app in [app for app in module.apps if app in SUPPORTED_APPS]:
            values = values_for(self.layout, self.machine, module.name, app, self._hook_commands(module))
            try:
                if kind == "text":
                    render.render_text(text, values, header=header)
                elif app == "codex":
                    render.render_agent_codex(text, values)
                else:
                    render.render_agent_md(text, app, values)
            except render.RenderError as error:
                raise render.RenderError(f"after this edit, the {self._app_label(app)} copy cannot be written: {error}") from None

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
            return change.source
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
            values = values_for(self.layout, self.machine, module.name, change.app, self._hook_commands(module))
            return render.carry_back_text(source.decode("utf-8"), old_base, edited_base, values).encode("utf-8")
        entry = self.index.copies[change.copy_key]
        old_render = self.index.rendered(change.copy_key)
        if old_render is None or source is None:
            raise render.RenderError("the copy this edit started from is not on record")
        kind = entry["render_kind"]
        if kind.startswith("override-replace-"):
            self._write_override_file(module.name, item.name, "replace", change.new)
            return source  # The config repo's version is unchanged.
        if kind.startswith("override-local-"):
            kind = kind[len("override-local-"):]
            edited_text, prepend, append = sections.unwrap_local(change.new.decode("utf-8"))
            old_text, _p, _a = sections.unwrap_local(old_render.decode("utf-8"))
            self._update_local_text(module.name, item.name, prepend, append)
            change = LocalChange(change.kind, change.item_key, change.module, change.item, change.app, change.copy_key, change.path, edited_text.encode("utf-8"))
            old_render = old_text.encode("utf-8")
        values = values_for(self.layout, self.machine, module.name, change.app, self._hook_commands(module))
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

    def _write_override_file(self, module: str, item: str, kind: str, data: bytes) -> None:
        override = self.machine.overrides.get(f"{module}/{item}") or {}
        target = Path(override.get("file", ""))
        if not target.is_absolute():
            target = self.layout.root / target
        atomic_write(target, data)

    def _update_local_text(self, module: str, item: str, prepend: Optional[str], append: Optional[str]) -> None:
        for kind, text in (("prepend", prepend), ("append", append)):
            override = self.machine.overrides.get(f"{module}/{item}")
            if text is None or not override or override.get("kind") != kind or not override.get("file"):
                continue
            target = Path(override["file"])
            if not target.is_absolute():
                target = self.layout.root / target
            atomic_write(target, text.encode("utf-8"))

    def _hook_commands(self, module: Module) -> Dict[str, str]:
        """Commands for every hook an item in `module` can name with {{kit.hook}}.

        These are the hooks in the inventory and the ones installed here. A module in the
        config repo reaches other machines, so it can name only hooks from the config repo;
        a hook from a local module stays a plain command there, and the checks hold it.
        """
        from .plan import hook_command, hook_commands
        commands = {f"{h['module']}/{h['name']}": hook_command(self.layout, self.machine, h["module"], h["name"]) for h in self.index.hooks}
        commands.update(hook_commands(self.layout, self.machine, self.carry_modules))
        if module.local:
            return commands
        local = {m.name for m in self.carry_modules if m.local}
        if self.layout.local_modules.is_dir():
            local |= {folder.name for folder in self.layout.local_modules.iterdir() if folder.is_dir()}
        return {key: command for key, command in commands.items() if key.split("/", 1)[0] not in local}

    def _check_sources(self, module: Module, item: Item, new_files: Dict[str, Optional[bytes]], tree: Tree) -> List[checks.Finding]:
        problems: List[checks.Finding] = []
        templated = set()
        if item.kind in ("section", "agent"):
            templated = set(new_files)
        elif item.kind in ("skill", "file"):
            templated = set(item_templates(item))
        for source_rel, data in new_files.items():
            if data is None:
                continue
            old = tree.read(source_rel)
            header = item.kind == "agent" or (item.kind in ("skill", "file") and source_rel.endswith(".md") and source_rel in templated)
            problems += self.checker.file(source_rel, data, own=True, templated=source_rel in templated, header=header,
                                          added_lines=checks.added_line_numbers(old, data), systems=self.module_systems(module))
        return problems

    def _run_hook_tests(self, module: Module, item: Item, new_files: Dict[str, Optional[bytes]]) -> List[checks.Finding]:
        """Run a hook's tests against the files this machine would install with the edit applied."""
        import tempfile
        from .testing import SKIPPED, UNPLANNED, missing_tests, run_staged_tests
        missing = missing_tests(module, item)
        if missing:
            return [checks.Finding("module-tests", item.data.get("file", item.key), 0, missing)]
        if not item.data.get("tests"):
            return []
        with tempfile.TemporaryDirectory(prefix="aks-module-") as tmp:
            copy_root = Path(tmp) / module.name
            shutil.copytree(module.root, copy_root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
            for rel, data in new_files.items():
                target = copy_root / Path(*PurePosixPath(rel).parts)
                if data is None:
                    if target.exists():
                        target.unlink()
                else:
                    atomic_write(target, data)
            try:
                edited = load_module(copy_root, module.name, module.local, module.source)
            except ConfigError as error:
                return [checks.Finding("module-tests", "module.toml", 0, str(error))]
            edited_item = next((i for i in edited.items if i.key == item.key), None)
            if edited_item is None:
                return []  # The edit removes the hook, so there is nothing left to test.
            # Test against what this machine installs: an item held for approval here is left out, or kept
            # at the version this machine has, so a hook that needs it cannot be pushed to other machines.
            held = {e.get("item_key"): e["kind"] for e in self.held.all() if e["kind"] in ("new-item", "incoming")}
            items: List[Item] = []
            trees: Dict[str, Tree] = {}
            for other in edited.items:
                pin = self.machine.overrides.get(other.key) or self.machine.overrides.get(module.name)
                if other.key != item.key and pin and pin.get("kind") == "pin" and pin.get("commit"):
                    pinned = self._item_at(module, other.name, pin["commit"])
                    if pinned is not None:
                        items.append(pinned[0])
                        trees[other.key] = pinned[1]
                    continue
                kind = held.get(other.key) if other.key != item.key else None
                if kind is None:
                    items.append(other)
                    continue
                info = self.index.items.get(other.key) or {}
                old = self._item_at(module, other.name, info["commit"]) if kind == "incoming" and info.get("commit") else None
                if old is not None:
                    items.append(old[0])
                    trees[other.key] = old[1]
            staged = Module(edited.name, edited.root, edited.local, edited.description, edited.apps, edited.requires, edited.ignore, items, edited.source)
            others = [m for m in (getattr(self, "carry_modules", None) or []) if m.name != module.name]
            failure = run_staged_tests(self.layout, self.machine, staged, others + [staged], edited_item,
                                       lambda m, i: trees.get(i.key) or Tree(m.root))
            if failure == SKIPPED:
                failure = None  # The hook does not run here; each machine that installs it tests it first.
            elif failure == UNPLANNED:
                failure = "agent-kit-sync could not plan this module with the edit applied; run agent-kit-sync status"
            return [checks.Finding("module-tests", edited_item.data.get("file", item.key), 0, failure)] if failure else []

    def _needs_incoming_tests(self, module: Module, item: Item, tree: Tree) -> bool:
        """Whether a new or changed hook from the config repo must pass its tests here before it installs.

        A hook with no tests is refused, and so is one whose tests fail or cannot run on this machine.
        """
        return item.kind == "hook" and not module.local and getattr(tree, "commit", None) is None

    def _outgoing_held(self, module: Module, item: Item, tree: Tree, new_files: Dict[str, Optional[bytes]]) -> bool:
        policy = self.machine.policy.get("outgoing", "push")
        if policy == "ask":
            return True
        if policy == "ask-code":
            if item.kind == "hook" or item_is_code(module, item, tree):
                return True
            if item.kind == "agent":
                from .plan import agent_is_code
                return any(data is not None and agent_is_code(data) for data in new_files.values())
            return any(data is not None and is_code_path(rel, item.kind, data) for rel, data in new_files.items())
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
        from .tree import ignored
        head = gitops.head(self.checkout)
        paths, failed = [], []
        templated_paths, header_paths = self._templated_paths()
        systems = self._systems_by_module()
        for entry in gitops.status_porcelain(self.checkout):
            status, path = entry[:2], entry[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if ignored(path, ()):
                # Never commit a cache or junk file; drop it from the checkout.
                if "?" in status:
                    (self.checkout / path).unlink()
                continue
            target = self.checkout / path
            data = read_or_none(target)
            if data is not None:
                old = gitops.show(self.checkout, "HEAD", path) if head else None
                templated = path in templated_paths or (path.endswith(".md") and "/sections/" in path) or "/agents/" in path
                header = path in header_paths or ("/agents/" in path and path.endswith(".md"))
                parts = PurePosixPath(path).parts
                module_systems = systems.get(parts[1], checks.SYSTEMS) if len(parts) > 2 and parts[0] == "modules" else checks.SYSTEMS
                problems = self.checker.file(path, data, own=True, templated=templated, header=header, added_lines=checks.added_line_numbers(old, data), systems=module_systems)
                if problems:
                    failed.append((path, problems))
                    continue
            paths.append(path)
        test_failures, manifest_fixes = self._hook_test_failures(paths)
        for path, problems in test_failures:
            paths.remove(path)
            failed.append((path, problems))
        for path, (fixed, names, problems) in manifest_fixes.items():
            # Only the failing hooks' entries go back to their last commit; the rest of module.toml is committed.
            target = self.checkout / path
            current = read_or_none(target)
            if current is not None:
                self._hold_checkout_file(path, f"The entries for {', '.join(names)} in {path} failed the checks, so they were set back: " + "; ".join(str(p) for p in problems[:3]), current, fixed)
            atomic_write(target, fixed)
            self.alerts.raise_(f"checkout:{path}", f"agent-kit-sync did not commit the entries for {', '.join(names)} in {path}, because they failed the checks: " + "; ".join(str(p) for p in problems[:3]) + ". They were set back to the last synced version.", "agent-kit-sync status")
        for path, problems in failed:
            # Save the failing file as a held change, then put it back to its last commit so it never blocks a later merge.
            data = read_or_none(self.checkout / path)
            committed = gitops.show(self.checkout, "HEAD", path) if head else None
            if data is not None:
                self._hold_checkout_file(path, f"{path} was changed in the config repo checkout but failed the checks, so it was set back: " + "; ".join(str(p) for p in problems[:3]), data, committed)
            if committed is not None:
                gitops.run(self.checkout, "checkout", "HEAD", "--", path, check=False)
            else:
                # Never committed: unstage it if staged, then delete it.
                gitops.run(self.checkout, "rm", "-fq", "--cached", "--ignore-unmatch", "--", path, check=False)
                remove_file(self.checkout / path)
                prune_empty_dirs((self.checkout / path).parent, self.checkout)
            self.alerts.raise_(f"checkout:{path}", f"agent-kit-sync did not commit {path} in the config repo, because it failed the checks: " + "; ".join(str(p) for p in problems[:3]) + ". It was set back to the last synced version.", "agent-kit-sync status")
        for path in paths:
            if path not in manifest_fixes:
                self.alerts.close(f"checkout:{path}")
        commit = gitops.commit_all(self.checkout, message, self.machine.git_identity, paths=paths)
        if commit:
            self.record.data["commits"].append(commit)
            self._approve_own_items(paths, head, commit)

    def _hold_checkout_file(self, path: str, summary: str, ours: bytes, set_back: Optional[bytes]) -> None:
        """Save a checkout file that was set back, with what it was set back to, so `use-this` can tell if it changed since."""
        data = {"ours": ours}
        if set_back is not None:
            data["set-back"] = set_back
        self.held.add(f"checkout:{path}", "checkout-edit", summary, ["discard", "use-this"], data, path=path, set_back_absent=set_back is None)

    def _approve_own_items(self, paths: List[str], parent: Optional[str], commit: Optional[str] = None) -> None:
        """Items this machine just committed, such as a `remember` entry or an `add`, are its own
        work, so the policy for changes from the config repo does not hold them here.

        An item is approved only when it is new in this commit, or when this machine had
        installed or approved exactly the version before this commit, so a held change from
        another machine is never approved along with it.

        A module whose files this machine commits before kit.toml lists it is approved the
        same way, so listing it later installs it here without asking. The approval holds the
        item's exact fingerprint, so a change another machine makes before it is listed is held.
        """
        touched = {PurePosixPath(path).parts[1] for path in paths if path.startswith("modules/") and len(PurePosixPath(path).parts) > 2}
        if not touched:
            return
        try:
            _inventory, modules = self.load_modules()
        except ConfigError:
            return
        try:
            listed = {str(entry.get("name")) for entry in toml.load_file(self.checkout / "kit.toml").get("modules", [])}
        except (OSError, toml.TOMLDecodeError):
            listed = {module.name for module in modules}
        loaded = {module.name for module in modules}
        for name in sorted(touched - listed - loaded):
            # Not in kit.toml yet: read it from the checkout so its items can be approved now.
            if (self.checkout / "modules" / name / "module.toml").is_file():
                try:
                    modules.append(load_module(self.checkout / "modules" / name, name, local=False))
                except ConfigError:
                    continue
        for module in modules:
            if module.name not in touched or module.local:
                continue
            prefix = module.root.relative_to(self.checkout).as_posix()
            # The commit decides which items are this machine's own, so a hook file whose tests failed and
            # was left out of it is not approved. The approval is the working tree's fingerprint, which is
            # what the install check compares; Git may store the same text with other line breaks.
            committed = CommitTree(self.checkout, commit, prefix) if commit else None
            tree = Tree(module.root)
            previous = self._module_at(module, parent) if parent else None
            if previous is False:
                continue  # The module did not load before this commit, so nothing here is known to be this machine's.
            for item in module.items:
                before = next((i for i in previous[0].items if i.name == item.name), None) if previous else None
                if before is not None:
                    info = self.index.items.get(item.key) or {}
                    old = item_fingerprint(module, before, previous[1])
                    if info.get("fp") != old and self.index.approvals.get(item.key) != old:
                        continue  # Not what this machine had installed or approved; leave it to the policy.
                elif parent and any(gitops.show(self.checkout, parent, f"{prefix}/{rel}") is not None for rel in item_source_files(module, item, tree)):
                    continue  # New to the manifest, but its files were already in the config repo from elsewhere.
                if committed is not None and not self._same_as_committed(module, item, tree, committed):
                    continue  # Part of the item was left out of the commit, such as a hook whose tests failed.
                self.index.approvals[item.key] = item_fingerprint(module, item, tree)

    @staticmethod
    def _same_as_committed(module: Module, item: Item, tree: Tree, committed: Tree) -> bool:
        """True when every file of the item in the working tree is the committed file, ignoring line breaks."""
        for rel in item_source_files(module, item, tree):
            here, there = tree.read(rel), committed.read(rel)
            if here is None or there is None or here.replace(b"\r\n", b"\n") != there.replace(b"\r\n", b"\n"):
                return False
        return True

    def _module_at(self, module: Module, commit: str):
        """The module as it was at `commit`, with its tree; None when it did not exist; False when it did not load."""
        prefix = module.root.relative_to(self.checkout).as_posix()
        tree = CommitTree(self.checkout, commit, prefix)
        if tree.read("module.toml") is None:
            return None
        try:
            return load_module(module.root, module.name, False, tree=tree), tree
        except ConfigError:
            return False

    def _hook_test_failures(self, paths: List[str]) -> Tuple[List[Tuple[str, List[checks.Finding]]], Dict[str, Tuple[bytes, List[str], List[checks.Finding]]]]:
        """Changed checkout files of synced hooks with no tests or with failing tests.

        Returns the hook files to leave uncommitted, with their problems, and, for each module.toml
        that changed a failing hook's entry, that file with only those entries set back to their last
        commit, the hooks' names, and the problems. The rest of module.toml is committed as usual.
        """
        from .testing import missing_tests, run_test
        changed = set(paths)
        touched = {PurePosixPath(path).parts[1] for path in paths if path.startswith("modules/") and len(PurePosixPath(path).parts) > 2}
        if not touched:
            return [], {}
        try:
            _inventory, modules = self.load_modules()
        except ConfigError:
            return [], {}
        failures: Dict[str, List[checks.Finding]] = {}
        manifests: Dict[str, Tuple[bytes, List[str], List[checks.Finding]]] = {}
        head = gitops.head(self.checkout)
        for module in modules:
            if module.name not in touched or module.local or module.source:
                continue
            base = f"modules/{module.name}"
            manifest_rel = f"{base}/module.toml"
            tree = Tree(module.root)
            previous = self._module_at(module, head) if head else None
            before = {item.name: item.data for item in previous[0].of("hook")} if previous else {}
            old_manifest = gitops.show(self.checkout, "HEAD", manifest_rel) if head else None
            for item in module.of("hook"):
                files = {f"{base}/{rel}" for rel in item_source_files(module, item, tree)}
                entry_changed = before.get(item.name) != item.data and manifest_rel in changed
                hit = sorted(files & changed)
                if not hit and not entry_changed:
                    continue
                problem = missing_tests(module, item)
                if problem is None:
                    for rel in item.data["tests"]:
                        result, detail = run_test(module.root, rel)
                        if result == "failed":
                            problem = f"{item.key}: {rel} failed: {detail.strip().splitlines()[-1] if detail.strip() else 'failed'}"
                            break
                if problem is None:
                    continue
                finding = checks.Finding("module-tests", item.data.get("file", item.key), 0, problem)
                for path in hit:
                    failures.setdefault(path, []).append(finding)
                if entry_changed:
                    current = manifests[manifest_rel][0] if manifest_rel in manifests else (read_or_none(self.checkout / manifest_rel) or b"")
                    old_raw = tomledit.entry_text(old_manifest.decode("utf-8"), "hooks", item.name) if old_manifest else None
                    fixed = tomledit.replace_entry(current.decode("utf-8"), "hooks", item.name, old_raw).encode("utf-8")
                    names = (manifests[manifest_rel][1] if manifest_rel in manifests else []) + [item.key]
                    found = (manifests[manifest_rel][2] if manifest_rel in manifests else []) + [finding]
                    manifests[manifest_rel] = (fixed, names, found)
        return sorted(failures.items()), manifests

    def _systems_by_module(self) -> Dict[str, Tuple[str, ...]]:
        """The operating systems each config repo module installs on, by module name."""
        try:
            _inventory, modules = self.load_modules()
        except ConfigError:
            return {}
        return {module.name: self.module_systems(module) for module in modules if not module.local and not module.source}

    def _templated_paths(self) -> Tuple[Set[str], Set[str]]:
        """Config repo paths whose placeholders are replaced, and those among them that may start with a --- header block.

        Placeholders are replaced in sections, subagent definitions, and skill templates. Subagent
        definitions and Markdown skill templates may start with a header block.
        """
        found: Set[str] = set()
        headers: Set[str] = set()
        try:
            _inventory, modules = self.load_modules()
        except ConfigError:
            return found, headers
        for module in modules:
            if module.local or module.source:
                continue
            base = f"modules/{module.name}"
            for item in module.items:
                if item.kind in ("section", "agent"):
                    found.add(f"{base}/{item.data.get('file', '')}")
                    found.update(f"{base}/{rel}" for rel in dict(item.data.get("variants", {})).values())
                    if item.kind == "agent":
                        headers.add(f"{base}/{item.data.get('file', '')}")
                elif item.kind in ("skill", "file"):
                    paths = {f"{base}/{rel}" for rel in item_templates(item)}
                    found |= paths
                    headers |= {path for path in paths if path.endswith(".md")}
        return found, headers

    def merge_remote(self) -> None:
        # A run started after a file write fetches at most once a minute; every other run fetches.
        if not self.throttle_fetch or time.time() - self.index.last_fetch >= 60 or self.report.edits or self.report.deletions:
            try:
                gitops.fetch(self.checkout)
                self.index.last_fetch = time.time()
            except gitops.GitError as error:
                self.alerts.raise_("fetch", f"agent-kit-sync could not fetch the config repo: {error}", "agent-kit-sync status")
                self.report.errors.append(str(error))
                return
            self.alerts.close("fetch")
        upstream = gitops.upstream(self.checkout)
        remote = gitops.rev(self.checkout, upstream)
        local = gitops.head(self.checkout)
        if remote is None or (local and gitops.is_ancestor(self.checkout, remote, local)):
            return
        if local is None:
            gitops.run(self.checkout, "reset", "-q", "--hard", remote)
            self.report.merged = True
            return
        self._set_aside_uncommitted()
        env = gitops.identity_env(self.machine.git_identity)
        if gitops.is_ancestor(self.checkout, local, remote):
            gitops.run(self.checkout, "merge", "-q", "--ff-only", upstream, env=env)
            self.report.merged = True
            return
        result = gitops.run(self.checkout, "merge", "--no-edit", "-q", upstream, check=False, env=env)
        if result.returncode != 0:
            if not gitops.unmerged(self.checkout):
                gitops.run(self.checkout, "merge", "--abort", check=False)
                self.alerts.raise_("merge", f"agent-kit-sync could not merge the config repo: {result.stdout.decode('utf-8', 'replace').strip()[-300:]}", "agent-kit-sync status")
                self.report.errors.append("merge failed")
                return
            self._resolve_conflicts(local, remote)
            gitops.run(self.checkout, "commit", "-q", "--no-verify", "--no-edit", env=env)
        self.report.merged = True

    def _set_aside_uncommitted(self) -> None:
        """Save files left uncommitted in the checkout as held changes, so they never block a merge."""
        for entry in gitops.status_porcelain(self.checkout):
            path = entry[3:].split(" -> ", 1)[-1]
            data = read_or_none(self.checkout / path)
            self._hold_checkout_file(path, f"{path} was changed in the config repo checkout but failed the checks, so it was set aside before merging.", data or b"", gitops.show(self.checkout, "HEAD", path))
        gitops.run(self.checkout, "reset", "-q", "--hard", "HEAD", check=False)
        gitops.run(self.checkout, "clean", "-fdq", check=False)

    def _resolve_conflicts(self, local: str, remote: str) -> None:
        restored_items = set()
        for path, code in sorted(gitops.unmerged(self.checkout).items()):
            target = self.checkout / path
            if code in ("UU", "AA"):
                ours = gitops.run(self.checkout, "show", f":2:{path}", check=False).stdout
                if path.endswith("module.toml"):
                    # A manifest: keep both sides' entries by taking theirs and re-adding ours that are missing.
                    theirs = gitops.run(self.checkout, "show", f":3:{path}", check=False).stdout
                    merged = self._union_manifest(ours, theirs)
                    atomic_write(target, merged)
                else:
                    self.held.add(f"merge:{path}", "merge-conflict", f"This machine and another changed {path} in the same place. The other machine's version was kept; this machine's version is saved here.", ["discard", "use-this"], {"ours": ours}, path=path)
                    gitops.run(self.checkout, "checkout", "--theirs", "--", path)
                    self.report.held.append(path)
                gitops.run(self.checkout, "add", "--", path)
            elif code in ("UD", "DU"):
                # One side deleted, the other edited: the edit wins and the item stays.
                gitops.run(self.checkout, "checkout", "--ours" if code == "UD" else "--theirs", "--", path, check=False)
                gitops.run(self.checkout, "add", "--", path)
                parts = PurePosixPath(path).parts
                if len(parts) >= 2 and parts[0] == "modules":
                    restored_items.add(parts[1])
                who = "Another machine" if code == "UD" else "This machine"
                self.alerts.raise_(f"kept:{path}", f"{who} deleted {path}, which the other machine had edited. The edit was kept, and the item stays.", "agent-kit-sync status")
            else:
                if target.exists():
                    gitops.run(self.checkout, "rm", "-q", "--", path)
                else:
                    gitops.run(self.checkout, "rm", "-q", "--cached", "--", path, check=False)
        for module in restored_items:
            self._restore_manifest_entries(module, local, remote)

    def _union_manifest(self, ours: bytes, theirs: bytes) -> bytes:
        """Take the other machine's module.toml and add back any entries only this machine has."""
        text = theirs.decode("utf-8")
        mine = ours.decode("utf-8")
        for array in ("sections", "skills", "agents", "hooks", "settings", "files"):
            try:
                ours_entries = toml.loads(mine).get(array, [])
                their_names = {e.get("name") for e in toml.loads(text).get(array, [])}
            except toml.TOMLDecodeError:
                return theirs
            for entry in ours_entries:
                name = entry.get("name")
                if name and name not in their_names:
                    raw = tomledit.entry_text(mine, array, name)
                    if raw:
                        text = tomledit.append_entry_text(text, raw)
        return text.encode("utf-8")

    def _restore_manifest_entries(self, module: str, local: str, remote: str) -> None:
        """Put back module.toml entries for items whose files the merge kept because of an edit."""
        manifest_rel = f"modules/{module}/module.toml"
        current = read_or_none(self.checkout / manifest_rel)
        sources = [gitops.show(self.checkout, commit, manifest_rel) for commit in (local, remote)]
        text = current.decode("utf-8") if current else 'description = ""\n'
        for source in sources:
            if source is None:
                continue
            old = source.decode("utf-8")
            try:
                old_data = toml.loads(old)
                names = {(a, e.get("name")) for a in ("sections", "skills", "agents", "hooks", "settings", "files") for e in toml.loads(text).get(a, [])}
            except toml.TOMLDecodeError:
                continue
            for array in ("sections", "skills", "agents", "hooks", "files"):
                for entry in old_data.get(array, []):
                    name = entry.get("name")
                    rel = entry.get("file") or entry.get("dir") or entry.get("path")
                    if name and (array, name) not in names and rel and (self.checkout / "modules" / module / rel).exists():
                        raw = tomledit.entry_text(old, array, name)
                        if raw:
                            text = tomledit.append_entry_text(text, raw)
                            names.add((array, name))
        atomic_write(self.checkout / manifest_rel, text.encode("utf-8"))
        gitops.run(self.checkout, "add", "--", manifest_rel)
        kit = self.checkout / "kit.toml"
        kit_text = kit.read_text(encoding="utf-8") if kit.exists() else "format = 1\n"
        if module not in {m.get("name") for m in toml.loads(kit_text).get("modules", [])}:
            atomic_write(kit, tomledit.append_entry(kit_text, "modules", {"name": module}).encode("utf-8"))
            gitops.run(self.checkout, "add", "--", "kit.toml")

    def push(self) -> None:
        local = gitops.head(self.checkout)
        remote = gitops.rev(self.checkout, gitops.upstream(self.checkout))
        if local is None or local == remote or (remote and gitops.is_ancestor(self.checkout, local, remote)):
            return
        for attempt in range(2):
            try:
                if gitops.push(self.checkout, gitops.branch(self.checkout)):
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
        latest = Index(self.layout)
        for key, value in latest.approvals.items():
            self.index.approvals.setdefault(key, value)
        trees: Dict[str, Tree] = {}
        head = gitops.head(self.checkout) if (self.checkout / ".git").exists() else None
        result = []
        present = set()
        for held in self.held.all():
            if held.get("kind") in ("new-item", "incoming", "removal") and held.get("item_key"):
                self._disabled_incoming(held["item_key"])
        pending: List[Tuple[Module, Item, Optional[str]]] = []  # Hooks to test once every item is decided.
        for module in modules:
            kept = []
            tree = Tree(module.root)
            for item in module.items:
                present.add(item.key)
                if self._disabled_incoming(item.key):
                    kept.append(item)  # Retain hook names for rendering other enabled items.
                    continue
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
                if item.key in self.report.edits or (self.forced(item.key) and self.index.approvals.get("outgoing:" + item.key)):
                    kept.append(item)  # This machine's own edit, carried back or approved in this run.
                    continue
                approved = self.index.approvals.get(item.key) == fingerprint
                if info is None or "fp" not in info:
                    if self.machine.policy["new_items"] == "apply" or item.key in self.approve_new or approved:
                        kept.append(item)
                        if self._needs_incoming_tests(module, item, tree):
                            pending.append((module, item, None))
                        continue
                    self._hold_incoming(item, "new-item", f"{item.key} is new from the config repo.", head, item_is_code(module, item, tree))
                    continue
                if info["fp"] == fingerprint:
                    kept.append(item)
                    if info.get("untested") and self._needs_incoming_tests(module, item, tree):
                        # Never tested here, so no earlier version has run here either: treat it as new.
                        pending.append((module, item, None))
                    continue
                code = item_is_code(module, item, tree)
                policy = self.machine.policy["code_changes" if code else "other_changes"]
                if policy == "apply" or approved:
                    kept.append(item)
                    if self._needs_incoming_tests(module, item, tree):
                        pending.append((module, item, info["commit"]))
                    continue
                old = self._item_at(module, item.name, info["commit"])
                self._hold_incoming(item, "incoming", f"{item.key} changed in the config repo.", head, code)
                if old is not None:
                    kept.append(old[0])
                    trees[item.key] = old[1]
            clone = Module(module.name, module.root, module.local, module.description, module.apps, module.requires, module.ignore, kept, module.source)
            result.append(clone)
        self._incoming_removals(modules, present, head, result, trees)
        self._retest_unchanged_hooks(pending, result, trees)
        self._test_incoming_hooks(pending, result, trees)
        return result, trees

    def _retest_unchanged_hooks(self, pending, result: List[Module], trees: Dict[str, Tree]) -> None:
        """Add the installed hooks of every module whose installed files change in this sync.

        A hook can import a support file, a template, or another hook's file. When any item of its
        module is new, changed, removed, or left out here, the unchanged hook is tested again with the
        files this machine will install, and keeps running only if its tests still pass.
        """
        self._retest = set()
        self._touched: Set[str] = set()
        queued = {item.key for _module, item, _old in pending}
        touched = {module.name for module, item, _old in pending if not (self.index.items.get(item.key) or {}).get("untested")}
        for item_key, info in self.index.items.items():
            module_name = item_key.split("/", 1)[0]
            effective = next((m for m in result if m.name == module_name), None)
            if effective is None or info.get("local"):
                continue
            if not any(i.key == item_key for i in effective.items):
                touched.add(module_name)  # Removed, held, or left out here: its files will be gone.
        for module in result:
            if module.local:
                continue
            current = Tree(module.root)
            installed_items = {entry.get("item_key") for entry in self.index.copies.values()}
            for item in module.items:
                if self._disabled_incoming(item.key):
                    if item.key in installed_items:
                        touched.add(module.name)  # Newly disabled: its installed files are about to go.
                        break
                    continue
                if item.key in trees:
                    continue  # Kept at a pinned or held version: its files do not change.
                info = self.index.items.get(item.key) or {}
                if "fp" not in info or info.get("fp") != item_fingerprint(module, item, current):
                    touched.add(module.name)
                    break
        self._touched = set(touched)
        for module in result:
            if module.name not in touched or module.local:
                continue
            for item in module.items:
                if item.kind != "hook" or item.key in queued or self._disabled_incoming(item.key):
                    continue
                info = self.index.items.get(item.key) or {}
                if "fp" not in info or item.key in trees:
                    continue  # Not installed yet, or kept at a pinned or held version tested with it.
                pending.append((module, item, info.get("commit")))
                queued.add(item.key)
                self._retest.add(item.key)

    def _disabled_incoming(self, item_key: str) -> bool:
        module_name = item_key.split("/", 1)[0]
        if not any(self.machine.overrides.get(key, {}).get("kind") == "disable" for key in (module_name, item_key)):
            return False
        key = f"incoming:{item_key}"
        held = self.held.by_key(key)
        if held is not None:
            self.held.remove(held["id"])
        self.alerts.close(key)
        self.alerts.close(f"tests:{item_key}")  # A disabled hook does not run here, so its test result no longer applies.
        return True

    def _test_incoming_hooks(self, pending, result: List[Module], trees: Dict[str, Tree]) -> None:
        """Run each new or changed hook's tests against the files this machine is about to install.

        The tests run in a folder holding the files this machine installs into the module's folder,
        as the planner renders them: an item held for approval, disabled, or failing its own tests is
        left out, and a pinned or held item has its earlier version. A hook that imports a file this
        machine does not install therefore fails here instead of failing in every agent app.
        """
        from .testing import SKIPPED, UNPLANNED, run_staged_tests
        by_name = {module.name: module for module in result}
        failures: Dict[str, str] = {}
        held_back: Dict[str, str] = {}  # Module name -> why it stays at the installed version this sync.
        self.held_back = held_back
        self.untested: Set[str] = set()

        def tree_for(module: Module, item: Item) -> Tree:
            return trees.get(item.key) or Tree(module.root)
        changed = True
        while changed:
            # Leaving out a failing hook changes what the others are tested with, so test again until nothing changes.
            changed = False
            for module, item, old_commit in pending:
                effective = by_name.get(module.name)
                if item.key in self._retest or item.key in failures or effective is None or item not in effective.items:
                    continue  # Unchanged hooks are tested after the changed ones have settled, below.
                failure = run_staged_tests(self.layout, self.machine, effective, result, item, tree_for)
                if failure == UNPLANNED:
                    continue  # The install fails the whole module, reports why, and keeps its previous copies.
                if failure == SKIPPED:
                    if item.key not in self._retest:
                        self.untested.add(item.key)  # Tested again once it runs on this machine.
                    continue
                if failure is None:
                    self.untested.discard(item.key)
                    continue
                failures[item.key] = failure
                position = effective.items.index(item)
                old = self._item_at(module, item.name, old_commit) if old_commit else None
                if old is not None:
                    effective.items[position] = old[0]  # This machine keeps the version it has, in its place.
                    trees[item.key] = old[1]
                else:
                    del effective.items[position]
                changed = True
        # Every hook that will run in a module whose files change must pass with the files this machine will
        # install: an unchanged hook, a changed hook set back to its earlier version, and a pinned or held
        # hook. Changed hooks still at their new version passed in the last round above. A hook that fails
        # here but passes with the files installed now is broken by the module's new version, so the whole
        # module stays as this machine installed it.
        settled = {item.key for _m, item, _o in pending if item.key not in self._retest and item.key not in failures}
        originals = {module.name: module for module, _i, _o in pending}
        for name in sorted(self._touched | set(originals)):
            effective = by_name.get(name)
            if effective is None or effective.local or name in held_back:
                continue
            module = originals.get(name) or effective
            for item in [i for i in effective.items if i.kind == "hook"]:
                if item.key in settled:
                    continue
                failure = run_staged_tests(self.layout, self.machine, effective, result, item, tree_for)
                if failure in (None, SKIPPED, UNPLANNED):
                    continue
                if self._installed_tests_pass(module, item):
                    held_back[name] = f"{item.key}: {failure}"
                    break
                if item.key in self._retest:
                    failures[item.key] = failure
        for name, failure in held_back.items():
            self.alerts.raise_(f"tests:{name}", f"agent-kit-sync kept module {name} at the version this machine has, because with the new version of the module an installed hook of it fails its tests here. The next sync tries again. {failure[:300]}", "agent-kit-sync test")
        for alert in self.alerts.open():
            key = alert.get("key", "")
            if key.startswith("tests:") and "/" not in key and key[len("tests:"):] not in held_back:
                self.alerts.close(key)
        for module, item, old_commit in pending:
            effective = by_name.get(module.name)
            failure = failures.get(item.key)
            if effective is None:
                continue
            if failure is None or (module.name in held_back and item.key not in self._retest):
                self.alerts.close(f"tests:{item.key}")  # Passed, or the module alert says why nothing changed.
                continue
            if item.key in self._retest:
                if module.name in held_back:
                    self.alerts.close(f"tests:{item.key}")  # The module alert says why.
                else:
                    self.alerts.raise_(f"tests:{item.key}", f"The installed hook {item.key} fails its tests on this machine with the files it has now. {failure[:300]}", "agent-kit-sync test")
                continue
            left_out = sorted(i.key for i in module.items if i.key != item.key and not any(k.key == i.key for k in effective.items))
            note = f" This machine does not install these items from its module yet: {', '.join(left_out)}." if left_out else ""
            if old_commit is None:
                message = f"agent-kit-sync did not install the new hook {item.key} on this machine, because it failed its tests here.{note} {failure[:300]}"
            else:
                message = f"An incoming change to {item.key} failed its tests on this machine, so this machine keeps the earlier version.{note} {failure[:300]}"
            self.alerts.raise_(f"tests:{item.key}", message, "agent-kit-sync test")

    def _installed_tests_pass(self, module: Module, item: Item) -> bool:
        """Whether a hook's tests pass with the files installed for its module on this machine now."""
        import tempfile
        from .testing import run_test
        installed = self.layout.installed_module(module.name)
        if not item.data.get("tests") or not installed.is_dir():
            return False
        with tempfile.TemporaryDirectory(prefix="aks-installed-") as tmp:
            stage = Path(tmp) / module.name
            shutil.copytree(installed, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            return all(run_test(stage, rel)[0] == "passed" for rel in item.data.get("tests", []))

    def _incoming_removals(self, modules, present, head, result, trees) -> None:
        for item_key, info in list(self.index.items.items()):
            if item_key in present or "commit" not in info or info.get("local") or item_key in self.report.deletions:
                continue
            if self._disabled_incoming(item_key):
                continue
            module_name, item_name = item_key.split("/", 1)
            pin = self.machine.overrides.get(item_key) or self.machine.overrides.get(module_name)
            pinned = bool(pin and pin.get("kind") == "pin" and pin.get("commit"))
            if self.machine.policy["other_changes"] == "apply" and not pinned:
                continue
            module = next((m for m in result if m.name == module_name), None)
            old = self._item_at(Module(module_name, self.checkout / "modules" / module_name, False), item_name, pin["commit"] if pinned else info["commit"])
            if old is None:
                continue
            if not pinned:
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

    def _preserve_migrated_references(self, plan: Plan, modules: List[Module] = ()) -> None:
        projects = self.machine.projects
        receipt = self.layout.state / "status-directory-migration.json"
        if projects is None or not receipt.is_file() or not (projects / "status").is_dir():
            return
        names = {projects.name + "_status", projects.name.lower() + "_status", projects.name.upper() + "_status"}
        roots = [projects / "_status"] + [parent / name for parent in (projects, projects.parent) for name in names]
        missing = [path for path in roots if not path.exists()]
        module_roots = {module.name: module.root for module in modules}
        sources = {(module.name, item.name): module.root / item.data.get("file", "module.toml")
                   for module in modules for item in module.items}
        def source_root(name):
            return module_roots.get(name, self.checkout / "modules" / name)
        texts = [(copy.module, copy.data.decode("utf-8", "replace"), source_root(copy.module) / copy.source_rel) for copy in plan.copies]
        texts += [(block.module, block.content, source_root(block.module) / block.source_rel) for block in plan.sections]
        texts += [(module, text, sources.get((module, item), source_root(module) / "module.toml")) for module, item, text in plan.cursor_blocks]
        texts += [(write.item_key.split("/", 1)[0], json.dumps(write.value), source_root(write.item_key.split("/", 1)[0]) / "module.toml") for write in plan.settings]
        texts += [(hook.module, json.dumps(hook.args), source_root(hook.module) / "module.toml") for hook in plan.hooks]
        affected = {}
        for module, text, source in texts:
            text = text.replace("\\\\", "\\").replace("\\", "/")
            for path in missing:
                pattern = re.escape(str(path).replace("\\", "/")) + r"(?=$|[/\s`\"'.,;:)])"
                if re.search(pattern, text, re.I if self.machine.os == "windows" else 0):
                    affected.setdefault(module, (source, path))
                    break
        for module in sorted(affected):
            plan.drop_module(module)
            source, old = affected[module]
            plan.errors.append(f"module {module}: {source} references missing directory {old}. Replace that directory in this source file with {projects / 'status'}, then run agent-kit-sync sync. Existing installed configuration is preserved.")

    def install(self, modules: List[Module], incoming: bool) -> None:
        effective, trees = self._effective_modules(modules, incoming)

        def tree_for(module: Module, item: Item) -> Tree:
            return trees.get(item.key) or Tree(module.root)

        plan = Planner(self.layout, self.machine, effective, tree_for).build()
        self._preserve_migrated_references(plan, effective)
        for name in self.held_back:
            if name not in plan.failed:
                # Keep everything this machine installed for the module, exactly as a module that cannot be planned keeps it.
                plan.drop_module(name)
        self.failed_modules = set(plan.failed)
        for error in plan.errors:
            if error not in self.report.errors:
                self.report.errors.append(error)
        for name in plan.failed:
            if name in self.held_back:
                continue  # Its tests alert says why.
            reason = next((e for e in plan.errors if e.startswith(f"module {name}:")), "")
            self.alerts.raise_(f"module:{name}", f"agent-kit-sync could not install module {name}, so this machine keeps its previous version. {reason}", "agent-kit-sync status")
        for name in {m.name for m in effective} - set(plan.failed):
            self.alerts.close(f"module:{name}")
        previous_hooks = [h for h in self.index.hooks if h["module"] in self.failed_modules]
        hooks = previous_hooks + [
            {"module": h.module, "name": h.name, "path": str(h.path), "events": h.events, "tools": h.tools, "protocol": h.protocol,
             "args": h.args, "runtime": h.runtime, "timeout": h.timeout, "on_failure": h.on_failure, "apps": h.apps, "order": h.order}
            for h in plan.hooks]
        self._write_copies(plan, hooks)
        self._write_sections(plan)
        self.index.save()
        self.write_cursor_rules(plan)
        self._write_settings(plan)
        self.index.save()
        head = gitops.head(self.checkout) if (self.checkout / ".git").exists() else None
        for module in effective:
            if module.name in self.failed_modules:
                continue
            for item in module.items:
                if self._disabled_incoming(item.key):
                    continue  # A disabled source version has not been approved or installed.
                tree = tree_for(module, item)
                info = self.index.items.setdefault(item.key, {})
                if item.key in trees and not module.local:
                    continue
                fingerprint = item_fingerprint(module, item, tree)
                if incoming and info.get("fp") and info["fp"] != fingerprint and info.get("commit") and item.key not in self.report.edits:
                    self.record.data["incoming"][item.key] = info["commit"]
                info["fp"] = fingerprint
                info["commit"] = head
                info["local"] = module.local
                info.pop("approved", None)
                if item.key in getattr(self, "untested", ()):
                    info["untested"] = True
                else:
                    info.pop("untested", None)
        if incoming:
            for key in list(self.index.items):
                if self.index.items[key].get("force"):
                    self.index.clear_force(key)
                if "outgoing:" + key in self.index.approvals:
                    self.index.drop_approval("outgoing:" + key)
        present = {item.key for module in effective for item in module.items}
        for item_key in list(self.index.items):
            if item_key.split("/", 1)[0] in self.failed_modules:
                continue
            if item_key not in present and not self.index.items[item_key].get("frozen") and not (self.restoring and self.index.items[item_key].get("adopted")):
                del self.index.items[item_key]
        self.index.save()

    def _write_copies(self, plan: Plan, hooks: Optional[List[dict]] = None) -> None:
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
        checked: Dict[str, Optional[str]] = {}   # copy key: the hash of the file when it was checked
        rebased = False
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
            if self.forced(f"{copy.module}/{copy.item}") and current is not None:
                expected = sha256(current)
            breaks_only = entry is not None and current is not None and sha256(current) != expected and self._copy_line_breaks_only(copy.key, entry, current)
            if breaks_only and copy.data == self.index.rendered(copy.key):
                continue  # Saved again with other line breaks, and nothing to update: leave it as saved.
            known = entry is not None or copy.key in self.carried
            if known and current is not None and sha256(current) != expected and not breaks_only:
                # Edited since this run started; the next run carries it back.
                if self.carried_text.get(copy.key) == sha256(copy.data):
                    # This run carried an earlier edit to this copy back, and the config repo now
                    # renders to that edit. Record it as the copy written here, so the next run
                    # compares the newer edit with the earlier one and not with the copy before both.
                    self.index.put_copy(copy.key, self._copy_entry(copy), copy.data)
                    self._settled(copy.key)
                    rebased = True
                continue
            if current is None and self._deleted_since_found(copy.key, entry, target, f"{copy.module}/{copy.item}", copy.data, self.index.rendered(copy.key)):
                continue  # Deleted since this run found it; the next run carries the deletion.
            if current == copy.data:
                if entry is None and copy.key not in self.carried:
                    # The file was already here, written by someone else: keep a copy so uninstall leaves it.
                    self._backup_unmanaged(copy, current, None)
                if entry is None or entry["hash"] != sha256(copy.data):
                    self.index.put_copy(copy.key, self._copy_entry(copy), copy.data)
                self._settled(copy.key)
                continue
            checked[copy.key] = sha256(current) if current is not None else None
            journal.append({"path": str(target), "hash": sha256(copy.data), "copy_key": copy.key, "index": self._copy_entry(copy), "rendered": True})
        if rebased:
            self.index.save()  # Rebased entries do not depend on a write, so a crash below must not lose them.
        self.journal.begin(journal)
        for record in journal:
            copy = wanted[record["copy_key"]]
            entry = self.index.copies.get(copy.key)
            current = read_or_none(copy.path)
            if (sha256(current) if current is not None else None) != checked[copy.key]:
                continue  # Changed since it was checked; the next run sees the change.
            backup = None if copy.key in self.carried else self._backup_unmanaged(copy, current, entry)
            try:
                atomic_write(copy.path, copy.data, executable=copy.executable, expect=checked[copy.key])
            except ChangedUnderneath:
                if backup is not None:
                    backup.unlink()  # Nothing was replaced, so there is nothing to put back.
                continue
            self.record.file(real(copy.path), current)
            self.index.put_copy(copy.key, record["index"], copy.data)
            self._settled(copy.key)
            self.report.installed.append(copy.key)
        if hooks is not None:
            # After the hook files are written and before removals, listing only hooks whose file is
            # here, so a saved index never names a missing hook.
            self.index.hooks = [h for h in hooks if Path(h["path"]).is_file()]
        self.index.save()
        self.journal.finish()
        self._remove_stale(set(wanted), plan)

    def _deleted_since_found(self, key: str, entry: Optional[dict], target: Optional[Path], item_key: str, planned: bytes, written: Optional[bytes]) -> bool:
        """Whether a copy or block that is missing now was there when this run found the local changes,
        or was written by this run's first install. Writing it again would undo a deletion the user
        made during the run; left missing, the next run finds the deletion and carries it.

        `planned` is what this install would write, and `written` what the index says was there. A
        change to the item from another machine or another app's copy wins over the deletion, as it
        does when a merge meets a deletion, and the item stays."""
        if entry is None or self.forced(item_key):
            return False
        if target is not None and real(Path(entry["path"])) != target:
            return False  # The copy moves to a new place this run; nothing was deleted.
        if key in self.carried and self.carried[key] is None:
            return False  # Already missing when the run found it.
        if planned == written or self.carried_text.get(key) == sha256(planned):
            return True  # Nothing new, or only the user's own edit, which the user then deleted.
        name = entry.get("path") or f"the {entry['key']} block in {self._app_label(entry['app'])}'s instructions"
        self.alerts.raise_(f"kept:{key}", f"{name} was deleted while agent-kit-sync was syncing, and a change to {item_key} arrived in the same sync. The change was kept, and the item stays. Delete it again to delete it everywhere.", "agent-kit-sync status")
        return False

    def _settled(self, key: str) -> None:
        """Forget that this run carried `key` back, once the index records what is on disk for it or
        what it was rebased to. A run installs twice, before and after it merges the remote, and the
        second install must compare the file with what the first one wrote."""
        self.carried.pop(key, None)
        self.carried_text.pop(key, None)

    def _line_breaks_only(self, copy_key: str, entry: dict, current: bytes) -> bool:
        """True when an installed copy differs from the one written only in its line breaks, which
        find_local_changes does not count as an edit."""
        if entry.get("render_kind", "copy") == "copy":
            return False
        return sha256(same_line_breaks(current, self.index.rendered(copy_key))) == entry["hash"]

    def _copy_line_breaks_only(self, copy_key: str, entry: dict, current: bytes) -> bool:
        """The same, for a copy this run did not carry back, so it can be replaced or removed."""
        return copy_key not in self.carried and self._line_breaks_only(copy_key, entry, current)

    def _copy_entry(self, copy: Copy) -> dict:
        return {"path": str(copy.path), "app": copy.app, "item_key": f"{copy.module}/{copy.item}", "kind": copy.kind,
                "source_rel": copy.source_rel, "render_kind": copy.render_kind, "hash": sha256(copy.data),
                "executable": copy.executable, "local": copy.local, "sig": None}

    def _collisions(self, wanted: Dict[str, Copy]) -> Set[str]:
        """App items whose destination holds a file the tool did not write."""
        found: Set[str] = set()
        for copy in wanted.values():
            group = f"{copy.app}|{copy.module}/{copy.item}"
            if copy.key in self.index.copies or copy.key in self.carried or group in self.replace_existing or self.index.approvals.get("replace:" + group) or copy.app == "kit":
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

    def _backup_unmanaged(self, copy: Copy, current: Optional[bytes], entry: Optional[dict]) -> Optional[Path]:
        """Keep a file the tool did not write before replacing it. Returns the backup when this call made it."""
        if entry is not None or current is None:
            return None
        target = real(copy.path)
        try:
            rel = target.relative_to(real(Path.home()))
        except ValueError:
            rel = Path(*target.parts[1:])
        backup = self.layout.state / "backups" / rel
        if backup.exists():
            return None
        atomic_write(backup, current)
        return backup

    def _remove_stale(self, wanted: Set[str], plan: Plan) -> None:
        failed = set(plan.failed)
        stale = [key for key in self.index.copies if key not in wanted and not self.frozen(self.index.copies[key]["item_key"])
                 and self.index.copies[key]["item_key"].split("/", 1)[0] not in failed]
        removals = []
        for copy_key in stale:
            entry = self.index.copies[copy_key]
            path = Path(entry["path"])
            current = read_or_none(path)
            expected = self.carried.get(copy_key, entry["hash"]) if copy_key in self.carried else entry["hash"]
            breaks_only = current is not None and sha256(current) != expected and self._copy_line_breaks_only(copy_key, entry, current)
            if current is not None and sha256(current) != expected and not breaks_only:
                continue  # Edited by someone; leave it for the next run to carry back.
            # The user's own file comes back when the copy that replaced it is retired. When the copy is
            # already gone, someone deleted it, and bringing the older file back would undo that.
            backup = self._backup_path(path)
            if current is None and backup is not None and backup.exists():
                self._set_aside(backup)
            saved = read_or_none(backup) if backup is not None and current is not None else None
            removals.append((copy_key, entry, path, current, breaks_only, backup if saved is not None else None,
                             {"path": str(path), "copy_key": copy_key, "remove": True,
                              "backup": str(backup) if saved is not None else None, "restored": sha256(saved) if saved is not None else None}))
        if not removals:
            return
        # Record the removals first, so a run that stops halfway is finished by the next run instead of
        # its missing copies being read as deletions to carry to every machine.
        self.journal.begin([removal[-1] for removal in removals])
        for copy_key, entry, path, current, breaks_only, backup, _journal in removals:
            if current is not None:
                try:
                    remove_file(path, expect=sha256(current) if breaks_only else entry["hash"])
                except ChangedUnderneath:
                    continue
                if entry.get("kind") == "skill":
                    prune_empty_dirs(path.parent, path.parent.parent.parent)
                else:
                    prune_empty_dirs(path.parent, self.layout.installed if entry["app"] == "kit" else path.parent.parent)
            restored = self._restore_backup(path, backup)
            self.record.file(real(path), current, kind="remove", backup=restored)
            self.index.drop_copy(copy_key)
            self.report.removed.append(copy_key)
        self.index.save()
        self.journal.finish()

    def _backup_path(self, path: Path) -> Optional[Path]:
        """Where `_backup_unmanaged` keeps the file the user had at `path` before agent-kit-sync."""
        try:
            rel = real(path).relative_to(real(Path.home()))
        except ValueError:
            return None
        return self.layout.state / "backups" / rel

    def _set_aside(self, backup: Path) -> None:
        """Keep a backup under another name, where no later removal at the same path restores it."""
        target = backup.with_name(f"{backup.name}.deleted-{self.record.run_id}")
        atomic_write(target, backup.read_bytes())
        backup.unlink()

    def _restore_backup(self, path: Path, backup: Optional[Path]) -> Optional[Path]:
        """Put the user's own file back at `path`. Returns the backup it came from, now deleted."""
        if backup is None or not backup.exists():
            return None
        atomic_write(path, backup.read_bytes())
        backup.unlink()
        return backup

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
            for entry in self.index.blocks.values():
                if entry["app"] == app and entry["key"].split("/", 1)[0] in getattr(self, "failed_modules", set()) and entry["key"] in present:
                    desired.setdefault(entry["key"], present[entry["key"]])
            for key in frozen_keys:
                if key in present:
                    desired.setdefault(key, present[key])
            skip = set()
            vanished = set()
            if raw is not None:
                for block_key, entry in self.index.blocks.items():
                    if (entry["app"] == app and entry["key"] in desired and entry["key"] not in present and not self.frozen(entry["item_key"])
                            and self._deleted_since_found(block_key, entry, None, entry["item_key"], desired[entry["key"]].encode("utf-8"), entry.get("content", "").encode("utf-8"))):
                        vanished.add(entry["key"])  # Deleted during this run; keep its record so the next run carries the deletion.
                        desired.pop(entry["key"], None)
            skip |= vanished
            # Blocks edited again after this run carried an earlier edit back; see _write_copies.
            rebase = {block.key for block in by_app.get(app, []) if self.carried_text.get(f"{app}|{block.key}") == sha256(block.content.encode("utf-8"))}
            for block_key, entry in self.index.blocks.items():
                if entry["app"] != app or entry["key"] not in present:
                    continue
                found = sha256(present[entry["key"]].encode("utf-8"))
                expected = self.carried.get(block_key, entry["hash"]) if block_key in self.carried else entry["hash"]
                if self.forced(entry["item_key"]):
                    expected = found
                if block_key not in self.carried and found != expected:
                    # A block saved with Windows line breaks is not an edit; see find_local_changes.
                    same = same_line_breaks(present[entry["key"]].encode("utf-8"), entry.get("content", "").encode("utf-8"))
                    if sha256(same) == expected:
                        expected = found
                        if desired.get(entry["key"]) == entry.get("content"):
                            desired[entry["key"]] = present[entry["key"]]  # Nothing to update: leave it as saved.
                if found != expected:
                    desired[entry["key"]] = present[entry["key"]]
                    skip.add(entry["key"])
            early = [block for block in by_app.get(app, []) if block.key in rebase and block.key in skip]
            for block in early:
                self.index.blocks[f"{app}|{block.key}"] = {"app": app, "key": block.key, "item_key": f"{block.module}/{block.item}", "source_rel": block.source_rel, "hash": sha256(block.content.encode("utf-8")), "content": block.content, "local": block.local}
                self._settled(f"{app}|{block.key}")
            if early:
                self.index.save()  # These do not depend on the write below, so a failed write or a crash must not lose them.
            managed = [entry["key"] for entry in self.index.blocks.values() if entry["app"] == app] + list(desired)
            unwrap = []
            if self.restoring:
                for entry in self.index.blocks.values():
                    adopted = self.index.items.get(entry["item_key"], {}).get("adopted", [])
                    if entry["app"] == app and str(real(path)) in adopted:
                        unwrap.append(entry["key"])
            new_text = sections.apply(text, desired, managed, unwrap)
            if raw is not None and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""):
                # Saved with Windows line breaks throughout: write the blocks the same way, so the file does not
                # end up with LF lines inside a CRLF file. Blocks are compared with their line breaks restored.
                new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
            if new_text != text:
                # Record what is about to be written, so a crash between the write and the index save
                # is finished by the next run instead of being read as an edit.
                pending = {f"{app}|{block.key}": sha256(block.content.encode("utf-8")) for block in by_app.get(app, [])}
                removed = [key for key, entry in self.index.blocks.items() if entry["app"] == app and entry["key"] not in desired and entry["key"] not in vanished]
                self.journal.begin([{"path": str(real(path)), "hash": sha256(new_text.encode("utf-8")), "blocks": pending, "removed": removed, "app": app}])
                try:
                    atomic_write(path, new_text.encode("utf-8"), expect=sha256(raw) if raw is not None else None)
                except ChangedUnderneath:
                    self.journal.finish()
                    continue
                self.record.file(real(path), raw, kind="instructions")
            for key in list(self.index.blocks):
                if self.index.blocks[key]["app"] == app and self.index.blocks[key]["key"] not in desired and self.index.blocks[key]["key"] not in vanished:
                    del self.index.blocks[key]
            for block in by_app.get(app, []):
                if block.key in skip and block.key not in rebase:
                    continue
                self.index.blocks[f"{app}|{block.key}"] = {"app": app, "key": block.key, "item_key": f"{block.module}/{block.item}", "source_rel": block.source_rel, "hash": sha256(block.content.encode("utf-8")), "content": block.content, "local": block.local}
                self._settled(f"{app}|{block.key}")
            self.index.save()
            self.journal.finish()

    def write_cursor_rules(self, plan: Plan, everything_removed: bool = False) -> None:
        """Write Cursor's rule files from the sections meant for Cursor. They are output only: nothing
        in them is carried back, so no state of Cursor's rules folder can change the config repo, and
        a problem with them raises an alert instead of stopping the run."""
        try:
            self._write_cursor_rules(plan, everything_removed)
        except Exception as error:  # noqa: BLE001 - Cursor's output must never stop a sync.
            self.alerts.raise_("cursor-rules", f"agent-kit-sync could not write Cursor's rule files: {error}", "agent-kit-sync status")

    def _write_cursor_rules(self, plan: Plan, everything_removed: bool) -> None:
        from . import cursor_rules
        folder = Path.home() / ".cursor" / "rules"   # The folder Cursor reads; it has no setting to move it.
        backups = self.layout.state / "backups" / "cursor-rules"
        if "cursor" not in self.machine.apps or everything_removed:
            # Cursor left this machine, or agent-kit-sync is being removed: take only its own files away.
            outcome = cursor_rules.write_rules(folder, [], set(), backups)
            for key in ("cursor-rules", "cursor-rule-import", "cursor-rule-size", "cursor-rule-foreign"):
                self.alerts.close(key)
            self._close_edited_notice()
            if outcome.backed_up:
                self._alert_list("cursor-rule-edited", outcome.backed_up, "agent-kit-sync removed its Cursor rule files, and saved the ones that had been edited as: {listed}")
            if outcome.errors:
                self._alert_list("cursor-rules", outcome.errors, "agent-kit-sync could not remove some of its Cursor rule files: {listed}")
            return
        support = {str(copy.path): copy.data for copy in plan.copies if copy.app == "kit" and copy.kind == "file"}
        failed = set(plan.failed)
        for entry in self.index.copies.values():
            # A failed module keeps its earlier installed files; a section importing one still gets its text.
            if entry.get("app") == "kit" and entry.get("kind") == "file" and entry.get("item_key", "").split("/", 1)[0] in failed:
                path = Path(entry["path"])
                try:
                    data = read_or_none(path)
                except OSError:
                    data = None
                if data is not None:
                    support.setdefault(str(path), data)
        blocks = [b for b in plan.cursor_blocks if not self.frozen(f"{b[0]}/{b[1]}")]
        planned = cursor_rules.plan_rules(blocks, support)
        # A module that failed this run, or an item with a held edit, keeps its rule files as they are.
        keep = {(module, item) for module, item in cursor_rules.owners(folder) if module in failed or self.frozen(f"{module}/{item}")}
        outcome = cursor_rules.write_rules(folder, planned.rules, keep, backups)
        self._alert_list("cursor-rule-import", planned.unmatched + planned.unreadable, "Cursor does not follow @path lines, and these lines in synced sections name no file agent-kit-sync installs as text, so Cursor gets the bare line: {listed}. Install the file with a [[files]] item, or write its text into the section.")
        self._alert_list("cursor-rule-size", planned.oversized, f"Cursor reads only the first {cursor_rules.RULE_LIMIT:,} bytes of a rule, and these rule files are still larger after splitting: {{listed}}. Shorten the section's longest paragraph.")
        self._alert_list("cursor-rule-foreign", outcome.foreign, "agent-kit-sync did not write these Cursor rule files, because a file it did not write is at the same name: {listed}. Rename the file, then run agent-kit-sync sync.")
        self._alert_list("cursor-rules", outcome.errors, "agent-kit-sync could not write some of Cursor's rule files: {listed}")
        if outcome.backed_up:
            self._alert_list("cursor-rule-edited", outcome.backed_up, "Cursor's rule files are written by agent-kit-sync and never carried back, so edits in them were replaced. Make the change in CLAUDE.md, AGENTS.md, or the installed file instead. The edited files were saved as: {listed}")
        else:
            self._close_edited_notice()

    def _close_edited_notice(self) -> None:
        """The notice of replaced edits stays a day, so every app's next session shows it, then closes."""
        for alert in self.alerts.open():
            if alert.get("key") == "cursor-rule-edited" and time.time() - float(alert.get("time", 0)) > 86400:
                self.alerts.close("cursor-rule-edited")

    def _alert_list(self, key: str, items: List[str], message: str) -> None:
        if not items:
            self.alerts.close(key)
            return
        unique = sorted(set(items))
        listed = ", ".join(unique[:5]) + (f", and {len(unique) - 5} more" if len(unique) > 5 else "")
        self.alerts.raise_(key, message.format(listed=listed), "agent-kit-sync status")

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
            if entry["item_key"].split("/", 1)[0] in getattr(self, "failed_modules", set()):
                continue
            path = Path(entry["path"])
            current = settings.read_value(path, entry["key"])
            if entry.get("mode") == "contains" and isinstance(current, list):
                # Remove only the entries this tool added; keep everything else in the list.
                added = entry.get("added", entry.get("value") or [])
                self._change_setting(path, entry["key"], [v for v in current if v not in added])
            elif json.dumps(current, default=str) == json.dumps(entry.get("written"), default=str):
                replaced = entry.get("replaced", None)
                if entry.get("was_missing"):
                    self._change_setting(path, entry["key"], settings.MISSING)
                elif replaced is not None and json.dumps(replaced, default=str) != json.dumps(current, default=str):
                    self._change_setting(path, entry["key"], replaced)
            del self.index.settings[setting_key]

    def _apply_setting(self, path: Path, key: str, value, mode: str, setting_key: str, item_key: str, app: str, entry: Optional[dict]) -> None:
        try:
            current = settings.read_value(path, key)
        except settings.SettingsError as error:
            self.alerts.raise_(f"settings:{path}", f"agent-kit-sync cannot read {path}: {error}", "agent-kit-sync status")
            return
        desired = settings.desired_value(None if current is settings.MISSING else current, value, mode)
        changed_elsewhere = False
        if entry is not None and current is not settings.MISSING:
            changed_elsewhere = json.dumps(current, sort_keys=True, default=str) != json.dumps(entry.get("written"), sort_keys=True, default=str)
        if mode == "contains" and changed_elsewhere and settings.satisfied(current, value, mode):
            changed_elsewhere = False
        if entry is not None and not entry.get("force") and changed_elsewhere:
            return  # Changed by something else and not yet decided; find_local_changes holds it.
        if current is not settings.MISSING and settings.satisfied(current, value, mode):
            if entry is None:
                entry = self.index.settings[setting_key] = {"app": app, "key": key, "path": str(path), "item_key": item_key, "written": current, "was_missing": False, "replaced": current}
            else:
                entry["written"] = current
                entry.pop("force", None)
            entry["mode"], entry["value"] = mode, value
            return
        if not self._change_setting(path, key, desired):
            return
        if entry is None:
            entry = self.index.settings[setting_key] = {"app": app, "key": key, "path": str(path), "item_key": item_key,
                                                        "was_missing": current is settings.MISSING, "replaced": None if current is settings.MISSING else current}
        entry["written"] = desired
        entry["mode"], entry["value"] = mode, value
        if mode == "contains" and isinstance(entry.get("replaced"), list):
            entry["added"] = [v for v in value if v not in entry["replaced"]]
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
            try:
                old_value = settings.read_value(path, key)
            except settings.SettingsError:
                old_value = settings.MISSING
            try:
                atomic_write(path, new_text.encode("utf-8"), expect=sha256(raw) if raw is not None else None)
                self.record.data.setdefault("settings", []).append({"path": str(path), "key": key, "missing": old_value is settings.MISSING, "old": None if old_value is settings.MISSING else old_value})
                return True
            except ChangedUnderneath:
                time.sleep(0.05)
        self.alerts.raise_(f"settings:{path}:{key}", f"agent-kit-sync did not change {key} in {path}, because the file kept changing while it wrote. The next sync tries again.", "agent-kit-sync status")
        return False
