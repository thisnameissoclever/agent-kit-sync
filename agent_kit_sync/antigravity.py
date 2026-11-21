"""Antigravity's calls to the hooks registered in its managed plugin hooks.json.

Antigravity sends camelCase payloads with event names such as `PreInvocation` and `PreToolUse`,
tool calls under `toolCall`, and JSONL transcripts with `USER_INPUT` / `PLANNER_RESPONSE` steps.
The dispatcher uses this module to give each synced hook the input Claude Code would have sent,
and to convert the combined answer into Antigravity's response format.

Only the standard library is used, because the dispatcher must start fast.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from . import cursor
from .dispatch import APP_EVENTS

CLAUDE_EVENTS = APP_EVENTS["claude"]
TOOL_NAMES = {
    "run_command": "Bash",
    "read_file": "Read",
    "view_file": "Read",
    "write_file": "Write",
    "write_to_file": "Write",
    "edit_file": "Edit",
    "replace_file_content": "Edit",
    "multi_replace_file_content": "MultiEdit",
    "grep_search": "Grep",
    "find_by_name": "Glob",
    "list_dir": "Glob",
    "search_web": "WebSearch",
    "read_url_content": "WebFetch",
    "invoke_subagent": "Agent",
    "ask_question": "AskUserQuestion",
}
MAX_TRANSCRIPT = 4_000_000
KEEP_SECONDS = 7 * 24 * 3600
SUPERSEDED_SECONDS = 120
MAX_PROMPT_TURNS = 200
UNAPPLIED_EDIT = (
    "A hook changed this edit in a way agent-kit-sync cannot turn into Antigravity's write of the whole file, "
    "so the edit was refused. Read the file again and make the edit again."
)


def raw_tool_name(payload: dict) -> str:
    call = payload.get("toolCall") if isinstance(payload, dict) else None
    return str(call.get("name") or "") if isinstance(call, dict) else ""


def _user_text(text: str) -> str:
    match = re.search(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", text, re.S)
    return match.group(1) if match else text


def allow_stop_hooks(payload: dict) -> bool:
    reason = payload.get("terminationReason") if isinstance(payload, dict) else None
    return reason in (None, "", "NO_TOOL_CALL")


def _prompt_turn_key(payload: dict) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    raw_input = payload.get("lastUserInput")
    prompt = _user_text(raw_input) if isinstance(raw_input, str) else ""
    if not prompt.strip():
        return None
    invocation = payload.get("invocationNum")
    if isinstance(invocation, int) and not isinstance(invocation, bool) and invocation > 1:
        return None
    conv_id = str(payload.get("conversationId") or "")
    initial_steps = payload.get("initialNumSteps")
    digest = hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{conv_id}|{initial_steps}|{digest}"


def _read_prompt_turns(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as handle:
            seen = json.load(handle)
    except (OSError, ValueError):
        return []
    return seen if isinstance(seen, list) else []


def _write_prompt_turns(folder: str, path: str, seen: list) -> None:
    data = json.dumps(seen[-MAX_PROMPT_TURNS:]).encode("utf-8")
    for attempt in range(3):
        try:
            os.makedirs(folder, exist_ok=True)
            cursor._replace(path, data)
            return
        except OSError:
            if attempt < 2:
                time.sleep(0.02)


def record_user_prompt(payload: dict, state: str) -> None:
    """Persist the current turn key after user_prompt hooks allow the prompt to proceed."""
    key = _prompt_turn_key(payload)
    if not key:
        return
    folder = os.path.join(state, "antigravity")
    path = os.path.join(folder, "prompt-turns.json")
    seen = _read_prompt_turns(path)
    if key in seen:
        return
    seen.append(key)
    _write_prompt_turns(folder, path, seen)


def should_run_user_prompt(payload: dict, state: str, record: bool = True) -> bool:
    """Fire PreInvocation as UserPromptSubmit once per user turn."""
    key = _prompt_turn_key(payload)
    if not key:
        return False
    folder = os.path.join(state, "antigravity")
    path = os.path.join(folder, "prompt-turns.json")
    seen = _read_prompt_turns(path)
    if key in seen:
        return False
    if record:
        seen.append(key)
        _write_prompt_turns(folder, path, seen)
    return True


def _decode_transcript_args(args) -> dict:
    if not isinstance(args, dict):
        return {}
    out = {}
    for key, value in args.items():
        if isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except ValueError:
                out[key] = value
        else:
            out[key] = value
    return out


def _mcp_arguments(raw_args) -> dict:
    if isinstance(raw_args, dict):
        return dict(raw_args)
    if isinstance(raw_args, str):
        if not raw_args.strip():
            return {}
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except ValueError:
            return {"value": raw_args}
    return {}


def map_tool_call(name: str, args: dict) -> Tuple[str, dict]:
    """Translate an Antigravity tool name and argument dict into Claude Code's equivalents."""
    raw = dict(args) if isinstance(args, dict) else {}
    if name == "call_mcp_tool":
        server = str(raw.get("ServerName") or "mcp")
        tool = str(raw.get("ToolName") or "")
        return f"mcp__{server}__{tool}", _mcp_arguments(raw.get("Arguments"))
    if name.startswith("mcp_") and not name.startswith("mcp__"):
        return "mcp__" + name[4:], raw
    mapped = TOOL_NAMES.get(name, name)
    if mapped == "Bash":
        if "command" not in raw and isinstance(raw.get("CommandLine"), str):
            raw["command"] = raw["CommandLine"]
        if "cwd" not in raw and isinstance(raw.get("Cwd"), str):
            raw["cwd"] = raw["Cwd"]
    elif mapped == "Read":
        path = raw.get("Path") if isinstance(raw.get("Path"), str) else raw.get("AbsolutePath")
        if "file_path" not in raw and isinstance(path, str):
            raw["file_path"] = path
    elif mapped == "Write":
        path = raw.get("Path") if isinstance(raw.get("Path"), str) else raw.get("TargetFile")
        content = raw.get("Content") if isinstance(raw.get("Content"), str) else raw.get("CodeContent")
        if "file_path" not in raw and isinstance(path, str):
            raw["file_path"] = path
        if "content" not in raw and isinstance(content, str):
            raw["content"] = content
    elif mapped == "Edit":
        path = raw.get("Path") if isinstance(raw.get("Path"), str) else raw.get("TargetFile")
        old = raw.get("Target") if isinstance(raw.get("Target"), str) else raw.get("TargetContent")
        new = raw.get("Replacement") if isinstance(raw.get("Replacement"), str) else raw.get("ReplacementContent")
        if "file_path" not in raw and isinstance(path, str):
            raw["file_path"] = path
        if "old_string" not in raw and isinstance(old, str):
            raw["old_string"] = old
        if "new_string" not in raw and isinstance(new, str):
            raw["new_string"] = new
    elif mapped == "MultiEdit":
        if "file_path" not in raw and isinstance(raw.get("TargetFile"), str):
            raw["file_path"] = raw["TargetFile"]
        chunks = raw.get("ReplacementChunks")
        if "edits" not in raw and isinstance(chunks, list):
            edits = []
            for chunk in chunks:
                if isinstance(chunk, dict):
                    old_chunk = chunk.get("TargetContent")
                    new_chunk = chunk.get("ReplacementContent")
                    edits.append({
                        "old_string": old_chunk if isinstance(old_chunk, str) else "",
                        "new_string": new_chunk if isinstance(new_chunk, str) else "",
                    })
            raw["edits"] = edits
    return mapped, raw


