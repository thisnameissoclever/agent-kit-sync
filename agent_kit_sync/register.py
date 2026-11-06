"""Registering the dispatcher in each agent app's hook settings.

Each entry the tool writes carries the launcher path in its command, which is how the
tool recognizes its own entries later. Entries written by anything else are never changed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .dispatch import APP_EVENTS, WRITE_TOOLS, matcher_for
from .fsutil import ChangedUnderneath, atomic_write, read_or_none, sha256
from .model import Machine
from .paths import Layout, os_name

TOOL_EVENTS = ("session_start", "post_tool_use", "stop")
# PowerShell reads these as single quotes too, so each is written twice inside a quoted string.
POWERSHELL_QUOTES = "'\u2018\u2019\u201a\u201b"


def claude_version() -> Optional[Tuple[int, ...]]:
    try:
        out = subprocess.run(["claude", "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout.decode()
    except (OSError, subprocess.TimeoutExpired):
        return None
    import re
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(part) for part in match.groups()) if match else None


def needed(hooks: List[dict], app: str) -> Dict[str, dict]:
    """The events to register for one app: each with its matcher and whether failure blocks."""
    events: Dict[str, dict] = {}
    for event in TOOL_EVENTS:
        tools = WRITE_TOOLS.get(app) if event == "post_tool_use" else None
        events[event] = {"matcher": ("^(?:" + "|".join(tools) + ")$") if tools else "*", "closed": False, "names": set(tools or [])}
    for hook in hooks:
        if hook.get("apps") and app not in hook["apps"] and not (app == "claude" and "cursor" in hook["apps"]):
            continue
        for event in hook.get("events", []):
            if event not in APP_EVENTS.get(app, {}):
                continue
            entry = events.setdefault(event, {"matcher": None, "closed": False, "names": set()})
            if event in ("pre_tool_use", "post_tool_use"):
                match = matcher_for(app, hook.get("tools"))
                if match == "*" or entry["matcher"] == "*":
                    entry["matcher"] = "*"
                    entry["names"] = set()
                else:
                    import re
                    entry["names"] |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_.*]*", match[4:-2]))
                    entry["matcher"] = "^(?:" + "|".join(sorted(entry["names"])) + ")$"
            else:
                entry["matcher"] = "*"
            if hook.get("on_failure") == "closed":
                entry["closed"] = True
    return events


def command_parts(layout: Layout, machine: Machine, app: str, event: str) -> List[str]:
    return [machine.python or sys.executable, "-I", "-S", str(layout.launcher), "hook", app, event]


def shell_join(parts: List[str]) -> str:
    if os_name() == "windows":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def windows_hook_quote(text: str) -> Optional[str]:
    """One argument quoted so Git Bash and PowerShell both read it as exactly `text`, or None.

    In single quotes, Bash and PowerShell both take every character literally, so a path with
    spaces, $, or backticks is safe in either. They differ only on a quote character inside the
    quotes, so such an argument goes in double quotes instead, which both read literally when it
    holds no $, backtick, double quote, or doubled or final backslash.
    """
    if not any(quote in text for quote in POWERSHELL_QUOTES):
        return "'" + text + "'"
    if any(char in text for char in '"$`!\u201c\u201d\u201e') or "\\\\" in text or text.endswith("\\"):
        return None
    return '"' + text + '"'


def windows_hook_command(parts: List[str]) -> Optional[str]:
    """The program in parts[0] and its arguments, as a command Claude Code and Cursor both run on Windows.

    Claude Code runs a hook's command in Git Bash on Windows, and Cursor, which runs the hooks in
    Claude Code's settings, keeps only the command and runs it in PowerShell. Every part is quoted,
    so the command starts with a quote, and Cursor puts PowerShell's call operator, &, in front of
    such a command. Bash and PowerShell then start the same program with the same arguments. Returns
    None when a part cannot be quoted for both, such as a path holding both ' and $.
    """
    quoted = [windows_hook_quote(part) for part in parts]
    return None if None in quoted else " ".join(quoted)


def git_bash() -> Optional[str]:
    """The Git Bash that Claude Code runs hooks in on Windows, found the way Claude Code 2.1.318 finds it."""
    chosen = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if chosen and os.path.basename(chosen).lower() in ("bash.exe", "sh.exe", "bash", "sh") and os.path.exists(chosen):
        return chosen
    for path in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.exists(path):
            return path
    git = shutil.which("git")
    if git:
        path = os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe")
        if os.path.exists(path):
            return path
    return None


def windows_claude_handler(parts: List[str], bash: Optional[str]) -> dict:
    """A Claude Code entry for Windows that Claude Code and Cursor both run.

    With Git Bash, where Claude Code runs a hook with no "shell", the command is quoted for Bash and
    PowerShell alike. Without it, Claude Code runs hooks in PowerShell, so the command starts with
    PowerShell's call operator and the entry says so. A path that cannot be quoted for both keeps the
    exec form, which Claude Code runs and Cursor cannot.
    """
    command = windows_hook_command(parts)
    if command is None:
        return {"type": "command", "command": parts[0], "args": parts[1:], "timeout": 60}
    if bash:
        return {"type": "command", "command": command, "timeout": 60}
    return {"type": "command", "shell": "powershell", "command": "& " + command, "timeout": 60}


def interpreter_only(handler: dict, python: str) -> bool:
    """True for an entry that runs only this machine's Python, with no script or arguments.

    Python then reads the hook's input as a program and fails on every event. An agent-kit-sync
    entry looks like this after a program rewrote the settings file and dropped its arguments.
    """
    if not isinstance(handler, dict) or handler.get("args"):
        return False
    command = str(handler.get("command", "")).strip()
    return bool(command) and command in (python, shlex.quote(python), subprocess.list2cmdline([python]))


def ours(command: str, launcher: Path) -> bool:
    """True when a command runs this kit's launcher, written plainly or quoted for a shell."""
    path = str(launcher)
    forms = {path, path.replace("\\", "/"), shlex.quote(path), subprocess.list2cmdline([path]), windows_hook_quote(path)}
    return any(form in command for form in forms if form)


