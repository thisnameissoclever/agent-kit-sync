"""Protect installed references left by earlier directory migrations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import toml

MAX_RECEIPT_BYTES = 4 * 1024 * 1024
COMPONENT_END = r"(?=$|[/\s`\"'<>()\[\]{},;:!?]|\.(?=$|\s))"


def normalized(value):
    return re.sub(r"[/\\]+", "/", str(value))


def quoted_strings(text):
    start, escaped = None, False
    for index, char in enumerate(text):
        if start is None:
            if char == '"':
                start = index
        elif escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            yield text[start:index + 1]
            start = None


def reference_views(text):
    """Inspect raw text and decoded JSON-compatible or TOML basic strings."""
    yield normalized(text)
    for literal in quoted_strings(text):
        if "\\" not in literal:
            continue
        try:
            decoded = json.loads(literal)
        except ValueError:
            if "\\U" not in literal:
                continue
            try:
                decoded = toml.loads("value = " + literal)["value"]
            except ValueError:
                continue
        if isinstance(decoded, str):
            yield normalized(decoded)


@dataclass(frozen=True)
class UnsafeReference:
    path: Path
    unverified: bool


class StatusReferences:
    def __init__(self, layout, projects, system):
        self.receipt = layout.state / "status-directory-migration.json"
        try:
            self.enabled = projects is not None and (self.receipt.is_file() or self.receipt.exists() or self.receipt.is_symlink())
        except OSError:
            self.enabled = projects is not None
        self.complete = False
        self.patterns = []
        if not self.enabled:
            return
        self.canonical = projects / "status"
        try:
            self.complete = self._complete_receipt() and self.canonical.is_dir()
        except OSError:
            self.complete = False
        flags = re.I if system == "windows" else 0
        profiles = [(projects, "status", True), (projects, "_status", False),
                    (projects, "(?i:" + re.escape(projects.name) + "_status)", False),
                    (projects.parent, re.escape(projects.name + "_status"), False)]
        for parent, component, destination in profiles:
            pattern = re.escape(normalized(parent).rstrip("/")) + "/(?P<name>" + component + ")" + COMPONENT_END
            self.patterns.append((re.compile(pattern, flags), parent, destination))

    def _complete_receipt(self):
        try:
            if not self.receipt.is_file():
                return False
            with self.receipt.open("rb") as handle:
                raw = handle.read(MAX_RECEIPT_BYTES + 1)
            if len(raw) > MAX_RECEIPT_BYTES:
                return False
            record = json.loads(raw.decode("utf-8"))
            if not isinstance(record, dict) or record.get("version") != 2 or record.get("state") != "complete":
                return False
            if record.get("staged", {}) != {} or not isinstance(record.get("retired", []), list):
                return False
            if any(not isinstance(entry, dict) or entry.get("phase") != "retained" for entry in record.get("retired", [])):
                return False
            canonical = record.get("canonical")
            if canonical is not None and (not isinstance(canonical, str) or Path(canonical) != self.canonical):
                return False
            if "posix_move" in record:
                move = record["posix_move"]
                if not isinstance(move, dict) or move.get("phase") != "complete":
                    return False
                canonical = move.get("canonical")
                if move.get("data_location") is not None and Path(move["data_location"]) != self.canonical:
                    return False
            return isinstance(canonical, str) and Path(canonical) == self.canonical
        except (OSError, ValueError, TypeError, RecursionError):
            return False

    def find_unsafe(self, text):
        for view in reference_views(text):
            for pattern, parent, destination in self.patterns:
                for match in pattern.finditer(view):
                    path = parent / match.group("name")
                    if destination:
                        if not self.complete:
                            return UnsafeReference(path, True)
                    else:
                        try:
                            if not path.exists():
                                return UnsafeReference(path, False)
                        except OSError:
                            return UnsafeReference(path, True)
        return None
