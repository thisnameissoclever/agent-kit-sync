"""Marked blocks inside instruction files such as CLAUDE.md and AGENTS.md.

A block is a begin marker line, the section's text, and an end marker line. The tool
reads and writes only text between its own markers and leaves every other byte as it was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

BEGIN = "<!-- agent-kit-sync begin {key} -->"
END = "<!-- agent-kit-sync end {key} -->"
MARKER = re.compile(r"^<!-- agent-kit-sync (begin|end) ([a-z0-9-]+/[a-z0-9-]+) -->\r?$")
LOCAL_BEGIN = "<!-- agent-kit-sync local {kind} -->"
LOCAL_END = "<!-- agent-kit-sync local end -->"
LOCAL_MARKER = re.compile(r"^<!-- agent-kit-sync local (prepend|append|end) -->\r?$")


class MarkerError(Exception):
    """Markers in an instruction file that do not pair up."""


@dataclass
class Block:
    key: str
    begin: int      # index of the begin marker line
    end: int        # index of the end marker line
    content: str    # text between the markers, with line endings


def parse(text: str) -> Tuple[List[str], List[Block]]:
    lines = text.splitlines(keepends=True)
    blocks: List[Block] = []
    open_key: Optional[str] = None
    open_at = -1
    seen = set()
    for number, line in enumerate(lines):
        match = MARKER.match(line.rstrip("\n"))
        if not match:
            continue
        kind, key = match.groups()
        if kind == "begin":
            if open_key is not None:
                raise MarkerError(f"block {open_key} opens at line {open_at + 1} and is not closed before {key} begins")
            if key in seen:
                raise MarkerError(f"block {key} appears twice")
            open_key, open_at = key, number
        else:
            if open_key != key:
                raise MarkerError(f"line {number + 1} closes block {key}, which is not open")
            blocks.append(Block(key, open_at, number, "".join(lines[open_at + 1:number])))
            seen.add(key)
            open_key = None
    if open_key is not None:
        raise MarkerError(f"block {open_key} opens at line {open_at + 1} and is never closed")
    return lines, blocks


def extract(text: str) -> Dict[str, str]:
    _, blocks = parse(text)
    return {block.key: block.content for block in blocks}


def _ensure_newline(content: str) -> str:
    return content if content.endswith("\n") or content == "" else content + "\n"


def apply(text: str, desired: Dict[str, str], managed: List[str], unwrap: Optional[List[str]] = None) -> str:
    """Return the file with each desired block written and each managed, undesired block removed.

    `desired` maps keys to content in the order new blocks are added. `managed` lists every
    key this tool may remove; blocks with other keys are left alone. Blocks in `unwrap` lose
    their markers but keep their text, for text that was the user's before it was synced.
    """
    unwrap = unwrap or []
    lines, blocks = parse(text)
    present = {block.key: block for block in blocks}
    out: List[str] = []
    cursor = 0
    for block in blocks:
        out.extend(lines[cursor:block.begin])
        cursor = block.end + 1
        if block.key in desired:
            out.append(lines[block.begin])
            out.append(_ensure_newline(desired[block.key]))
            out.append(lines[block.end])
        elif block.key in unwrap:
            out.extend(lines[block.begin + 1:block.end])
        elif block.key not in managed:
            out.extend(lines[block.begin:block.end + 1])
        else:
            # Removing a block also removes the blank line that separated it, so
            # adding and then removing a block gives back the original file.
            before_blank = out and out[-1].strip() == ""
            after_blank = cursor < len(lines) and lines[cursor].strip() == ""
            if before_blank and (after_blank or cursor >= len(lines)):
                out.pop()
    out.extend(lines[cursor:])
    result = "".join(out)
    additions = [key for key in desired if key not in present]
    if additions:
        if result and not result.endswith("\n"):
            result += "\n"
        if result.strip():
            result += "\n"
        for number, key in enumerate(additions):
            if number:
                result += "\n"
            result += BEGIN.format(key=key) + "\n" + _ensure_newline(desired[key]) + END.format(key=key) + "\n"
    return result


def adopt(text: str, start: int, stop: int, key: str) -> str:
    """Wrap lines start..stop-1 of the file in markers for `key`, in place."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    wrapped = [BEGIN.format(key=key) + "\n"] + lines[start:stop] + [END.format(key=key) + "\n"]
    return "".join(lines[:start] + wrapped + lines[stop:])


# ---------------------------------------------------------------- local additions inside a block


def with_local(base: str, prepend: Optional[str], append: Optional[str]) -> str:
    parts = []
    if prepend is not None:
        parts.append(LOCAL_BEGIN.format(kind="prepend") + "\n" + _ensure_newline(prepend) + LOCAL_END + "\n")
    parts.append(_ensure_newline(base) if base else "")
    if append is not None:
        parts.append(LOCAL_BEGIN.format(kind="append") + "\n" + _ensure_newline(append) + LOCAL_END + "\n")
    return "".join(parts)


def split_local(content: str) -> Tuple[Optional[str], str, Optional[str]]:
    """Split block content into local prepend text, the synced text, and local append text."""
    lines = content.splitlines(keepends=True)
    prepend: Optional[str] = None
    append: Optional[str] = None
    base: List[str] = []
    number = 0
    while number < len(lines):
        match = LOCAL_MARKER.match(lines[number].rstrip("\n"))
        if match and match.group(1) in ("prepend", "append"):
            kind = match.group(1)
            stop = number + 1
            while stop < len(lines) and not (LOCAL_MARKER.match(lines[stop].rstrip("\n")) and LOCAL_MARKER.match(lines[stop].rstrip("\n")).group(1) == "end"):
                stop += 1
            if stop >= len(lines):
                raise MarkerError(f"local {kind} text is never closed")
            text = "".join(lines[number + 1:stop])
            if kind == "prepend":
                prepend = text
            else:
                append = text
            number = stop + 1
            continue
        base.append(lines[number])
        number += 1
    return prepend, "".join(base), append


def _split_header(text: str) -> Tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        for number in range(1, len(lines)):
            if lines[number].rstrip("\r\n") == "---":
                return "".join(lines[: number + 1]), "".join(lines[number + 1:])
    return "", text


def wrap_local(text: str, prepend: Optional[str], append: Optional[str]) -> str:
    """Add local text to a file, after any --- header block at its top."""
    header, body = _split_header(text)
    return header + with_local(body, prepend, append)


def unwrap_local(text: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Return the file without its local text, and the local prepend and append text."""
    header, body = _split_header(text)
    prepend, base, append = split_local(body)
    return header + base, prepend, append