def _entry_command(handler: dict) -> str:
    command = str(handler.get("command", ""))
    args = handler.get("args")
    if isinstance(args, list):
        command += " " + " ".join(str(arg) for arg in args)
    return command


def update_file(path: Path, layout: Layout, desired: Dict[str, dict]) -> bool:
    """Make the tool's entries in one hook settings file match `desired`. Returns True if it changed."""
    for _attempt in range(3):
        raw = read_or_none(path)
        try:
            data = json.loads(raw.decode("utf-8")) if raw and raw.strip() else {}
        except ValueError as error:
            raise ValueError(f"{path} is not valid JSON: {error}") from None
        hooks = data.setdefault("hooks", {})
        changed = False
        # Remove our entries that are no longer wanted or differ from what is wanted.
        for native_event in list(hooks):
            groups = hooks[native_event]
            if not isinstance(groups, list):
                continue
            kept = []
            for group in groups:
                handlers = group.get("hooks", []) if isinstance(group, dict) else []
                mine = [h for h in handlers if isinstance(h, dict) and ours(_entry_command(h), layout.launcher)]
                if not mine:
                    kept.append(group)
                    continue
                wanted = desired.get(native_event)
                if wanted and len(handlers) == 1 and group.get("matcher") == wanted["matcher"] and handlers[0] == wanted["handler"]:
                    kept.append(group)
                    wanted["present"] = True
                    continue
                changed = True
                others = [h for h in handlers if h not in mine]
                if others:
                    kept.append(dict(group, hooks=others))
            hooks[native_event] = kept
            if not kept:
                del hooks[native_event]
                changed = True
        for native_event, wanted in desired.items():
            if wanted.get("present"):
                continue
            hooks.setdefault(native_event, []).append({"matcher": wanted["matcher"], "hooks": [wanted["handler"]]})
            changed = True
        if not hooks:
            data.pop("hooks", None)
        if not changed:
            return False
        from .settings import json_indent
        original = raw.decode("utf-8") if raw else ""
        text = json.dumps(data, indent=json_indent(original), ensure_ascii=False) + ("\n" if raw is None or raw.endswith(b"\n") else "")
        try:
            atomic_write(path, text.encode("utf-8"), expect=sha256(raw) if raw is not None else None)
            return True
        except ChangedUnderneath:
            for wanted in desired.values():
                wanted.pop("present", None)
    raise ValueError(f"{path} kept changing while agent-kit-sync updated it")


def desired_entries(layout: Layout, machine: Machine, app: str, hooks: List[dict], on_failure_supported: bool) -> Dict[str, dict]:
    result = {}
    for event, spec in needed(hooks, app).items():
        native = APP_EVENTS[app][event]
        parts = command_parts(layout, machine, app, event)
        if app == "claude":
            # The whole command sits in one string, because a program that rewrites settings.json
            # may keep only each handler's type and command, and because Cursor, which runs these
            # entries too, keeps only the command. On Windows the string is quoted so that Git Bash,
            # where Claude Code runs it, and PowerShell, where Cursor runs it, read it the same way.
            # A path no quoting suits both keeps the exec form, which Cursor cannot run; doctor says so.
            if os_name() == "windows":
                handler = windows_claude_handler(parts, git_bash())
            else:
                handler = {"type": "command", "command": shell_join(parts), "timeout": 60}
            if spec["closed"] and on_failure_supported and event not in ("stop", "subagent_stop"):
                handler["onFailure"] = "block"
        else:
            handler = {"type": "command", "command": shell_join(parts), "timeout": 60}
            if os_name() == "windows":
                handler["commandWindows"] = subprocess.list2cmdline(parts)
        matcher = spec["matcher"] if event in ("pre_tool_use", "post_tool_use") else ("" if app == "claude" else None)
        result[native] = {"matcher": matcher, "handler": handler}
    return result


def register(layout: Layout, machine: Machine, hooks: List[dict]) -> Dict[str, bool]:
    """Write the dispatcher entries for each app. Returns which apps' files changed."""
    changed = {}
    version = claude_version() if "claude" in machine.apps else None
    supported = version is not None and version >= (2, 1, 295)
    for app in ("claude", "codex"):
        if app not in machine.apps:
            continue
        path = machine.app_homes[app] / ("settings.json" if app == "claude" else "hooks.json")
        changed[app] = update_file(path, layout, desired_entries(layout, machine, app, hooks, supported))
    return changed


def unregister(layout: Layout, machine: Machine) -> Dict[str, bool]:
    changed = {}
    for app in ("claude", "codex"):
        if app not in machine.apps:
            continue
        path = machine.app_homes[app] / ("settings.json" if app == "claude" else "hooks.json")
        if path.exists():
            changed[app] = update_file(path, layout, {})
    return changed
