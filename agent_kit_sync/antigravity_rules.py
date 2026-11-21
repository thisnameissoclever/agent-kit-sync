"""Antigravity's managed plugin manifest and rule files, written as output only.

Antigravity reads rule files in ~/.gemini/config/plugins/agent-kit-sync/rules/*.md when the
plugin is enabled, expands @[label](path) includes up to 24,000 bytes, and leaves ~/.gemini/GEMINI.md
for the user's own rules. Each section meant for Antigravity becomes one or more Markdown rule
files with any @path lines naming installed support files expanded inline, split under the
24,000-byte per-file cap. These files are output only: nothing in them is carried back.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import VERSION, cursor_rules
from .fsutil import ChangedUnderneath, atomic_write, read_or_none, remove_file, sha256
from .paths import ANTIGRAVITY_PLUGIN_NAME

# Antigravity caps each rule file at 24,000 bytes after expanding includes.
RULE_LIMIT = 24000
# Room left under the cap for the frontmatter header, marker line, and closing a split code fence.
BODY_LIMIT = RULE_LIMIT - 1024
EXT = ".md"
PREFIX = cursor_rules.PREFIX
NAME_LIMIT = cursor_rules.NAME_LIMIT
HEADER = "---\ntrigger: always_on\n---\n\n"
PLUGIN_MANIFEST = {
    "name": ANTIGRAVITY_PLUGIN_NAME,
    "displayName": ANTIGRAVITY_PLUGIN_NAME,
    "version": VERSION,
    "description": "Skills, rules, subagents, and lifecycle hooks synced across machines and coding agents by agent-kit-sync.",
}


def file_name(module: str, item: str, part: int = 0) -> str:
    """agent-kit-sync--MODULE--ITEM.md; module and item names are lowercase letters, digits, and hyphens."""
    stem = f"{PREFIX}{module}--{item}" + (f"--part{part}" if part else "")
    if len(stem) + len(EXT) > NAME_LIMIT:
        digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
        stem = stem[:60] + "--" + digest
    return stem + EXT


def compose(module: str, item: str, body: str, part: int = 0, parts: int = 1) -> cursor_rules.Rule:
    del parts  # Kept to match cursor_rules.compose's signature; HEADER carries no part label.
    body = cursor_rules.normalized(body).strip("\n") + "\n"
    name = file_name(module, item, part)
    marker = f"<!-- agent-kit-sync rule file={name} item={module}/{item} sha256={cursor_rules._hash(HEADER + body)} -->\n"
    return cursor_rules.Rule(name, module, item, HEADER + marker + body)


def plan_rules(blocks: List[Tuple[str, str, str]], support: Dict[str, bytes]) -> cursor_rules.Planned:
    """Rules for the (module, item, text) of each Antigravity section."""
    planned = cursor_rules.Planned()
    for module, item, text in blocks:
        body, missing, unreadable = cursor_rules.expand_imports(text, support)
        planned.unmatched.extend(missing)
        planned.unreadable.extend(unreadable)
        pieces = cursor_rules.split(body, BODY_LIMIT)
        for number, piece in enumerate(pieces, 1):
            rule = compose(module, item, piece, number if len(pieces) > 1 else 0, len(pieces))
            if len(rule.text.encode("utf-8")) > RULE_LIMIT:
                planned.oversized.append(rule.name)
            planned.rules.append(rule)
    return planned


def write_rules(folder: Path, rules: List[cursor_rules.Rule], keep: Set[Tuple[str, str]], backups: Path) -> cursor_rules.Outcome:
    """Make agent-kit-sync's Antigravity rule files in `folder` match `rules`."""
    outcome = cursor_rules.Outcome()
    wanted = {rule.name: rule for rule in rules}
    try:
        present = sorted(name for name in os.listdir(folder) if name.startswith(PREFIX) and name.endswith(EXT))
    except FileNotFoundError:
        present = []
    except OSError as error:
        outcome.errors.append(f"{folder}: {error}")
        return outcome
    for name in present:
        if name in wanted:
            continue
        path = folder / name
        state, raw = cursor_rules._read(path, outcome)
        if state != "file":
            continue
        mark = cursor_rules.read_mark(raw.decode("utf-8", "replace"))
        if mark is None or mark.name != name or (mark.module, mark.item) in keep:
            continue
        if mark.edited and not cursor_rules._backup(path, raw, backups, outcome):
            continue
        try:
            if sha256(path.read_bytes()) != sha256(raw):
                continue
            path.unlink()
            outcome.removed.append(str(path))
        except OSError as error:
            outcome.errors.append(f"{path}: {error}")
    for name, rule in wanted.items():
        path = folder / name
        state, raw = cursor_rules._read(path, outcome)
        if state == "error":
            continue
        if state == "other":
            outcome.foreign.append(str(path))
            continue
        if state == "file":
            if raw == rule.text.encode("utf-8"):
                continue
            mark = cursor_rules.read_mark(raw.decode("utf-8", "replace"))
            if mark is None or mark.name != name:
                outcome.foreign.append(str(path))
                continue
            if mark.edited and not cursor_rules._backup(path, raw, backups, outcome):
                continue
        try:
            atomic_write(path, rule.text.encode("utf-8"), expect=sha256(raw) if state == "file" else None)
            outcome.written.append(str(path))
        except ChangedUnderneath:
            continue
        except OSError as error:
            outcome.errors.append(f"{path}: {error}")
    if not wanted or outcome.removed:
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass
    return outcome


