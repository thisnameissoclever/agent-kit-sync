"""TOML reading through the standard library when available, else the bundled tomli, and a small writer."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

try:
    import tomllib as _toml
except ImportError:  # Python before 3.11
    from ._vendor import tomli as _toml

TOMLDecodeError = _toml.TOMLDecodeError
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def loads(text: str) -> dict:
    return _toml.loads(text)


def load_file(path) -> dict:
    return loads(Path(path).read_bytes().decode("utf-8"))


def key(name: str) -> str:
    return name if BARE_KEY.match(name) else string(name)


def string(value: str) -> str:
    # JSON string escapes are a subset of TOML basic-string escapes, except that
    # TOML has no \/ escape, which json.dumps never emits.
    return json.dumps(value, ensure_ascii=False)


def value(item) -> str:
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, (int, float)):
        return repr(item)
    if isinstance(item, str):
        return string(item)
    if isinstance(item, (datetime.date, datetime.datetime)):
        return item.isoformat()
    if isinstance(item, (list, tuple)):
        return "[" + ", ".join(value(element) for element in item) + "]"
    if isinstance(item, dict):
        return "{ " + ", ".join(f"{key(k)} = {value(v)}" for k, v in item.items()) + " }"
    raise TypeError(f"cannot write {type(item).__name__} as TOML")


def dumps(data: dict) -> str:
    """Write a dict whose nested dicts become tables; dicts inside lists become inline tables."""
    lines = []
    _table(data, [], lines)
    return "\n".join(lines).strip("\n") + "\n"


def _table(data: dict, path: list, lines: list) -> None:
    scalars = [(k, v) for k, v in data.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in data.items() if isinstance(v, dict)]
    if path and (scalars or not tables):
        lines.append("")
        lines.append("[" + ".".join(key(part) for part in path) + "]")
    for k, v in scalars:
        lines.append(f"{key(k)} = {value(v)}")
    for k, v in tables:
        _table(v, path + [k], lines)