def _existing_dir(path: str) -> Optional[str]:
    candidate = cursor.native_root(path)
    return candidate if candidate and os.path.isdir(candidate) else None


def _workspace_dir(payload: dict) -> Optional[str]:
    roots = payload.get("workspacePaths")
    if not isinstance(roots, list):
        return None
    for root in roots:
        if isinstance(root, str):
            found = _existing_dir(root)
            if found:
                return found
    return None


def to_claude(event: str, payload: dict) -> dict:
    """The input Claude Code would send for this event, built from Antigravity's payload."""
    out = dict(payload) if isinstance(payload, dict) else {}
    out["hook_event_name"] = CLAUDE_EVENTS.get(event, out.get("hook_event_name"))
    conv_id = out.get("conversationId")
    if isinstance(conv_id, str) and conv_id and "session_id" not in out:
        out["session_id"] = conv_id
    transcript = out.get("transcriptPath")
    if isinstance(transcript, str) and transcript and "transcript_path" not in out:
        out["transcript_path"] = transcript
    cwd = out.get("cwd")
    resolved_cwd = _existing_dir(cwd) if isinstance(cwd, str) else None
    if resolved_cwd:
        out["cwd"] = resolved_cwd
    else:
        workspace = _workspace_dir(out)
        if workspace:
            out["cwd"] = workspace
        else:
            out.pop("cwd", None)
    if event == "session_start":
        out.setdefault("source", "startup")
    elif event == "user_prompt":
        raw_input = out.get("lastUserInput")
        if isinstance(raw_input, str) and "prompt" not in out:
            out["prompt"] = _user_text(raw_input)
    elif event in ("pre_tool_use", "post_tool_use"):
        call = out.get("toolCall")
        if isinstance(call, dict):
            raw_name = str(call.get("name") or "")
            raw_args = call.get("args") if isinstance(call.get("args"), dict) else {}
            mapped_name, mapped_input = map_tool_call(raw_name, raw_args)
            out["tool_name"] = mapped_name
            out["tool_input"] = mapped_input
            out["antigravity_tool"] = raw_name
            out["antigravity_args"] = dict(raw_args)
            if mapped_name == "Bash":
                folder = raw_args.get("Cwd")
                if isinstance(folder, str) and folder:
                    folder = cursor.native_root(folder)
                    base = out.get("cwd") if isinstance(out.get("cwd"), str) else None
                    if not os.path.isabs(folder) and base:
                        folder = os.path.join(base, folder)
                    if os.path.isabs(folder) and os.path.isdir(folder):
                        out["cwd"] = os.path.abspath(folder)
            if event == "pre_tool_use" and mapped_name == "Write" and not raw_args.get("Append"):
                as_edit = cursor.edit_of_existing_file(mapped_input)
                if as_edit is not None:
                    out["tool_name"] = "Edit"
                    out["tool_input"] = as_edit
                    out["antigravity_converted_write"] = True
    elif event == "stop":
        if "stop_hook_active" not in out:
            out["stop_hook_active"] = bool(out.get("continuationActive"))
    return out


