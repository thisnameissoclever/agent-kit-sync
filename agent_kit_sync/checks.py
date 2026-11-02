"""Checks that a file must pass before it is committed to the config repo or installed."""

from __future__ import annotations

import getpass
import re
import socket
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Sequence

from . import render

SECRET_PATTERNS = [
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("API key", re.compile(r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}|AIza[0-9A-Za-z_-]{35})\b")),
]
USER_ROOTS = re.compile(r"(?<![\w.~-])(?:/Users/[^/\s\"'`]+|/home/[^/\s\"'`]+|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'`]+)(?=[/\\\s\"'`),;:]|$)")
DRIVE = re.compile(r"(?<![\w%$])[A-Za-z]:[\\/](?=[\w.\-])")
FIXED_INTERPRETERS = re.compile(r"(?<![\w.-])(?:/opt/homebrew/bin/|/usr/local/bin/|/opt/local/bin/)(?:python3?(?:\.\d+)?|node|bash|zsh)\b")
TEXT_SUFFIXES = (".md", ".txt", ".toml", ".json", ".yaml", ".yml", ".py", ".sh", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".cfg", ".ini", "")


@dataclass
class Finding:
    check: str
    path: str
    line: int
    detail: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.check}: {where}: {self.detail}"


def read_terms(path: Path) -> List[str]:
    if not path.is_file():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def machine_values() -> List[str]:
    values = []
    home = str(Path.home())
    if len(home) > 3:
        values.append(home)
    try:
        user = getpass.getuser()
        if len(user) >= 3:
            values.append(user)
    except Exception:
        pass
    try:
        host = socket.gethostname().split(".")[0]
        if len(host) >= 3 and host.lower() not in ("localhost",):
            values.append(host)
    except Exception:
        pass
    return values


def decode_any(data: bytes) -> str:
    """Decode text in UTF-8, UTF-16, or Latin-1, so no encoding skips the checks."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    sample = data[:4096]
    if sample and sample.count(b"\0") * 3 > len(sample):
        for codec in ("utf-16-le", "utf-16-be"):
            try:
                return data.decode(codec)
            except UnicodeDecodeError:
                continue
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _word(term: str) -> re.Pattern:
    escaped = re.escape(term)
    left = r"(?<![A-Za-z0-9])" if term[:1].isalnum() else ""
    right = r"(?![A-Za-z0-9])" if term[-1:].isalnum() else ""
    return re.compile(left + escaped + right, re.IGNORECASE)


class Checker:
    def __init__(self, blocklist: Sequence[str] = (), own_values: Optional[Sequence[str]] = None):
        self.terms = [(term, _word(term)) for term in blocklist]
        values = machine_values() if own_values is None else list(own_values)
        self.values = [(value, _word(value)) for value in values]

    def text_findings(self, path: str, text: str, *, own: bool, lines: Optional[Iterable[int]] = None) -> List[Finding]:
        findings: List[Finding] = []
        wanted = set(lines) if lines is not None else None
        for number, line in enumerate(text.splitlines(), start=1):
            if wanted is not None and number not in wanted:
                continue
            for name, pattern in SECRET_PATTERNS:
                if own and pattern.search(line):
                    findings.append(Finding("secrets", path, number, f"text shaped like a {name}"))
            if not own:
                continue
            for term, pattern in self.terms:
                if pattern.search(line):
                    findings.append(Finding("blocklist", path, number, f"contains the blocked term {term!r}"))
            if line.lstrip().startswith("#!"):
                continue
            for value, pattern in self.values:
                if pattern.search(line):
                    findings.append(Finding("machine-specific", path, number, f"contains this machine's value {value!r}; use a placeholder or a relative path"))
            match = USER_ROOTS.search(line)
            if match:
                findings.append(Finding("machine-specific", path, number, f"contains the user folder path {match.group(0)!r}"))
            elif DRIVE.search(line):
                findings.append(Finding("machine-specific", path, number, "contains a drive letter path"))
            match = FIXED_INTERPRETERS.search(line)
            if match:
                findings.append(Finding("machine-specific", path, number, f"calls the fixed interpreter path {match.group(0)!r}; call it by name"))
        return findings

    def placeholder_findings(self, path: str, text: str) -> List[Finding]:
        findings = []
        for number, line in enumerate(text.splitlines(), start=1):
            for match in render.PLACEHOLDER.finditer(line):
                name, arg = match.group(1), match.group(2)
                if name in ("home", "projects", "worktrees", "module", "app", "os", "cli") and arg is None:
                    continue
                if name in ("hook", "literal") and arg:
                    continue
                findings.append(Finding("format", path, number, f"unknown placeholder {match.group(0)}"))
        return findings

    def python_findings(self, path: str, data: bytes) -> List[Finding]:
        try:
            compile(data, path, "exec", dont_inherit=True)
        except SyntaxError as error:
            return [Finding("python-syntax", path, error.lineno or 0, error.msg)]
        except ValueError as error:
            return [Finding("python-syntax", path, 0, str(error))]
        return []

    def file(self, path: str, data: bytes, *, own: bool, templated: bool = False, added_lines: Optional[Iterable[int]] = None) -> List[Finding]:
        """Check one file. `own` is True for the user's files and False for files from a source."""
        findings: List[Finding] = []
        if PurePosixPath(path).suffix == ".py":
            findings += self.python_findings(path, data)
        text = decode_any(data)
        if "\0" in text:
            # Binary: look for blocked terms and secrets in the printable runs only.
            text = "\n".join(re.findall(r"[\x20-\x7e]{6,}", text))
            added_lines = None
        findings += self.text_findings(path, text, own=own, lines=added_lines)
        if templated:
            findings += self.placeholder_findings(path, text)
        return findings


def added_line_numbers(old: Optional[bytes], new: bytes) -> Optional[List[int]]:
    """Line numbers in `new` that are not in `old`, or None to check every line."""
    if old is None or b"\0" in old[:4096] or b"\0" in new[:4096]:
        return None
    import difflib
    old_lines = decode_any(old).splitlines()
    new_lines = decode_any(new).splitlines()
    numbers = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            numbers.extend(range(j1 + 1, j2 + 1))
    return numbers
