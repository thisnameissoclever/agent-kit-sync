"""Cursor's copies of the instruction sections, written as rule files that are never read back.

Cursor does not read ~/.claude/CLAUDE.md. It applies every .mdc rule file in
<user folder>/.cursor/rules to a workspace inside the user folder, does not follow @path
lines in a rule, and reads only the first 102,400 bytes of a rule (Cursor 3.27.22).

Each section meant for Cursor becomes one rule file, with each @path line that names a
support file agent-kit-sync installs replaced by that file's text. These files are output
only: nothing in them is carried back, and nothing done to them can change the config repo.
Edits belong in CLAUDE.md, AGENTS.md, or the installed support file, which all sync.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Cursor reads only the first 102,400 bytes of a rule's text.
RULE_LIMIT = 102400
# Room left under the limit for the header and the marker line.
BODY_LIMIT = RULE_LIMIT - 2048
PREFIX = "agent-kit-sync--"
MARKER = re.compile(r"^<!-- agent-kit-sync rule sha256=([0-9a-f]{64}) -->$", re.M)
HEADER = "---\ndescription: {description}\nalwaysApply: true\n---\n\n"
IMPORT_LINE = re.compile(r"^@(\S.*?)\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
# File names over this many bytes are shortened, so a deep folder stays under Windows' path limit.
NAME_LIMIT = 100


@dataclass
class Rule:
    name: str        # file name in the rules folder
    module: str
    item: str
    text: str        # the whole file, header and marker included


def file_name(module: str, item: str, part: int = 0) -> str:
    """agent-kit-sync--MODULE--ITEM.mdc; module and item names are lowercase letters, digits, and hyphens."""
    stem = f"{PREFIX}{module}--{item}" + (f"--part{part}" if part else "")
    if len(stem) + 4 > NAME_LIMIT:
        digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
        stem = stem[:60] + "--" + digest
    return stem + ".mdc"


def owner(name: str) -> Optional[Tuple[str, str]]:
    """The (module, item) a rule file name belongs to, or None for a shortened or foreign name."""
    if not (name.startswith(PREFIX) and name.endswith(".mdc")):
        return None
    parts = name[len(PREFIX):-4].split("--")
    return (parts[0], parts[1]) if len(parts) >= 2 and parts[0] and parts[1] else None


def normalized(text: str) -> str:
    """The text as agent-kit-sync writes it: no byte order mark, LF line breaks."""
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def body_hash(body: str) -> str:
    return hashlib.sha256(normalized(body).encode("utf-8")).hexdigest()


def compose(module: str, item: str, body: str, part: int = 0, parts: int = 1) -> Rule:
    body = normalized(body).strip("\n") + "\n"
    label = f"{module}/{item}" + (f", part {part} of {parts}" if parts > 1 else "")
    header = HEADER.format(description=f"Instructions agent-kit-sync keeps the same on each of this user's machines ({label})")
    text = header + f"<!-- agent-kit-sync rule sha256={body_hash(body)} -->\n" + body
    return Rule(file_name(module, item, part), module, item, text)


def expand_imports(text: str, support: Dict[str, bytes]) -> Tuple[str, List[str]]:
    """Replace each @path line that names an installed support file with the file's text.
    Returns the text and the @path lines that name no installed file. Lines in code fences stay."""
    def same(path: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.expanduser(path)))
    files = {same(path): data for path, data in support.items()}
    out, unmatched, fenced = [], [], False
    for line in text.splitlines(keepends=True):
        if FENCE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        match = None if fenced else IMPORT_LINE.match(line.rstrip("\r\n"))
        if match:
            data = files.get(same(match.group(1)))
            if data is not None:
                expanded = normalized(data.decode("utf-8", "replace"))
                out.append(expanded if expanded.endswith("\n") else expanded + "\n")
                continue
            unmatched.append(match.group(1))
        out.append(line)
    return "".join(out), unmatched


def split(body: str, limit: int = BODY_LIMIT) -> List[str]:
    """Pieces of the body each under `limit` bytes, cut at blank lines outside code fences."""
    if len(body.encode("utf-8")) <= limit:
        return [body]
    pieces, current, fenced = [], [], False
    size = 0
    for line in body.splitlines(keepends=True):
        if FENCE.match(line):
            fenced = not fenced
        line_size = len(line.encode("utf-8"))
        if current and not fenced and line.strip() == "" and size + line_size > limit * 0.9:
            pieces.append("".join(current))
            current, size = [], 0
            continue
        if current and size + line_size > limit:
            pieces.append("".join(current))  # No blank line close enough: cut at a line break.
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        pieces.append("".join(current))
    return pieces


def plan_rules(blocks: List[Tuple[str, str, str]], support: Dict[str, bytes]) -> Tuple[List[Rule], List[str], List[str]]:
    """Rules for the (module, item, text) of each Cursor section.
    Returns the rules, the unmatched @path lines, and the names of rules still over Cursor's limit."""
    rules, unmatched, oversized = [], [], []
    for module, item, text in blocks:
        body, missing = expand_imports(text, support)
        unmatched.extend(missing)
        pieces = split(body)
        for number, piece in enumerate(pieces, 1):
            rule = compose(module, item, piece, number if len(pieces) > 1 else 0, len(pieces))
            if len(rule.text.encode("utf-8")) > RULE_LIMIT:
                oversized.append(rule.name)
            rules.append(rule)
    return rules, unmatched, oversized


