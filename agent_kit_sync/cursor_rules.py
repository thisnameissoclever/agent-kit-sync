"""Cursor's copies of the instruction sections, written as rule files that are never read back.

Cursor does not read ~/.claude/CLAUDE.md. It applies every .mdc rule file in
<user folder>/.cursor/rules to a workspace inside the user folder, does not follow @path
lines in a rule, and reads only the first 102,400 bytes of a rule (Cursor 3.27.22).

Each section meant for Cursor becomes one rule file, with each @path line that names a
support file agent-kit-sync installs replaced by that file's text. These files are output
only: nothing in them is carried back, and nothing done to them can change the config repo.
Edits belong in CLAUDE.md, AGENTS.md, or the installed support file, which all sync.

Each file holds a marker line that records its own file name, the module and item it comes
from, and a hash of the rest of the file. agent-kit-sync treats a file as its own only when
the marker names that file, and as edited when the rest of the file no longer matches.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .fsutil import ChangedUnderneath, atomic_write

# Cursor reads only the first 102,400 bytes of a rule's text.
RULE_LIMIT = 102400
# Room left under the limit for the header, the marker line, and closing a split code fence.
BODY_LIMIT = RULE_LIMIT - 2048
PREFIX = "agent-kit-sync--"
MARKER = re.compile(r"^<!-- agent-kit-sync rule file=(\S+) item=([a-z0-9-]+)/([a-z0-9-]+) sha256=([0-9a-f]{64}) -->\n", re.M)
HEADER = "---\ndescription: {description}\nalwaysApply: true\n---\n\n"
# An import line names a file: an absolute path, a home-relative path, or a Windows drive path.
IMPORT_LINE = re.compile(r"^@((?:/|~[/\\]|[A-Za-z]:[/\\])\S.*?)\s*$")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# File names over this many characters are shortened, so a deep folder stays under Windows' path limit.
NAME_LIMIT = 100


@dataclass
class Rule:
    name: str        # file name in the rules folder
    module: str
    item: str
    text: str        # the whole file, marker included


def file_name(module: str, item: str, part: int = 0) -> str:
    """agent-kit-sync--MODULE--ITEM.mdc; module and item names are lowercase letters, digits, and hyphens."""
    stem = f"{PREFIX}{module}--{item}" + (f"--part{part}" if part else "")
    if len(stem) + 4 > NAME_LIMIT:
        digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
        stem = stem[:60] + "--" + digest
    return stem + ".mdc"


def normalized(text: str) -> str:
    """The text as agent-kit-sync writes it: no byte order mark, LF line breaks."""
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def compose(module: str, item: str, body: str, part: int = 0, parts: int = 1) -> Rule:
    body = normalized(body).strip("\n") + "\n"
    label = f"{module}/{item}" + (f", part {part} of {parts}" if parts > 1 else "")
    header = HEADER.format(description=f"Instructions agent-kit-sync keeps the same on each of this user's machines ({label})")
    name = file_name(module, item, part)
    marker = f"<!-- agent-kit-sync rule file={name} item={module}/{item} sha256={_hash(header + body)} -->\n"
    return Rule(name, module, item, header + marker + body)


@dataclass
class Mark:
    name: str
    module: str
    item: str
    edited: bool


def read_mark(text: str) -> Optional[Mark]:
    """The marker of a file agent-kit-sync wrote, or None for a file it did not write."""
    text = normalized(text)
    match = MARKER.search(text)
    if not match:
        return None
    rest = text[:match.start()] + text[match.end():]
    return Mark(match.group(1), match.group(2), match.group(3), _hash(rest) != match.group(4))


def _same(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expanduser(path)))


class Fences:
    """Tracks whether a line is inside a Markdown code fence, the way CommonMark closes one:
    a closing fence uses the same character, is at least as long, and has nothing after it."""

    def __init__(self) -> None:
        self.open: Optional[str] = None   # the opening fence's marker, such as ```

    def step(self, line: str) -> bool:
        """Read one line; True when it is a fence line."""
        match = FENCE.match(line)
        if not match:
            return False
        mark = match.group(1)
        if self.open is None:
            self.open = mark
            return True
        if mark[0] == self.open[0] and len(mark) >= len(self.open) and not line.strip()[len(mark):].strip():
            self.open = None
            return True
        return False


def expand_imports(text: str, support: Dict[str, bytes]) -> Tuple[str, List[str], List[str]]:
    """Replace each @path line that names an installed support file with the file's text.
    Returns the text, the @path lines that name no installed file, and the files that are not
    UTF-8 text. Lines inside code fences stay as they are. The imported file's own @ lines
    are not followed."""
    files = {_same(path): data for path, data in support.items()}
    out, unmatched, unreadable = [], [], []
    fences = Fences()
    for line in text.splitlines(keepends=True):
        inside = fences.open is not None
        if fences.step(line) or inside:
            out.append(line)
            continue
        match = IMPORT_LINE.match(line.rstrip("\r\n"))
        if match:
            data = files.get(_same(match.group(1)))
            if data is None:
                unmatched.append(match.group(1))
            else:
                try:
                    expanded = normalized(data.decode("utf-8"))
                except UnicodeDecodeError:
                    unreadable.append(match.group(1))
                else:
                    out.append(expanded if expanded.endswith("\n") else expanded + "\n")
                    continue
        out.append(line)
    return "".join(out), unmatched, unreadable


def split(body: str, limit: int = BODY_LIMIT) -> List[str]:
    """Pieces of the body each under `limit` bytes. A cut falls at a blank line outside code
    fences when one is near. A cut inside a fence closes the fence at the end of one piece and
    opens it again, with its info string, at the start of the next."""
    if len(body.encode("utf-8")) <= limit:
        return [body]
    pieces: List[str] = []
    current: List[str] = []
    size = 0
    fences = Fences()
    opening = ""   # the line that opened the current fence

    def cut() -> None:
        nonlocal current, size
        inside = fences.open is not None
        pieces.append("".join(current) + (fences.open + "\n" if inside else ""))
        current = [opening] if inside else []
        size = len(opening.encode("utf-8")) if inside else 0

    for line in body.splitlines(keepends=True):
        if not line.endswith("\n"):
            line += "\n"
        line_size = len(line.encode("utf-8"))
        if fences.open is None and line.strip() == "" and current and size + line_size > limit * 0.9:
            cut()
            continue
        if current and size + line_size > limit - 256:
            cut()
        was_open = fences.open is not None
        fences.step(line)
        if not was_open and fences.open is not None:
            opening = line
        current.append(line)
        size += line_size
    if current:
        pieces.append("".join(current))
    return pieces


@dataclass
class Planned:
    rules: List[Rule] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)
    oversized: List[str] = field(default_factory=list)


def plan_rules(blocks: List[Tuple[str, str, str]], support: Dict[str, bytes]) -> Planned:
    """Rules for the (module, item, text) of each Cursor section."""
    planned = Planned()
    for module, item, text in blocks:
        body, missing, unreadable = expand_imports(text, support)
        planned.unmatched.extend(missing)
        planned.unreadable.extend(unreadable)
        pieces = split(body)
        for number, piece in enumerate(pieces, 1):
            rule = compose(module, item, piece, number if len(pieces) > 1 else 0, len(pieces))
            if len(rule.text.encode("utf-8")) > RULE_LIMIT:
                planned.oversized.append(rule.name)
            planned.rules.append(rule)
    return planned


@dataclass
class Outcome:
    written: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    backed_up: List[str] = field(default_factory=list)   # backups of rule files someone edited
    foreign: List[str] = field(default_factory=list)     # files at agent-kit-sync's names it did not write
    errors: List[str] = field(default_factory=list)


def write_rules(folder: Path, rules: List[Rule], keep: Set[Tuple[str, str]], backups: Path) -> Outcome:
    """Make agent-kit-sync's rule files in `folder` match `rules`.

    A file is agent-kit-sync's only when its marker names the file itself; every other file is
    left alone. `keep` holds (module, item) pairs whose files stay as they are, such as a module
    that failed this run. A file someone edited is backed up before it is replaced or removed.
    """
    outcome = Outcome()
    wanted = {rule.name: rule for rule in rules}
    try:
        present = sorted(name for name in os.listdir(folder) if name.startswith(PREFIX) and name.endswith(".mdc"))
    except OSError:
        present = []
    for name in present:
        if name in wanted:
            continue
        path = folder / name
        raw = _read(path, outcome)
        if raw is None:
            continue
        mark = read_mark(raw.decode("utf-8", "replace"))
        if mark is None or mark.name != name or (mark.module, mark.item) in keep:
            continue  # Not agent-kit-sync's file, or one it keeps this run.
        if mark.edited and not _backup(path, raw, backups, outcome):
            continue
        try:
            path.unlink()
            outcome.removed.append(str(path))
        except OSError as error:
            outcome.errors.append(f"{path}: {error}")
    for name, rule in wanted.items():
        path = folder / name
        raw = _read(path, outcome) if path.exists() else None
        if path.exists() and raw is None:
            continue
        if raw is not None:
            if raw == rule.text.encode("utf-8"):
                continue
            mark = read_mark(raw.decode("utf-8", "replace"))
            if mark is None or mark.name != name:
                outcome.foreign.append(str(path))
                continue
            if mark.edited and not _backup(path, raw, backups, outcome):
                continue
        try:
            atomic_write(path, rule.text.encode("utf-8"))
            outcome.written.append(str(path))
        except (OSError, ChangedUnderneath) as error:
            outcome.errors.append(f"{path}: {error}")
    if outcome.removed:
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()  # Only a folder agent-kit-sync just emptied.
        except OSError:
            pass
    return outcome


def _read(path: Path, outcome: Outcome) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError as error:
        outcome.errors.append(f"{path}: {error}")
        return None


def _backup(path: Path, raw: bytes, backups: Path, outcome: Outcome) -> bool:
    """Save an edited rule file's bytes under a name no other backup has."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    try:
        backups.mkdir(parents=True, exist_ok=True)
        for number in range(1000):
            target = backups / (f"{path.name}.{stamp}" + (f".{number}" if number else ""))
            try:
                with open(target, "xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                continue
            outcome.backed_up.append(str(target))
            return True
        raise OSError("too many backups with the same time")
    except OSError as error:
        outcome.errors.append(f"{path}: could not save a backup before replacing it: {error}")
        return False


def owners(folder: Path) -> Set[Tuple[str, str]]:
    """The (module, item) of each rule file agent-kit-sync wrote in `folder`, read from the markers."""
    found = set()
    try:
        names = [name for name in os.listdir(folder) if name.startswith(PREFIX) and name.endswith(".mdc")]
    except OSError:
        return found
    for name in names:
        try:
            mark = read_mark((folder / name).read_bytes().decode("utf-8", "replace"))
        except OSError:
            continue
        if mark is not None and mark.name == name:
            found.add((mark.module, mark.item))
    return found
