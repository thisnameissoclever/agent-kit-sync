"""The dispatcher: one registered command per event that runs every synced hook for it.

It must start fast, so it imports only the standard library modules it needs and reads
the index directly. Everything slow, such as a sync run, happens in a detached process.
"""

from __future__ import annotations

import json
import os
import sys

# The tool's event names, and each app's name for them.
APP_EVENTS = {
    "claude": {"session_start": "SessionStart", "user_prompt": "UserPromptSubmit", "pre_tool_use": "PreToolUse", "post_tool_use": "PostToolUse",
               "stop": "Stop", "subagent_start": "SubagentStart", "subagent_stop": "SubagentStop", "pre_compact": "PreCompact", "session_end": "SessionEnd"},
    "codex": {"session_start": "SessionStart", "user_prompt": "UserPromptSubmit", "pre_tool_use": "PreToolUse", "post_tool_use": "PostToolUse",
              "stop": "Stop", "subagent_start": "SubagentStart", "subagent_stop": "SubagentStop", "pre_compact": "PreCompact", "session_end": "SessionEnd"},
}
# Each app's tool names, by the tool's own names.
APP_TOOLS = {
    "claude": {"shell": ["Bash", "PowerShell"], "read": ["Read"], "write": ["Write", "NotebookEdit"], "edit": ["Edit", "MultiEdit"],
               "search": ["Grep", "Glob"], "web": ["WebFetch", "WebSearch"], "task": ["Agent", "Task"], "mcp": ["mcp__.*"]},
    "codex": {"shell": ["Bash"], "read": [], "write": ["apply_patch"], "edit": ["apply_patch"], "search": [], "web": [], "task": ["Agent"], "mcp": ["mcp__.*"]},
}
WRITE_TOOLS = {"claude": ["Write", "Edit", "MultiEdit", "NotebookEdit"], "codex": ["apply_patch"]}
CLAUDE_MIN_ON_FAILURE = (2, 1, 295)


def tool_kind(app: str, name: str) -> str:
    if name.startswith("mcp__"):
        return "mcp"
    for kind, names in APP_TOOLS.get(app, {}).items():
        if name in names:
            return kind
    if app == "codex" and name in ("exec_command", "functions.exec_command", "shell", "local_shell"):
        return "shell"
    return name.lower()


def matcher_for(app: str, tools) -> str:
    """A regex matching the app's names for the tool kinds, or "*" for every tool."""
    if not tools:
        return "*"
    names = []
    for kind in tools:
        names.extend(APP_TOOLS.get(app, {}).get(kind, []))
    names = sorted(set(names))
    return "^(?:" + "|".join(names) + ")$" if names else "*"


# ---------------------------------------------------------------- running hooks


def caller(app: str, payload: dict) -> str:
    """Cursor runs Claude Code's entries; report the app that actually called."""
    if app == "claude" and ("cursor_version" in payload or os.environ.get("CURSOR_VERSION")):
        return "cursor"
    return app


def run_one(hook: dict, app: str, event: str, raw: bytes, payload: dict, env: dict) -> dict:
    import subprocess
    import time
    # One registration serves every app, so {{kit.app}} becomes the app that called the dispatcher.
    calling = env.get("AGENT_KIT_SYNC_APP") or app
    args = [str(arg).replace("{{kit.app}}", calling) for arg in hook.get("args", [])]
    path = hook["path"]
    runtime = hook.get("runtime", "python")
    if runtime == "python":
        cmd = [sys.executable, path] + args
    elif runtime == "node":
        cmd = ["node", path] + args
    elif runtime == "bash":
        cmd = ["bash", path] + args
    else:
        cmd = [path] + args
    if hook.get("protocol") == "kit":
        data = json.dumps(kit_input(app, event, payload)).encode("utf-8")
    else:
        data = raw
    started = time.monotonic()
    try:
        result = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=hook.get("timeout", 10), env=env, cwd=payload.get("cwd") or None)
        return {"hook": hook, "code": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "seconds": time.monotonic() - started}
    except subprocess.TimeoutExpired:
        return {"hook": hook, "code": None, "stdout": b"", "stderr": b"timed out", "timeout": True, "seconds": time.monotonic() - started}
    except OSError as error:
        return {"hook": hook, "code": None, "stdout": b"", "stderr": str(error).encode(), "seconds": time.monotonic() - started}


def kit_input(app: str, event: str, payload: dict) -> dict:
    return {
        "app": caller(app, payload), "event": event, "tool": tool_kind(app, str(payload.get("tool_name", ""))),
        "tool_input": payload.get("tool_input"), "session_id": payload.get("session_id", ""), "cwd": payload.get("cwd", ""),
        "stop_hook_active": bool(payload.get("stop_hook_active")), "prompt": payload.get("prompt"), "native": payload,
    }