@dataclass
class Outcome:
    written: List[str]
    removed: List[str]
    backed_up: List[str]     # paths of backups of rule files someone edited
    foreign: List[str]       # rule files at agent-kit-sync's names that it did not write
    errors: List[str]


def ours(text: str) -> Optional[str]:
    """The body hash a rule file agent-kit-sync wrote records, or None for a file it did not write."""
    match = MARKER.search(normalized(text))
    return match.group(1) if match else None


def edited(text: str) -> bool:
    """True when a rule file agent-kit-sync wrote was changed since: its body no longer matches its marker."""
    text = normalized(text)
    recorded = ours(text)
    if recorded is None:
        return False
    marker_end = MARKER.search(text).end()
    return body_hash(text[marker_end + 1:]) != recorded


def write_rules(folder: Path, rules: List[Rule], keep: set, backups: Path) -> Outcome:
    """Make the agent-kit-sync rule files in `folder` match `rules`.

    `keep` holds (module, item) pairs whose existing files stay as they are, such as a module
    that failed this run. A file at an agent-kit-sync name without its marker is someone
    else's and is never changed. A file agent-kit-sync wrote and someone edited is backed up
    before it is replaced or removed.
    """
    outcome = Outcome([], [], [], [], [])
    wanted = {rule.name: rule for rule in rules}
    try:
        present = sorted(name for name in os.listdir(folder) if name.startswith(PREFIX) and name.endswith(".mdc"))
    except OSError:
        present = []
    for name in present:
        if name in wanted:
            continue
        owned = owner(name)
        if owned in keep:
            continue
        path = folder / name
        try:
            text = path.read_bytes().decode("utf-8", "replace")
        except OSError as error:
            outcome.errors.append(f"{path}: {error}")
            continue
        if ours(text) is None:
            continue  # Not agent-kit-sync's file; a name it no longer plans is not its concern.
        if edited(text) and not _backup(path, text, backups, outcome):
            continue
        try:
            path.unlink()
            outcome.removed.append(str(path))
        except OSError as error:
            outcome.errors.append(f"{path}: {error}")
    for name, rule in wanted.items():
        path = folder / name
        try:
            current = path.read_bytes().decode("utf-8", "replace") if path.exists() else None
        except OSError as error:
            outcome.errors.append(f"{path}: {error}")
            continue
        if current is not None:
            if current == rule.text:
                continue
            if ours(current) is None:
                outcome.foreign.append(str(path))
                continue
            if edited(current) and not _backup(path, current, backups, outcome):
                continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{name}.{os.getpid()}.tmp")
            temporary.write_bytes(rule.text.encode("utf-8"))
            os.replace(temporary, path)
            outcome.written.append(str(path))
        except OSError as error:
            outcome.errors.append(f"{path}: {error}")
            try:
                temporary.unlink()
            except (OSError, UnboundLocalError):
                pass
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass
    return outcome


def _backup(path: Path, text: str, backups: Path, outcome: Outcome) -> bool:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = backups / f"{path.name}.{stamp}"
    try:
        backups.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        outcome.backed_up.append(str(target))
        return True
    except OSError as error:
        outcome.errors.append(f"{path}: could not save a backup before replacing it: {error}")
        return False
