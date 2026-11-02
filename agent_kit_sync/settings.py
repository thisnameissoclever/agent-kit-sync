"""Reading and changing single keys in agent apps' settings files.

Claude Code keeps settings in JSON and Codex in TOML. Both files are also written by the
apps themselves and by other software, so every change here touches one key, keeps the
rest of the file, and is verified before the file is replaced.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

from . import toml

MISSING = object()
KeyPath = Union[str, Sequence[str]]


class SettingsError(Exception):
    """A settings file the tool will not change, such as one it cannot parse."""


def split_key(key: KeyPath) -> List[str]:
    parts = list(key) if isinstance(key, (list, tuple)) else str(key).split(".")
    if not parts or any(not isinstance(part, str) or part == "" for part in parts):
        raise SettingsError(f"setting key {key!r} is empty or has an empty part")
    return parts


def get_path(data: Any, parts: Sequence[str]) -> Any:
    current = data
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return MISSING
        current = current[part]
    return current


def set_path(data: dict, parts: Sequence[str], value: Any) -> dict:
    result = copy.deepcopy(data)
    current = result
    for part in parts[:-1]:
        nxt = current.get(part)
        if nxt is None:
            nxt = current[part] = {}
        if not isinstance(nxt, dict):
            raise SettingsError(f"setting {'.'.join(parts)}: {part} is not a table")
        current = nxt
    if value is MISSING:
        current.pop(parts[-1], None)
    else:
        current[parts[-1]] = value
    return result


def desired_value(current: Any, value: Any, mode: str) -> Any:
    """The value a setting should hold. In contains mode, add missing list entries."""
    if mode == "contains":
        if not isinstance(value, list):
            raise SettingsError("a contains setting needs a list value")
        base = list(current) if isinstance(current, list) else []
        return base + [entry for entry in value if entry not in base]
    return value


def satisfied(current: Any, value: Any, mode: str) -> bool:
    if mode == "contains":
        return isinstance(current, list) and all(entry in current for entry in value)
    return current == value


# ---------------------------------------------------------------- JSON (Claude Code)


def json_indent(text: str) -> Union[int, str]:
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            prefix = line[: len(line) - len(stripped)]
            return prefix if "\t" in prefix else len(prefix)
    return 2


def json_load(text: str) -> dict:
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise SettingsError(f"not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise SettingsError("the top level is not an object")
    return data


def json_dump(data: dict, original: str) -> str:
    out = json.dumps(data, indent=json_indent(original), ensure_ascii=False)
    return out + ("\n" if original.endswith("\n") or not original else "")


def json_set(text: str, key: KeyPath, value: Any) -> str:
    data = json_load(text)
    return json_dump(set_path(data, split_key(key), value), text)


# ---------------------------------------------------------------- TOML (Codex)

HEADER = re.compile(r"^\s*\[\[?\s*((?:[A-Za-z0-9_-]+|\"(?:[^\"\\\\]|\\\\.)*\"|'[^']*')(?:\s*\.\s*(?:[A-Za-z0-9_-]+|\"(?:[^\"\\\\]|\\\\.)*\"|'[^']*'))*)\s*\]\]?\s*(?:#.*)?$")
ASSIGN = re.compile(r"^\s*((?:[A-Za-z0-9_-]+|\"(?:[^\"\\]|\\.)*\"|'[^']*')(?:\s*\.\s*(?:[A-Za-z0-9_-]+|\"(?:[^\"\\]|\\.)*\"|'[^']*'))*)\s*=")
TOML_SCALARS = (str, int, float, bool)


def _header_parts(raw: str) -> List[str]:
    return _key_parts(raw)


def _key_parts(raw: str) -> List[str]:
    try:
        return _key_parts_strict(raw)
    except ValueError:
        return [raw.strip()]


def _key_parts_strict(raw: str) -> List[str]:
    parts, buf, quote, number = [], "", None, 0
    while number < len(raw):
        char = raw[number]
        if quote:
            if char == "\\" and quote == '"' and number + 1 < len(raw):
                buf += raw[number:number + 2]
                number += 2
                continue
            buf += char
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            buf += char
        elif char == ".":
            parts.append(buf.strip())
            buf = ""
        else:
            buf += char
        number += 1
    parts.append(buf.strip())
    clean = []
    for part in parts:
        if part.startswith('"'):
            clean.append(json.loads(part))
        elif part.startswith("'"):
            clean.append(part[1:-1])
        else:
            clean.append(part)
    return clean


def _check_toml_value(value: Any) -> None:
    if isinstance(value, TOML_SCALARS):
        return
    if isinstance(value, list) and all(isinstance(entry, TOML_SCALARS) for entry in value):
        return
    raise SettingsError("Codex settings may hold strings, numbers, booleans, and lists of those")


def toml_load(text: str) -> dict:
    try:
        return toml.loads(text)
    except toml.TOMLDecodeError as error:
        raise SettingsError(f"not valid TOML: {error}") from None


def toml_set(text: str, key: KeyPath, value: Any) -> str:
    """Change or insert one key, leaving comments and every other line as they are."""
    parts = split_key(key)
    if value is not MISSING:
        _check_toml_value(value)
    original = toml_load(text)
    expected = set_path(original, parts, value)
    table, name = parts[:-1], parts[-1]
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    # Find the table's lines: from its header (or the top of the file) to the next header.
    start, stop = (0, len(lines))
    if table:
        start = None
        for number, line in enumerate(lines):
            match = HEADER.match(line)
            if match and not line.lstrip().startswith("[[") and _header_parts(match.group(1)) == table:
                start = number + 1
                break
    if start is not None:
        for number in range(start, len(lines)):
            if HEADER.match(lines[number]):
                stop = number
                break
    rendered = None if value is MISSING else f"{toml.key(name)} = {toml.value(value)}\n"
    if start is None:
        if rendered is None:
            return text
        prefix = "" if not lines or lines[-1].strip() == "" else "\n"
        result = "".join(lines) + prefix + "[" + ".".join(toml.key(part) for part in table) + "]\n" + rendered
    else:
        found = None
        last_assignment = start - 1
        number = start
        while number < stop:
            match = ASSIGN.match(lines[number])
            if not match:
                number += 1
                continue
            end = _value_end(lines, number, match.end(), stop)
            if _key_parts(match.group(1)) == [name]:
                found = number
            last_assignment = end
            number = end + 1
        if found is not None:
            after = lines[found][ASSIGN.match(lines[found]).end():].strip()
            if after.startswith(("[", "{", '"""', "'''")) and not _single_line_value(after):
                raise SettingsError(f"{'.'.join(parts)} spans several lines; change it by hand")
            if rendered is None:
                del lines[found]
            else:
                lines[found] = rendered
            result = "".join(lines)
        elif rendered is None:
            return text
        else:
            insert_at = last_assignment + 1 if last_assignment >= start else start
            lines.insert(insert_at, rendered)
            result = "".join(lines)
    if toml_load(result) != expected:
        raise SettingsError(f"changing {'.'.join(parts)} would change other settings too")
    return result


def _value_end(lines: List[str], number: int, offset: int, stop: int) -> int:
    """The last line of the value that starts on line `number`."""
    text = lines[number][offset:]
    end = number
    while end + 1 < stop:
        try:
            toml.loads("x = " + text)
            return end
        except toml.TOMLDecodeError:
            end += 1
            text += lines[end]
    return end


def _single_line_value(after: str) -> bool:
    try:
        toml.loads("x = " + after)
        return True
    except toml.TOMLDecodeError:
        return False


# ---------------------------------------------------------------- files


def file_format(path: Path) -> str:
    return "toml" if path.suffix == ".toml" else "json"


def read_value(path: Path, key: KeyPath) -> Any:
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return MISSING
    data = toml_load(text) if file_format(path) == "toml" else json_load(text)
    return get_path(data, split_key(key))


def change(text: str, fmt: str, key: KeyPath, value: Any) -> str:
    if fmt == "toml":
        return toml_set(text, key, value)
    if value is MISSING:
        data = json_load(text)
        return json_dump(set_path(data, split_key(key), MISSING), text)
    return json_set(text, key, value)