def failed(result: dict) -> bool:
    if result.get("timeout") or result["code"] is None:
        return True
    if result["code"] not in (0, 2):
        return True
    if result["hook"].get("protocol") == "kit" and result["code"] == 0 and result["stdout"].strip():
        try:
            json.loads(result["stdout"].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return True
    return False


def parse_json(blob: bytes):
    text = blob.decode("utf-8", "replace").strip()
    if not text.startswith("{"):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class Combined:
    """Everything the matching hooks said, before it is written in the app's format."""

    def __init__(self):
        self.blocked = False
        self.decision = None          # allow, deny, ask, continue
        self.reasons = []
        self.context = []
        self.user = []
        self.updated_input = None
        self.extra = {}               # other JSON fields from a single native hook

    def note_decision(self, value: str, reason: str = "") -> None:
        rank = {"deny": 4, "block": 4, "continue": 3, "ask": 2, "allow": 1}
        if self.decision is None or rank.get(value, 0) > rank.get(self.decision, 0):
            self.decision = value
        if reason:
            self.reasons.append(reason)


def combine(app: str, event: str, results) -> Combined:
    out = Combined()
    for result in results:
        hook = result["hook"]
        name = f"{hook['module']}/{hook['name']}"
        if failed(result):
            detail = "timed out" if result.get("timeout") else (result["stderr"].decode("utf-8", "replace").strip()[-300:] or f"exit code {result['code']}")
            if hook.get("on_failure") == "closed":
                message = f"The {name} hook failed ({detail}), and it is set to block when it fails."
                if event in ("pre_tool_use", "user_prompt", "stop", "subagent_stop", "post_tool_use"):
                    out.note_decision("deny" if event in ("pre_tool_use", "user_prompt", "post_tool_use") else "continue", message)
                    out.blocked = True
                else:
                    out.user.append("agent-kit-sync: " + message + " This event cannot be blocked, so the session continues.")
            else:
                out.user.append(f"agent-kit-sync: the {name} hook failed ({detail}) and was skipped.")
            continue
        if hook.get("protocol") == "kit":
            data = parse_json(result["stdout"]) or {}
            decision = data.get("decision", "allow")
            if decision in ("deny", "ask", "continue"):
                out.note_decision(decision, str(data.get("reason", "")))
            if data.get("agent_context"):
                out.context.append(str(data["agent_context"]))
            if data.get("user_message"):
                out.user.append(str(data["user_message"]))
            if data.get("updated_input") is not None:
                out.updated_input = data["updated_input"]
            continue
        # Native hook: read exit code 2, the app's JSON fields, or plain output.
        if result["code"] == 2:
            out.note_decision("deny" if event in ("pre_tool_use", "user_prompt") else "continue", result["stderr"].decode("utf-8", "replace").strip())
            out.blocked = True
            continue
        data = parse_json(result["stdout"])
        if data is None:
            text = result["stdout"].decode("utf-8", "replace").strip()
            if text and event in ("session_start", "user_prompt"):
                out.context.append(text)
            continue
        specific = data.get("hookSpecificOutput") or {}
        permission = specific.get("permissionDecision")
        if permission in ("deny", "ask", "allow"):
            out.note_decision(permission, str(specific.get("permissionDecisionReason", "")) if permission != "allow" else "")
        if data.get("decision") == "block":
            out.note_decision("deny" if event in ("pre_tool_use", "user_prompt", "post_tool_use") else "continue", str(data.get("reason", "")))
        if specific.get("additionalContext"):
            out.context.append(str(specific["additionalContext"]))
        if specific.get("updatedInput") is not None:
            out.updated_input = specific["updatedInput"]
        if data.get("systemMessage"):
            out.user.append(str(data["systemMessage"]))
        for key in ("continue", "stopReason", "suppressOutput"):
            if key in data:
                out.extra[key] = data[key]
    return out


def emit(app: str, event: str, combined: Combined, payload: dict, cursor_ask_is_deny: bool) -> tuple:
    """Return (exit code, stdout bytes) in the calling app's format."""
    native_event = APP_EVENTS.get(app, {}).get(event, event)
    decision = combined.decision
    if decision == "ask" and cursor_ask_is_deny:
        decision = "deny"
        combined.reasons.append("Ask the user before running this; the app does not support confirmation prompts from hooks.")
    if app == "codex" and decision == "ask":
        decision = "deny"
        combined.reasons.append("Ask the user before running this.")
    reason = "\n".join(r for r in combined.reasons if r)
    out = {}
    specific = {}
    if event == "pre_tool_use":
        if decision in ("deny", "ask"):
            specific = {"hookEventName": native_event, "permissionDecision": decision, "permissionDecisionReason": reason or "Blocked by a hook."}
        elif decision == "allow" or combined.updated_input is not None:
            specific = {"hookEventName": native_event, "permissionDecision": "allow"}
            if combined.updated_input is not None:
                specific["updatedInput"] = combined.updated_input
    elif event in ("stop", "subagent_stop"):
        if decision == "continue" or (decision == "deny"):
            out["decision"] = "block"
            out["reason"] = reason or "A hook asked the agent to keep working."
    elif event in ("user_prompt", "post_tool_use"):
        if decision in ("deny", "continue"):
            out["decision"] = "block"
            out["reason"] = reason or "Blocked by a hook."
    if combined.context and event not in ("stop", "subagent_stop") or (combined.context and app == "claude"):
        specific.setdefault("hookEventName", native_event)
        specific["additionalContext"] = "\n\n".join(combined.context)
    if specific:
        out["hookSpecificOutput"] = specific
    if combined.user:
        out["systemMessage"] = "\n".join(combined.user)
    out.update({k: v for k, v in combined.extra.items() if k not in out})
    if app == "codex" and event in ("stop", "subagent_stop") and not out:
        return 0, b"{}"
    return 0, (json.dumps(out).encode("utf-8") if out else b"")


# ---------------------------------------------------------------- entry point


def detach(argv, layout_root: str) -> None:
    """Start a sync run in the background, with no link to the session's pipes."""
    import subprocess
    logs = os.path.join(layout_root, "logs")
    os.makedirs(logs, exist_ok=True)
    log = open(os.path.join(logs, "sync-background.log"), "ab")
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True, "cwd": str(layout_root)}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)
    log.close()