def owners(folder: Path) -> Set[Tuple[str, str]]:
    """The (module, item) of each rule file agent-kit-sync wrote in `folder`, read from the markers."""
    found = set()
    try:
        names = [name for name in os.listdir(folder) if name.startswith(PREFIX) and name.endswith(EXT)]
    except OSError:
        return found
    for name in names:
        try:
            mark = cursor_rules.read_mark((folder / name).read_bytes().decode("utf-8", "replace"))
        except OSError:
            continue
        if mark is not None and mark.name == name:
            found.add((mark.module, mark.item))
    return found


def ensure_manifest(plugin_dir: Path) -> Optional[str]:
    """Write or update plugin.json when absent or owned by agent-kit-sync. Returns an error message or None."""
    manifest = plugin_dir / "plugin.json"
    desired = (json.dumps(PLUGIN_MANIFEST, indent=2) + "\n").encode("utf-8")
    try:
        info = os.lstat(manifest)
    except FileNotFoundError:
        try:
            atomic_write(manifest, desired, expect=None)
            return None
        except (ChangedUnderneath, OSError) as error:
            return f"{manifest}: {error}"
    except OSError as error:
        return f"{manifest}: {error}"
    if not stat.S_ISREG(info.st_mode):
        return f"{manifest} is not a regular file"
    raw = read_or_none(manifest)
    if raw is None:
        return f"{manifest} could not be read"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return f"{manifest} is not valid JSON"
    if not isinstance(parsed, dict) or parsed.get("name") != ANTIGRAVITY_PLUGIN_NAME:
        return f"{manifest} already exists for another plugin"
    if raw == desired:
        return None
    try:
        atomic_write(manifest, desired, expect=sha256(raw))
        return None
    except (ChangedUnderneath, OSError) as error:
        return f"{manifest}: {error}"


def remove_manifest(plugin_dir: Path) -> Optional[str]:
    """Remove plugin.json when owned by agent-kit-sync, and prune `plugin_dir` if empty."""
    manifest = plugin_dir / "plugin.json"
    try:
        info = os.lstat(manifest)
    except FileNotFoundError:
        _rmdir_if_empty(plugin_dir)
        return None
    except OSError as error:
        return f"{manifest}: {error}"
    if not stat.S_ISREG(info.st_mode):
        return f"{manifest} is not a regular file"
    raw = read_or_none(manifest)
    if raw is None:
        return f"{manifest} could not be read"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return f"{manifest} is not valid JSON"
    if not isinstance(parsed, dict) or parsed.get("name") != ANTIGRAVITY_PLUGIN_NAME:
        return f"{manifest} already exists for another plugin"
    try:
        remove_file(manifest, expect=sha256(raw))
    except (ChangedUnderneath, OSError) as error:
        return f"{manifest}: {error}"
    _rmdir_if_empty(plugin_dir)
    return None


def _rmdir_if_empty(folder: Path) -> None:
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass
