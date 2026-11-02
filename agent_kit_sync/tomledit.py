"""Small text edits to module.toml files that keep comments and layout.

Each [[kind]] entry runs from its header line to the line before the next header.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from . import toml

HEADER = re.compile(r"^\s*\[\[?\s*[A-Za-z0-9_\"'.-][^\]]*\]\]?\s*(?:#.*)?$")


def _entries(text: str, array: str) -> List[Tuple[int, int, dict]]:
    lines = text.splitlines(keepends=True)
    found = []
    number = 0
    while number < len(lines):
        if re.match(r"^\s*\[\[\s*" + re.escape(array) + r"\s*\]\]\s*(?:#.*)?$", lines[number]):
            stop = number + 1
            while stop < len(lines) and not HEADER.match(lines[stop]):
                stop += 1
            body = "".join(lines[number + 1:stop])
            try:
                data = toml.loads(body)
            except toml.TOMLDecodeError:
                data = {}
            found.append((number, stop, data))
            number = stop
            continue
        number += 1
    return found


def _match(data: dict, array: str, name: str) -> bool:
    if data.get("name") == name:
        return True
    if array == "settings" and "name" not in data:
        derived = re.sub(r"[^a-z0-9]+", "-", f"{data.get('app', '')}-{data.get('key', '')}".lower()).strip("-")
        return derived == name
    if array == "files" and "name" not in data:
        return re.sub(r"[^a-z0-9]+", "-", str(data.get("path", "")).lower()).strip("-") == name
    return False


def entry_text(text: str, array: str, name: str) -> Optional[str]:
    lines = text.splitlines(keepends=True)
    for start, stop, data in _entries(text, array):
        if _match(data, array, name):
            return "".join(lines[start:stop])
    return None


def remove_entry(text: str, array: str, name: str) -> str:
    lines = text.splitlines(keepends=True)
    for start, stop, data in _entries(text, array):
        if _match(data, array, name):
            # Also drop one blank line before the entry, if there is one.
            if start > 0 and lines[start - 1].strip() == "":
                start -= 1
            return "".join(lines[:start] + lines[stop:])
    return text


def append_entry_text(text: str, raw: str) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text.strip() else "") + (raw if raw.endswith("\n") else raw + "\n")


def append_entry(text: str, array: str, fields: dict) -> str:
    body = "".join(f"{toml.key(k)} = {toml.value(v)}\n" for k, v in fields.items())
    return append_entry_text(text, f"[[{array}]]\n" + body)


def set_field(text: str, array: str, name: str, field: str, value) -> str:
    lines = text.splitlines(keepends=True)
    for start, stop, data in _entries(text, array):
        if not _match(data, array, name):
            continue
        rendered = f"{toml.key(field)} = {toml.value(value)}\n"
        pattern = re.compile(r"^\s*" + re.escape(field) + r"\s*=")
        for number in range(start + 1, stop):
            if pattern.match(lines[number]):
                lines[number] = rendered
                break
        else:
            insert = stop
            while insert > start + 1 and lines[insert - 1].strip() == "":
                insert -= 1
            lines.insert(insert, rendered)
        result = "".join(lines)
        toml.loads(result)
        return result
    raise KeyError(f"no [[{array}]] entry named {name}")