def copies_changed(index: dict) -> bool:
    for entry in index.get("copies", {}).values():
        try:
            st = os.stat(entry["path"])
        except OSError:
            return True
        sig = entry.get("sig")
        if sig is None or sig != [st.st_size, st.st_mtime_ns]:
            return True
    return False


def _seen_path(state: str, session: str) -> str:
    return os.path.join(state, "seen", session.replace("/", "_").replace("\\", "_")[:120] + ".json")


def _seen(state: str, session: str) -> set:
    try:
        with open(_seen_path(state, session), encoding="utf-8") as handle:
            return set(json.load(handle))
    except (OSError, ValueError):
        return set()


def unseen_alerts(state: str, session: str) -> list:
    """Open alerts this session has not been shown. mark_shown records the ones delivered."""
    try:
        with open(os.path.join(state, "alerts.json"), encoding="utf-8") as handle:
            alerts = [a for a in json.load(handle) if not a.get("closed")]
    except (OSError, ValueError):
        return []
    if not alerts:
        return []
    seen = _seen(state, session)
    return [a for a in alerts if a.get("id") not in seen]


def mark_shown(state: str, session: str, alerts: list) -> None:
    if not alerts:
        return
    path = _seen_path(state, session)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(sorted(_seen(state, session) | {a["id"] for a in alerts}), handle)
        os.replace(tmp, path)
    except OSError:
        pass


def alert_lines(event: str, alerts: list) -> tuple:
    """The text for the user and the line for the agent that deliver these alerts."""
    user = ["agent-kit-sync: " + alert["message"] for alert in alerts]
    context = []
    if alerts and event != "stop":
        commands = sorted({alert.get("command") or "agent-kit-sync status" for alert in alerts})
        context.append(f"agent-kit-sync raised {len(alerts)} alert(s) for the user. Run `{commands[0]}` to see them, then follow the agent-kit-sync-resolve skill to help the user decide.")
    return user, context