# ------------------------------------------------------------------ transcripts


def claude_row(row: dict) -> Optional[dict]:
    """One transcript row in Claude Code's format, or None for steps hooks should ignore."""
    kind = row.get("type")
    if kind == "USER_INPUT":
        content = row.get("content")
        if not isinstance(content, str):
            return None
        text = _user_text(content)
        if not text.strip():
            return None
        return {
            "type": "user",
            "uuid": f"step-{row.get('step_index', '')}",
            "timestamp": row.get("created_at", ""),
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    if kind == "PLANNER_RESPONSE":
        blocks = []
        content = row.get("content")
        if isinstance(content, str) and content.strip():
            blocks.append({"type": "text", "text": content})
        tool_calls = row.get("tool_calls")
        for call in tool_calls if isinstance(tool_calls, list) else []:
            if not isinstance(call, dict):
                continue
            raw_name = str(call.get("name") or "")
            raw_args = _decode_transcript_args(call.get("args") or {})
            mapped_name, mapped_input = map_tool_call(raw_name, raw_args)
            blocks.append({"type": "tool_use", "name": mapped_name, "input": mapped_input})
        if not blocks:
            return None
        return {
            "type": "assistant",
            "uuid": f"step-{row.get('step_index', '')}",
            "timestamp": row.get("created_at", ""),
            "message": {"role": "assistant", "content": blocks},
        }
    return None


def _current_turn_last_text(rows: List[dict]) -> str:
    """Latest non-empty assistant text since the last user row in translated Claude-format rows."""
    start = 0
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].get("type") == "user":
            start = index + 1
            break
    for row in reversed(rows[start:]):
        if row.get("type") != "assistant":
            continue
        blocks = (row.get("message") or {}).get("content") or []
        for block in reversed(blocks):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    return text
    return ""


def translate_transcript(source: str, folder: str) -> Optional[str]:
    """Write the tail of an Antigravity transcript in Claude Code's format and return its path."""
    try:
        info = os.stat(source)
    except OSError:
        return None
    name = hashlib.sha256(os.path.abspath(source).encode("utf-8")).hexdigest()[:24]
    version = hashlib.sha256(f"{info.st_size} {info.st_mtime_ns}".encode("utf-8")).hexdigest()[:12]
    target = os.path.join(folder, f"{name}-{version}.jsonl")
    if os.path.isfile(target):
        try:
            os.utime(target)
        except OSError:
            pass
        return target
    try:
        with open(source, "rb") as stream:
            start = max(0, info.st_size - MAX_TRANSCRIPT)
            stream.seek(start)
            if start:
                stream.readline()
            data = stream.read(MAX_TRANSCRIPT)
        lines = []
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            converted = claude_row(row) if isinstance(row, dict) else None
            if converted is not None:
                lines.append(json.dumps(converted))
        os.makedirs(folder, exist_ok=True)
        cursor._replace(target, ("\n".join(lines) + "\n" if lines else "").encode("utf-8"))
    except OSError:
        return target if os.path.isfile(target) else None
    cutoff = time.time() - SUPERSEDED_SECONDS
    for entry in os.listdir(folder):
        if entry.startswith(name + "-") and entry.endswith(".jsonl") and entry != os.path.basename(target):
            path = os.path.join(folder, entry)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    return target


