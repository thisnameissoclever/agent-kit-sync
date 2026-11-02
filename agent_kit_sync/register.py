"""Registering the dispatcher in each agent app's hook settings.

Each entry the tool writes carries the launcher path in its command, which is how the
tool recognizes its own entries later. Entries written by anything else are never changed.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .dispatch import APP_EVENTS, WRITE_TOOLS, matcher_for
from .fsutil import ChangedUnderneath, atomic_write, read_or_none, sha256
from .model import Machine
from .paths import Layout, os_name

TOOL_EVENTS = ("session_start", "post_tool_use", "stop")


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


def ours(command: str, launcher: Path) -> bool:
    return str(launcher) in command or str(launcher).replace("\\", "/") in command


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
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
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
            handler = {"type": "command", "command": parts[0], "args": parts[1:], "timeout": 60}
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