def pass_through(app: str, event: str, results: list, alerts: list, ask_is_deny: bool):
    """When one native hook ran and did not fail, return its own (exit code, stdout, stderr, delivered alerts).

    The output stays byte for byte what the hook printed unless alerts must be added, or the hook
    asked for confirmation where the app may not enforce it (Codex, and any machine with Cursor,
    as in emit), which becomes a refusal telling the agent to ask. Alerts
    that cannot be added without rewriting the output, after exit code 2 or plain text, wait for
    the session's next event. Returns None when the combining path must handle the results.
    """
    if len(results) != 1:
        return None
    result = results[0]
    if result["hook"].get("protocol", "native") != "native" or failed(result):
        return None
    stdout, stderr = result["stdout"], result["stderr"]
    if result["code"] == 2:
        return 2, stdout, stderr, []
    if not stdout.strip():
        if alerts or (app == "codex" and event in ("stop", "subagent_stop")):
            return None  # The combining path adds the alerts, or the empty object Codex needs on stop.
        return 0, stdout, stderr, []
    data = parse_json(stdout)
    if data is None and alerts and event in ("session_start", "user_prompt"):
        return None  # Plain text here is context; the combining path keeps it and adds the alerts.
    if not isinstance(data, dict):
        return 0, stdout, stderr, []
    specific = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else None
    convert_ask = (ask_is_deny or app == "codex") and specific is not None and specific.get("permissionDecision") == "ask"
    if not alerts and not convert_ask:
        return 0, stdout, stderr, []
    user, context = alert_lines(event, alerts)
    if user:
        data["systemMessage"] = "\n".join(([str(data["systemMessage"])] if data.get("systemMessage") else []) + user)
    if context and (event not in ("stop", "subagent_stop") or app == "claude"):
        if specific is None:
            specific = data["hookSpecificOutput"] = {"hookEventName": APP_EVENTS.get(app, {}).get(event, event)}
        specific["additionalContext"] = "\n\n".join(([str(specific["additionalContext"])] if specific.get("additionalContext") else []) + context)
    if convert_ask:
        specific["permissionDecision"] = "deny"
        reason = str(specific.get("permissionDecisionReason") or "")
        note = "Ask the user before running this; the app does not support confirmation prompts from hooks." if ask_is_deny else "Ask the user before running this."
        specific["permissionDecisionReason"] = (reason + "\n" if reason else "") + note
    return 0, json.dumps(data).encode("utf-8"), stderr, alerts


def main(layout_root, launcher, app: str, event: str) -> int:
    layout_root = str(layout_root)
    raw = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    try:
        payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except ValueError:
        payload = {}
    state = os.path.join(layout_root, "state")
    try:
        with open(os.path.join(state, "index.json"), encoding="utf-8") as handle:
            index = json.load(handle)
    except (OSError, ValueError):
        index = {}
    hooks = [h for h in index.get("hooks", []) if event in h.get("events", [])]
    calling = caller(app, payload)
    hooks = [h for h in hooks if not h.get("apps") or calling in h["apps"] or (calling == "cursor" and "claude" in h["apps"])]
    if event in ("pre_tool_use", "post_tool_use"):
        kind = tool_kind(app, str(payload.get("tool_name", "")))
        hooks = [h for h in hooks if not h.get("tools") or kind in h["tools"]]
    hooks.sort(key=lambda h: (h.get("order", 0), h.get("name", "")))
    results = []
    if hooks:
        from concurrent.futures import ThreadPoolExecutor
        env = dict(os.environ)
        env["AGENT_KIT_SYNC_APP"] = calling
        with ThreadPoolExecutor(max_workers=min(8, len(hooks))) as pool:
            results = list(pool.map(lambda h: run_one(h, app, event, raw, payload, env), hooks))
    # Alerts: one line to the user and one to the agent, once per session.
    session = str(payload.get("session_id") or "")
    fresh = []
    if session and event in ("session_start", "user_prompt", "pre_tool_use", "post_tool_use", "stop"):
        fresh = unseen_alerts(state, session)
    cursor_ask_is_deny = calling == "cursor" or (app == "claude" and os.path.isdir(os.path.join(os.path.expanduser("~"), ".cursor")))
    passed = pass_through(app, event, results, fresh, cursor_ask_is_deny)
    if passed is None:
        combined = combine(app, event, results)
        user, context = alert_lines(event, fresh)
        combined.user += user
        combined.context += context
        delivered = fresh
    # Start a background sync when this event calls for one.
    want_sync = event == "session_start"
    if event == "post_tool_use" and str(payload.get("tool_name", "")) in WRITE_TOOLS.get(app, []):
        want_sync = True
    if event == "stop" and copies_changed(index):
        want_sync = True
    if want_sync and not os.environ.get("AGENT_KIT_SYNC_NO_BACKGROUND"):
        try:
            detach([sys.executable, "-I", "-S", str(launcher), "sync", "--background", "--quiet-wait" if event == "post_tool_use" else "--now"], layout_root)
        except OSError:
            pass
    if passed is None:
        code, out = emit(app, event, combined, payload, cursor_ask_is_deny)
        # A lone native hook that succeeded keeps its own error text even when its answer is combined.
        lone = results[0] if len(results) == 1 else None
        err = lone["stderr"] if lone and lone["hook"].get("protocol", "native") == "native" and not failed(lone) else b""
    else:
        code, out, err, delivered = passed
    if session:
        mark_shown(state, session, delivered)
    if err:
        sys.stderr.buffer.write(err)
    if out:
        sys.stdout.buffer.write(out)
    return code
