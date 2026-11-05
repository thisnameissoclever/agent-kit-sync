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
HOOK_COMMAND = re.compile(r"\s-I\s+-S\s+[\"']?\S*launcher\.py[\"']?\s+run-hook\s")
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


SYSTEMS =("macos", "windows", "linux")
SYSTEM_LABELS = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}
CODE_SUFFIXES = (".py", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl")
SHELL_SUFFIXES = (".sh", ".bash", ".zsh")
WINDOWS_SUFFIXES = (".ps1", ".bat", ".cmd")
PORTABLE_MARK = re.compile(r"agent-kit-sync:\s*portable because\s+(\S.*?)\s*(?:\*/|-->)?\s*$")
# Code that checks which system it runs on, or that falls back when a module is missing. A comment does not count.
PLATFORM_TEST = re.compile(r"^(?!\s*(?:#|//)).*(?:\bsys\.platform\b|\bplatform\.system\(|\bos\.name\b|\bos_name\(|\bprocess\.platform\b|\bos\.platform\(|\bcommand -v\b|\$OSTYPE\b|\$\(uname\b|\buname -s\b|\bexcept\s+\(?\s*(?:ImportError|ModuleNotFoundError)\b)", re.M)
NODE_PROGRAMS = "npx|npm|pnpm|pnpx|yarn|bunx|tsc|jest|eslint|prettier"
SPAWN_PROGRAM = re.compile(r"\b(?:spawn|spawnSync|execFile|execFileSync)\(\s*['\"`](" + NODE_PROGRAMS + r")['\"`]")
SHELL_OPTION = re.compile(r"\bshell\s*[:=]\s*(?:true|True)\b")
NOT_WINDOWS = ("windows",)
NOT_UNIX = ("macos", "linux")
# Each entry: (pattern, systems it fails on, guarded, problem). A guarded pattern is not reported in a file that
# checks which system it runs on, because such code usually runs only on the system it was written for.
PORTABILITY_PATTERNS = [
    (re.compile(r"(?:\b(?:spawn|spawnSync|execFile|execFileSync)\(\s*|\b(?:command|cmd|program|bin|executable)\s*[:=]\s*|\b(?:run|call|check_call|check_output|Popen)\(\s*\[\s*)['\"`](npx|npm|pnpm|pnpx|yarn|bunx|tsc|jest|eslint|prettier)['\"`]"), NOT_WINDOWS, False,
     "starts {0} without a shell; on Windows {0} is a .cmd file, which Node's spawn refuses and Python finds only by its full name. Run the package's JavaScript file with the Node interpreter instead"),
    (re.compile(r"node_modules['\"]?\s*[,/\\+]\s*['\"]?\.bin\b|node_modules(?:/|\\{1,2})\.bin\b"), NOT_WINDOWS, False,
     "runs a program from node_modules/.bin; on Windows those are .cmd files, which spawn cannot start. Run the package's JavaScript file with process.execPath instead"),
    (re.compile(r"['\"`]#!\s*/[\w/.+-]+(?:\s+[\w.+-]+)?(?:\\n|$)"), NOT_WINDOWS, False,
     "writes a script that starts with #! to run as a program; Windows cannot run a file by its #! line. Write the fake program in Python or JavaScript and start it with the interpreter"),
    (re.compile(r":/usr/(?:local/)?s?bin\b|\bPATH\b.*(?:['\"]:['\"]|\}:\$?\{|['\"]:\$|:\$PATH)"), NOT_WINDOWS, False,
     "joins PATH entries with ':'; Windows separates them with ';'. Use os.pathsep in Python or path.delimiter in Node"),
    (re.compile(r"\bshell\s*=\s*True\b|\bshell:\s*true\b|\bos\.system\(|\bos\.popen\(|(?<![\w.])execSync\(|(?<![\w.])exec\(\s*['\"`]|\bchild_process\.exec(?:Sync)?\("), NOT_WINDOWS, False,
     "runs a command line through the shell, which on Windows is cmd.exe and reads quotes, pipes, and variables differently from sh. Pass a list of arguments to subprocess.run, or to spawn, without a shell"),
    (re.compile(r"^\s*(?:import|from)\s+(fcntl|pwd|grp|termios|tty|pty|resource|posix|crypt|syslog)\b"), NOT_WINDOWS, True,
     "imports {0}, which Windows does not have"),
    (re.compile(r"\b(os\.(?:fork|forkpty|getuid|geteuid|getgid|getegid|killpg|setsid|getpgid|setpgid|mkfifo|uname)|signal\.(?:alarm|SIGALRM|SIGKILL|SIGHUP|SIGUSR1|SIGUSR2|SIGQUIT)|process\.(?:getuid|geteuid|getgid))\b"), NOT_WINDOWS, True,
     "uses {0}, which Windows does not have"),
    (re.compile(r"['\"](/(?:usr/(?:local/)?)?s?bin/[\w.+-]+)"), NOT_WINDOWS, True,
     "calls {0}, which does not exist on Windows. Call the program by name, or do the work in Python or JavaScript"),
    (re.compile(r"\b(osascript|launchctl|pbcopy|pbpaste|xcrun|xcode-select|mdfind)\b"), ("windows", "linux"), True,
     "runs {0}, which only macOS has"),
    (re.compile(r"\b(xdg-open|notify-send|systemctl)\b"), ("macos", "windows"), True,
     "runs {0}, which only Linux has"),
    (re.compile(r"^\s*(?:import|from)\s+(winreg|msvcrt|winsound)\b|['\"](powershell(?:\.exe)?|pwsh(?:\.exe)?|cmd\.exe)['\"]|['\"](cmd)['\"]\s*,\s*['\"]/[cCkK]['\"]"), NOT_UNIX, True,
     "uses {0}, which only Windows has"),
]


def is_code(path: str, data: bytes) -> bool:
    """A file that runs: a script by its suffix, or any file whose first line starts with #!."""
    return PurePosixPath(path).suffix.lower() in CODE_SUFFIXES or data.startswith(b"#!")


def system_list(systems: Iterable[str]) -> str:
    labels = [SYSTEM_LABELS.get(system, system) for system in SYSTEMS if system in systems]
    return " and ".join(labels) if len(labels) < 3 else "macOS, Windows, and Linux"


def portability_findings(path: str, text: str, systems: Sequence[str] = SYSTEMS, lines: Optional[Iterable[int]] = None, marks: Optional[list] = None) -> List[Finding]:
    """Known ways a script works on one operating system and fails on another.

    Only problems on the systems in `systems`, the ones the module installs on, are reported. A line
    that holds `agent-kit-sync: portable because REASON` is not reported; it is added to `marks`.
    """
    findings: List[Finding] = []
    wanted = set(lines) if lines is not None else None
    suffix = PurePosixPath(path).suffix.lower()
    if wanted is None:
        whole = []
        if suffix in SHELL_SUFFIXES and "windows" in systems:
            whole.append("fails on Windows: a shell script, and Windows has no sh unless Git Bash or WSL is installed. Write it in Python, which every agent-kit-sync machine has")
        if suffix in WINDOWS_SUFFIXES and set(systems) & set(NOT_UNIX):
            whole.append(f"fails on {system_list(set(systems) & set(NOT_UNIX))}: a Windows script. Write it in Python, which every agent-kit-sync machine has")
        # A mark in the file's first lines accepts a problem with the whole file.
        mark = next((m for m in (PORTABLE_MARK.search(line) for line in text.splitlines()[:10]) if m), None) if whole else None
        for detail in whole:
            if mark:
                if marks is not None:
                    marks.append(Finding("portability", path, 0, f"marked portable: {mark.group(1)}"))
            else:
                findings.append(Finding("portability", path, 0, detail))
    guarded = bool(PLATFORM_TEST.search(text))
    for number, line in enumerate(text.splitlines(), start=1):
        if wanted is not None and number not in wanted:
            continue
        if number == 1 and line.startswith("#!"):
            continue
        if line.lstrip().startswith(("#", "//", "/*", "* ")) and not PORTABLE_MARK.search(line):
            continue  # A comment never runs.
        hits = []
        for index, (pattern, fails_on, guardable, problem) in enumerate(PORTABILITY_PATTERNS):
            broken = [system for system in fails_on if system in systems]
            if not broken or (guardable and guarded):
                continue
            if index == 0 and SHELL_OPTION.search(line):
                continue  # Started through a shell, which finds the .cmd file; the shell pattern reports the line instead.
            match = pattern.search(line)
            if match:
                named = next((group for group in match.groups() if group), match.group(0)) if match.groups() else match.group(0)
                hits.append(f"fails on {system_list(broken)}: {problem.format(named)}")
        if not hits:
            continue
        mark = PORTABLE_MARK.search(line)
        if mark:
            if marks is not None:
                marks.append(Finding("portability", path, number, f"marked portable: {mark.group(1)}"))
            continue
        findings.extend(Finding("portability", path, number, hit) for hit in hits)
    if "windows" in systems:
        # A call split across lines, such as spawn( on one line and 'npx' on the next, which the line patterns miss.
        reported = {finding.line for finding in findings}
        all_lines = text.splitlines()
        for match in SPAWN_PROGRAM.finditer(text):
            first = text.count("\n", 0, match.start()) + 1
            last = text.count("\n", 0, match.end()) + 1
            if first == last or last in reported or (wanted is not None and not set(range(first, last + 1)) & wanted):
                continue
            if any(SHELL_OPTION.search(line) for line in all_lines[first - 1:last + 3]):
                continue
            mark = next((m for m in (PORTABLE_MARK.search(line) for line in all_lines[first - 1:last]) if m), None)
            if mark:
                if marks is not None:
                    marks.append(Finding("portability", path, last, f"marked portable: {mark.group(1)}"))
                continue
            findings.append(Finding("portability", path, last, "fails on Windows: " + PORTABILITY_PATTERNS[0][3].format(match.group(1))))
    return findings


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
            if HOOK_COMMAND.search(line):
                findings.append(Finding("machine-specific", path, number, "contains the command that runs a synced hook on this machine; write {{kit.literal hook MODULE/HOOK}} for a hook in the config repo"))
            match = FIXED_INTERPRETERS.search(line)
            if match:
                findings.append(Finding("machine-specific", path, number, f"calls the fixed interpreter path {match.group(0)!r}; call it by name"))
        return findings

    def placeholder_findings(self, path: str, text: str, header: bool = False) -> List[Finding]:
        """Check placeholders and {{kit.only}} blocks. With `header`, the file's --- header block cannot hold a block."""
        findings = []
        lines = text.splitlines(keepends=True)
        for number, line in enumerate(lines, start=1):
            for match in render.PLACEHOLDER.finditer(line):
                name, arg = match.group(1), match.group(2)
                if name in ("home", "projects", "worktrees", "module", "app", "os", "cli") and arg is None:
                    continue
                if name in ("hook", "literal") and arg:
                    continue
                if name in ("only", "end"):
                    if not (render.ONLY_START.match(line) or render.ONLY_END.match(line)):
                        findings.append(Finding("format", path, number, f"{match.group(0)} must be alone on its line"))
                    continue
                findings.append(Finding("format", path, number, f"unknown placeholder {match.group(0)}"))
        try:
            _hidden, _shown, markers = render.only_lines(lines, "claude")
            if markers and PurePosixPath(path).suffix != ".md":
                findings.append(Finding("format", path, min(markers) + 1, "{{kit.only}} blocks work only in Markdown files, because agent-kit-sync shows their edges as Markdown comments"))
            if markers and header:
                _lines, start, end = render.split_frontmatter(text)
                if start >= 0 and min(markers) <= end:
                    findings.append(Finding("format", path, min(markers) + 1, "a {{kit.only}} block is in the header block; write a field for one app with its name in front, such as claude.tools"))
        except render.RenderError as error:
            findings.append(Finding("format", path, 0, str(error)))
        return findings

    def python_findings(self, path: str, data: bytes) -> List[Finding]:
        try:
            compile(data, path, "exec", dont_inherit=True)
        except SyntaxError as error:
            return [Finding("python-syntax", path, error.lineno or 0, error.msg)]
        except ValueError as error:
            return [Finding("python-syntax", path, 0, str(error))]
        return []

    def file(self, path: str, data: bytes, *, own: bool, templated: bool = False, header: bool = False, added_lines: Optional[Iterable[int]] = None,
             systems: Optional[Sequence[str]] = SYSTEMS) -> List[Finding]:
        """Check one file. `own` is True for the user's files and False for files from a source.

        `systems` lists the operating systems the file's module installs on, for the portability
        check; None skips that check, for a module that never leaves this machine.
        """
        findings: List[Finding] = []
        if PurePosixPath(path).suffix == ".py":
            findings += self.python_findings(path, data)
        text = decode_any(data)
        binary = "\0" in text
        if binary:
            # Binary: look for blocked terms and secrets in the printable runs only.
            text = "\n".join(re.findall(r"[\x20-\x7e]{6,}", text))
            added_lines = None
        findings += self.text_findings(path, text, own=own, lines=added_lines)
        if own and systems and not binary and is_code(path, data):
            findings += portability_findings(path, text, systems, added_lines)
        if templated:
            findings += self.placeholder_findings(path, text, header)
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