def with_transcripts(payload: dict, state: str, event: str = "") -> dict:
    """Point `transcript_path` at a Claude-format copy and populate stop's last_assistant_message."""
    out = dict(payload)
    folder = os.path.join(state, "antigravity")
    path = out.get("transcript_path")
    translated = None
    if isinstance(path, str) and path:
        translated = translate_transcript(path, folder)
        if translated:
            out["transcript_path"] = translated
        else:
            out.pop("transcript_path", None)
    elif "transcript_path" in out:
        out.pop("transcript_path")
    if event == "stop" and translated and not (isinstance(out.get("last_assistant_message"), str) and out["last_assistant_message"].strip()):
        try:
            with open(translated, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
            text = _current_turn_last_text(rows)
            if text:
                out["last_assistant_message"] = text
        except (OSError, ValueError):
            pass
    return out


def remove_old(state: str) -> None:
    folder = os.path.join(state, "antigravity")
    try:
        names = os.listdir(folder)
    except OSError:
        return
    cutoff = time.time() - KEEP_SECONDS
    for entry in names:
        if entry == "prompt-turns.json":
            continue
        path = os.path.join(folder, entry)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


# ------------------------------------------------------------------ answers


def antigravity_overwrite(updated: dict, sent: dict) -> Optional[dict]:
    """Convert Claude-style updatedInput into an Antigravity shallow overwrite map."""
    if not isinstance(updated, dict):
        return None
    raw_tool = sent.get("antigravity_tool")
    raw_args = sent.get("antigravity_args")

    def _same_keys_or_none() -> Optional[dict]:
        if isinstance(raw_args, dict) and updated and set(updated) <= set(raw_args):
            return dict(updated)
        return None

    if sent.get("antigravity_converted_write"):
        if "file_path" in updated and updated["file_path"] != sent.get("tool_input", {}).get("file_path"):
            return None
        whole = sent.get("tool_input", {}).get("old_string", "")
        old, new = updated.get("old_string"), updated.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or old not in whole:
            return None
        content = whole.replace(old, new, 1)
        return {"CodeContent": content} if raw_tool == "write_to_file" else {"Content": content}
    if raw_tool == "run_command":
        res: Dict[str, object] = {}
        if "command" in updated:
            res["CommandLine"] = updated["command"]
        if "cwd" in updated:
            res["Cwd"] = updated["cwd"]
        return res or _same_keys_or_none()
    if raw_tool == "edit_file":
        res = {}
        if "file_path" in updated:
            res["Path"] = updated["file_path"]
        if "old_string" in updated:
            res["Target"] = updated["old_string"]
        if "new_string" in updated:
            res["Replacement"] = updated["new_string"]
        return res or _same_keys_or_none()
    if raw_tool == "replace_file_content":
        res = {}
        if "file_path" in updated:
            res["TargetFile"] = updated["file_path"]
        if "old_string" in updated:
            res["TargetContent"] = updated["old_string"]
        if "new_string" in updated:
            res["ReplacementContent"] = updated["new_string"]
        return res or _same_keys_or_none()
    if raw_tool == "multi_replace_file_content":
        res = {}
        if "file_path" in updated and isinstance(updated["file_path"], str):
            res["TargetFile"] = updated["file_path"]
        if "edits" in updated:
            edits = updated.get("edits")
            orig_chunks = raw_args.get("ReplacementChunks") if isinstance(raw_args, dict) else None
            if not isinstance(edits, list):
                return None
            if isinstance(orig_chunks, list) and len(edits) != len(orig_chunks):
                return None
            chunks = []
            for idx, item in enumerate(edits):
                if not isinstance(item, dict) or not isinstance(item.get("old_string"), str) or not isinstance(item.get("new_string"), str):
                    return None
                base = dict(orig_chunks[idx]) if isinstance(orig_chunks, list) and isinstance(orig_chunks[idx], dict) else {}
                base["TargetContent"] = item["old_string"]
                base["ReplacementContent"] = item["new_string"]
                chunks.append(base)
            res["ReplacementChunks"] = chunks
        return res or _same_keys_or_none()
    if raw_tool == "write_file":
        res = {}
        if "file_path" in updated:
            res["Path"] = updated["file_path"]
        if "content" in updated:
            res["Content"] = updated["content"]
        return res or _same_keys_or_none()
    if raw_tool == "write_to_file":
        res = {}
        if "file_path" in updated:
            res["TargetFile"] = updated["file_path"]
        if "content" in updated:
            res["CodeContent"] = updated["content"]
        return res or _same_keys_or_none()
    if raw_tool == "read_file":
        return {"Path": updated["file_path"]} if "file_path" in updated else _same_keys_or_none()
    if raw_tool == "view_file":
        return {"AbsolutePath": updated["file_path"]} if "file_path" in updated else _same_keys_or_none()
    if raw_tool == "call_mcp_tool":
        return {"Arguments": updated}
    if isinstance(raw_tool, str) and raw_tool.startswith("mcp_"):
        return dict(updated)
    return _same_keys_or_none()


def _inject_steps(notice: str, context: str) -> List[dict]:
    steps = []
    if isinstance(notice, str) and notice.strip():
        steps.append({"systemMessage": {"systemMessage": "", "metadata": {"user_markdown": notice.strip()}}})
    if isinstance(context, str) and context.strip():
        steps.append({"systemMessage": {"systemMessage": context.strip()}})
    return steps


def answer(event: str, data: dict, raw_payload: Optional[dict] = None, sent: Optional[dict] = None) -> dict:
    """Convert a Claude-format dispatcher answer into Antigravity's JSON contract."""
    specific = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else {}
    context = specific.get("additionalContext") if isinstance(specific.get("additionalContext"), str) else ""
    notice = data.get("systemMessage") if isinstance(data.get("systemMessage"), str) else ""
    reason = str(data.get("reason") or "")
    if event == "pre_tool_use":
        decision = specific.get("permissionDecision")
        why = str(specific.get("permissionDecisionReason") or "")
        if decision in ("deny", "ask"):
            return {"decision": decision, "reason": why or "Blocked by a hook."}
        out: Dict[str, object] = {"decision": "allow"}
        if specific.get("updatedInput") is not None:
            converted = antigravity_overwrite(specific["updatedInput"], sent or {})
            if converted is None:
                return {"decision": "deny", "reason": UNAPPLIED_EDIT}
            out["overwrite"] = converted
        return out
    if event == "post_tool_use":
        return {}
    if event == "session_start":
        steps = _inject_steps(notice, context)
        return {"injectSteps": steps} if steps else {}
    if event == "user_prompt":
        if data.get("decision") == "block":
            return {
                "injectSteps": _inject_steps(notice, "") + [{"errorMessage": {"userMessage": reason or "Blocked by a hook."}}],
                "terminationBehavior": "terminate",
            }
        steps = _inject_steps(notice, context)
        return {"injectSteps": steps} if steps else {}
    if event == "stop":
        raw = raw_payload if isinstance(raw_payload, dict) else {}
        if data.get("decision") == "block" and allow_stop_hooks(raw) and not raw.get("continuationActive"):
            return {"decision": "continue", "reason": reason or "A hook asked the agent to keep working."}
        return {"decision": "allow"}
    return {}


def stdout(event: str, out: bytes, raw_payload: Optional[dict] = None, sent: Optional[dict] = None) -> bytes:
    """The dispatcher's answer as Antigravity reads it."""
    text = out.decode("utf-8", "replace").strip()
    data = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    payload = answer(event, data if isinstance(data, dict) else {}, raw_payload, sent)
    return json.dumps(payload).encode("utf-8")


def fallback(event: str, combined, raw_payload: Optional[dict] = None) -> bytes:
    """The hooks' decision in Antigravity's fields when full response translation fails."""
    reasons = getattr(combined, "reasons", ()) or ()
    reason = "\n".join(r for r in reasons if isinstance(r, str) and r) or "A hook refused this."
    decision = getattr(combined, "decision", None)
    updated_input = getattr(combined, "updated_input", None)
    out: Dict[str, object] = {}
    if event == "pre_tool_use":
        if updated_input is not None and decision not in ("deny", "ask"):
            out = {"decision": "deny", "reason": UNAPPLIED_EDIT}
        elif decision in ("deny", "ask"):
            out = {"decision": decision, "reason": reason}
        else:
            out = {"decision": "allow"}
    elif event == "user_prompt" and decision in ("deny", "continue"):
        notices = "\n".join(u for u in (getattr(combined, "user", ()) or ()) if isinstance(u, str) and u.strip())
        out = {
            "injectSteps": _inject_steps(notices, "") + [{"errorMessage": {"userMessage": reason}}],
            "terminationBehavior": "terminate",
        }
    elif event == "stop":
        raw = raw_payload if isinstance(raw_payload, dict) else {}
        if decision in ("deny", "continue") and allow_stop_hooks(raw) and not raw.get("continuationActive"):
            out = {"decision": "continue", "reason": reason}
        else:
            out = {"decision": "allow"}
    return json.dumps(out).encode("utf-8")
