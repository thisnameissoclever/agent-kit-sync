"""Protect installed references left by earlier directory migrations."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import toml

MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_SOURCE_PATHS = 32
COMPONENT_END = r"(?=$|[/\s`\"'<>()\[\]{},;:!?]|\.(?=$|\s))"


def normalized(value):
    return re.sub(r"[/\\]+", "/", str(value))


def reference_views(text):
    """Inspect raw text and string values in complete JSON or TOML documents."""
    yield normalized(text)
    try:
        decoded = json.loads(text)
    except (ValueError, RecursionError):
        try:
            decoded = toml.loads(text)
        except (ValueError, RecursionError):
            return
    pending = [decoded]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            yield normalized(value)
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


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
        self.record = {}
        self.sources_valid = True
        self.patterns = []
        if not self.enabled:
            return
        self.canonical = projects / "status"
        try:
            self.complete = self._complete_receipt() and self.canonical.is_dir()
        except OSError:
            self.complete = False
        recorded_sources = self._recorded_sources()
        self.complete = self.complete and self.sources_valid
        flags = re.I if system == "windows" else 0
        profiles = [(projects, "status", True), (projects, "_status", False),
                    (projects, "(?i:" + re.escape(projects.name) + "_status)", False),
                    (projects.parent, re.escape(projects.name + "_status"), False)]
        spellings = recorded_sources | {Path(os.path.normpath(str(path))) for path in recorded_sources}
        profiles += [(path.parent, re.escape(path.name), False) for path in sorted(spellings)]
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
            if isinstance(record, dict):
                self.record = record
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

    def _recorded_sources(self):
        paths = set()
        def add(value):
            if value is None:
                return
            if not isinstance(value, str) or not Path(value).is_absolute():
                self.sources_valid = False
                return
            paths.add(Path(value))
            if len(paths) > MAX_SOURCE_PATHS:
                raise RuntimeError(f"Migration receipt {self.receipt} exceeds {MAX_SOURCE_PATHS} distinct source paths; inspect it before syncing.")
        explicit = self.record.get("explicit_sources", [])
        if isinstance(explicit, list):
            for value in explicit:
                add(value)
        else:
            self.sources_valid = False
        staged = self.record.get("staged", {})
        if isinstance(staged, dict):
            for value in staged:
                add(value)
        else:
            self.sources_valid = False
        retired = self.record.get("retired", [])
        if isinstance(retired, list):
            for entry in retired:
                if isinstance(entry, dict):
                    add(entry.get("source"))
                else:
                    self.sources_valid = False
        else:
            self.sources_valid = False
        move = self.record.get("posix_move")
        if isinstance(move, dict):
            add(move.get("source"))
        return paths

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
