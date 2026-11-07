"""Cursor's calls to the hooks registered in Claude Code's settings.

Cursor runs those hooks, but it sends them its own input: its event names, such as
`preToolUse`, its tool names, such as `Shell`, and transcripts in its own format. It reads
answers in its own format first. The dispatcher uses this module to give each hook the
input Claude Code would have sent, and to give Cursor an answer it reads.

Only the standard library is used, because the dispatcher must start fast.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Optional

from .dispatch import APP_EVENTS

# Claude Code's event names, by the tool's event names.
CLAUDE_EVENTS = APP_EVENTS["claude"]
# Cursor's tool names that differ from Claude Code's, in hook input and in transcripts.
TOOL_NAMES = {"Shell": "Bash", "StrReplace": "Edit", "MultiStrReplace": "MultiEdit", "EditNotebook": "NotebookEdit",
              "AskQuestion": "AskUserQuestion", "ask_question": "AskUserQuestion"}
# Cursor drops a hook's answer whose added context is longer than this, counted in UTF-16
# code units as JavaScript counts a string's length. Read from Cursor 3.27.22's code.
CONTEXT_LIMIT = 10000
# Most bytes read from the end of a Cursor transcript.
MAX_TRANSCRIPT = 4_000_000
# Largest existing file whose text is read to show an edit as Claude Code's Edit.
MAX_EDITED_FILE = 1_000_000
# Translated transcripts and full context texts older than this are removed when a Cursor session starts.
KEEP_SECONDS = 7 * 24 * 3600
# A copy of a transcript not handed to a hook for this long is removed when a newer copy is written. It is
# longer than any hook's timeout, so a hook that was handed a copy can still read it.
SUPERSEDED_SECONDS = 120


def native_root(root: str, windows: bool = os.name == "nt") -> str:
    """Cursor's editor sends a Windows folder as a URI path, such as /c:/Users/name/project."""
    return root[1:] if windows and re.match(r"^/[A-Za-z]:[/\\]", root) else root


def workspace_root(payload: dict) -> Optional[str]:
    roots = payload.get("workspace_roots")
    if not isinstance(roots, list):
        return None
    for root in roots:
        if isinstance(root, str) and root and os.path.isdir(native_root(root)):
            return native_root(root)
    return None


def to_claude(event: str, payload: dict) -> dict:
    """The input Claude Code would send for this event, built from Cursor's, except its transcripts."""
    out = dict(payload)
    out["hook_event_name"] = CLAUDE_EVENTS.get(event, out.get("hook_event_name"))
    cwd = out.get("cwd")
    if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
        root = workspace_root(payload)
        if root:
            out["cwd"] = root
        else:
            out.pop("cwd", None)
    name = out.get("tool_name")
    if isinstance(name, str):
        # Cursor names an MCP tool MCP:<tool> without its server; Claude Code names it mcp__<server>__<tool>.
        out["tool_name"] = "mcp__cursor__" + name[4:] if name.startswith("MCP:") else TOOL_NAMES.get(name, name)
        tool_input = out.get("tool_input")
        if isinstance(tool_input, dict):
            out["tool_input"] = claude_tool_input(out["tool_name"], tool_input)
            if out["tool_name"] == "Bash":
                # The shell command's own folder, which first-use-review and others run git and gh in.
                folder = tool_input.get("cwd") or tool_input.get("working_directory")
                if isinstance(folder, str) and folder:
                    folder = native_root(folder)
                    base = out.get("cwd") if isinstance(out.get("cwd"), str) else None
                    if not os.path.isabs(folder) and base:
                        folder = os.path.join(base, folder)
                    if os.path.isabs(folder) and os.path.isdir(folder):
                        out["cwd"] = os.path.abspath(folder)
            if event == "pre_tool_use" and out["tool_name"] == "Write":
                as_edit = edit_of_existing_file(out["tool_input"])
                if as_edit is not None:
                    out["tool_name"], out["tool_input"] = "Edit", as_edit
                    out["cursor_tool"] = "Write"
    if event == "session_start":
        out.setdefault("source", "startup")
    if event in ("stop", "subagent_stop") and "stop_hook_active" not in out:
        count = out.get("loop_count")
        out["stop_hook_active"] = isinstance(count, int) and count > 0
    if event == "subagent_stop" and "agent_id" not in out and out.get("subagent_id"):
        out["agent_id"] = out["subagent_id"]
    return out


def with_transcripts(payload: dict, state: str) -> dict:
    """Point the transcript fields at copies in Claude Code's format. A transcript that cannot be
    copied is left out, because a hook reading Cursor's format would misread it."""
    out = dict(payload)
    for field in ("transcript_path", "agent_transcript_path"):
        path = out.get(field)
        if isinstance(path, str) and path:
            translated = translate_transcript(path, os.path.join(state, "cursor"))
            if translated:
                out[field] = translated
            else:
                out.pop(field, None)
        elif field in out:
            out.pop(field)
    return out


def claude_tool_input(tool: str, tool_input: dict) -> dict:
    """Cursor's field names for a file's path and text, as Claude Code names them."""
    out = dict(tool_input)
    if tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        if "file_path" not in out and isinstance(out.get("path"), str):
            out["file_path"] = out["path"]
        if tool == "Write" and "content" not in out and isinstance(out.get("contents"), str):
            out["content"] = out["contents"]
    return out


def edit_of_existing_file(tool_input: dict) -> Optional[dict]:
    """Cursor shows an edit to a file as a Write of the whole new text. When the file exists, return
    the Edit Claude Code would show, replacing the old text with the new, so that a hook checking
    what an edit adds does not take the file's existing text for new text."""
    path, content = tool_input.get("file_path"), tool_input.get("content")
    if not isinstance(path, str) or not isinstance(content, str) or not os.path.isfile(path):
        return None
    try:
        if os.path.getsize(path) > MAX_EDITED_FILE:
            return None
        with open(path, "rb") as handle:
            old = handle.read(MAX_EDITED_FILE + 1).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if "\r\n" in old and "\r\n" not in content:
        # The new text uses plain line breaks; compare like with like, or every line would look new.
        old = old.replace("\r\n", "\n")
    return {"file_path": path, "old_string": old, "new_string": content}


# ------------------------------------------------------------------ transcripts


def _user_text(text: str) -> str:
    """Cursor wraps a typed message in <user_query> after a <timestamp> line; keep what the user typed."""
    match = re.search(r"<user_query>\n?(.*?)\n?</user_query>", text, re.S)
    return match.group(1) if match else text


def claude_row(row: dict) -> Optional[dict]:
    """One transcript row in Claude Code's format, or None for a row Claude Code would not have."""
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    role = row.get("role") or message.get("role")
    if role not in ("user", "assistant"):
        return None
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return None
    blocks = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            blocks.append({"type": "text", "text": _user_text(block["text"]) if role == "user" else block["text"]})
        elif block.get("type") == "tool_use":
            name = TOOL_NAMES.get(str(block.get("name", "")), block.get("name"))
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            converted = dict(block, name=name, input=claude_tool_input(str(name), tool_input))
            blocks.append(converted)
        else:
            blocks.append(block)
    return {"type": role, "message": {"role": role, "content": blocks}}


def translate_transcript(source: str, folder: str) -> Optional[str]:
    """Write the end of a Cursor transcript in Claude Code's format and return that file's path.

    Each version of the transcript, by its size and time, gets its own copy, so a copy is never
    replaced while a hook reads it; Windows refuses to replace a file another program has open.
    Handing out a copy refreshes its time, and a new copy removes the older copies of the same
    transcript that no hook was handed for two minutes.
    """
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
                lines.append(json.dumps(converted))  # ASCII, so a lone surrogate cannot stop the write.
        os.makedirs(folder, exist_ok=True)
        _replace(target, ("\n".join(lines) + "\n" if lines else "").encode("utf-8"))
    except OSError:
        return target if os.path.isfile(target) else None  # Another hook may have written the same copy.
    cutoff = time.time() - SUPERSEDED_SECONDS
    for entry in os.listdir(folder):
        if entry.startswith(name + "-") and entry.endswith(".jsonl") and entry != os.path.basename(target):
            path = os.path.join(folder, entry)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass  # Open in a hook still running; a later copy or session start removes it.
    return target


def _replace(path: str, data: bytes) -> None:
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def remove_old(state: str) -> None:
    folder = os.path.join(state, "cursor")
    try:
        names = os.listdir(folder)
    except OSError:
        return
    cutoff = time.time() - KEEP_SECONDS
    for entry in names:
        path = os.path.join(folder, entry)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


# ------------------------------------------------------------------ answers


def _length(text: str) -> int:
    """The text's length as JavaScript counts it: a character outside the Basic Multilingual Plane is two."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def bounded_context(text: str, state: str, session: str, event: str) -> str:
    """Context Cursor accepts: the whole text when it fits, else its start and a note saying where the rest is."""
    if _length(text) <= CONTEXT_LIMIT:
        return text
    where = ""
    try:
        folder = os.path.join(state, "cursor")
        os.makedirs(folder, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", session)[:80] or "session"
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
        path = os.path.join(folder, f"{safe}-{event}-{digest}-context.md")
        _replace(path, text.encode("utf-8", "replace"))
        where = f" Read the whole text in {path}."
    except OSError:
        pass
    # Room for the note with the largest count it can show.
    template = "\n\n[agent-kit-sync: Cursor accepts at most {limit:,} characters of added context here, so the last {omitted:,} characters were left out.{where}]"
    room = CONTEXT_LIMIT - _length(template.format(limit=CONTEXT_LIMIT, omitted=len(text), where=where))
    kept = text
    while _length(kept) > room:
        kept = kept[: max(0, len(kept) - max(1, _length(kept) - room))]
    cut = kept.rfind("\n")
    if cut > len(kept) // 2:
        kept = kept[:cut]
    return kept + template.format(limit=CONTEXT_LIMIT, omitted=len(text) - len(kept), where=where)


def cursor_input(updated: dict, sent: dict) -> Optional[dict]:
    """A hook's changed tool input in the form Cursor's tool takes, or None when it cannot be. When an
    edit was shown to the hooks as Claude Code's Edit, Cursor's tool is still a Write of the whole new
    text, so the hook's replacement is applied to the old text."""
    if not (isinstance(updated, dict) and sent.get("tool_name") == "Edit" and sent.get("cursor_tool") == "Write"):
        return updated
    whole = sent.get("tool_input", {}).get("old_string", "")
    old, new = updated.get("old_string"), updated.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or old not in whole:
        return None
    return {"file_path": updated.get("file_path", sent["tool_input"].get("file_path")), "content": whole.replace(old, new, 1)}


UNAPPLIED_EDIT = ("A hook changed this edit in a way agent-kit-sync cannot turn into Cursor's write of the whole file, "
                  "so the edit was refused. Read the file again and make the edit again.")
NOTICE = "agent-kit-sync notice meant for the user, which Cursor has no place to show; pass it on: "


def answer(event: str, data: dict, state: str, session: str, sent: Optional[dict] = None) -> dict:
    """Add Cursor's own fields to an answer written in Claude Code's format.

    Cursor reads its own fields first, and reads Claude Code's nested fields only when a setting
    in Cursor allows it, so both are written.
    """
    specific = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else {}
    context = specific.get("additionalContext") if isinstance(specific.get("additionalContext"), str) else ""
    reason = str(data.get("reason") or "")
    if event == "pre_tool_use":
        decision = specific.get("permissionDecision")
        if decision in ("allow", "deny", "ask"):
            data["permission"] = decision
            why = str(specific.get("permissionDecisionReason") or "")
            if why and decision != "allow":
                data["agent_message"] = why
                data["user_message"] = why
        if specific.get("updatedInput") is not None:
            converted = cursor_input(specific["updatedInput"], sent or {})
            if converted is None:
                data["permission"] = "deny"
                data["agent_message"] = data["user_message"] = UNAPPLIED_EDIT
            else:
                data["updated_input"] = converted
    elif event == "user_prompt":
        if data.get("decision") == "block":
            data["continue"] = False
            data["user_message"] = reason or "Blocked by a hook."
    elif event in ("stop", "subagent_stop"):
        if data.get("decision") == "block" and reason:
            data["followup_message"] = reason
    elif event == "post_tool_use":
        if data.get("decision") == "block" and reason:
            context = reason + ("\n\n" + context if context else "")
    notice = data.get("systemMessage")
    if isinstance(notice, str) and notice.strip():
        # Claude Code shows systemMessage to the user. Cursor shows the user only the message of a refused
        # tool call or prompt, so a notice goes there when there is one, and to the agent otherwise.
        said = "\n".join(str(data.get(key) or "") for key in ("agent_message", "user_message")) + "\n" + context
        lines = [line for line in notice.splitlines() if line.strip() and not any(line.strip() in part or part in line for part in said.splitlines() if part.strip())]
        if lines:
            refused = data.get("permission") == "deny" or data.get("continue") is False
            if refused:
                data["user_message"] = (str(data["user_message"]) + "\n" if data.get("user_message") else "") + "\n".join(lines)
            else:
                context = (context + "\n\n" if context else "") + NOTICE + "\n".join(lines)
    if context and event in ("session_start", "user_prompt", "pre_tool_use", "post_tool_use"):
        data["additional_context"] = context
    return data


def stdout(event: str, out: bytes, state: str, session: str, sent: Optional[dict] = None) -> bytes:
    """The dispatcher's answer as Cursor reads it. Cursor needs JSON, and counts empty output as a failure."""
    text = out.decode("utf-8", "replace").strip()
    data = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    # The dispatcher's own answer is always JSON or empty.
    data = answer(event, data, state, session, sent) if isinstance(data, dict) else {}
    return finish(event, data, state, session)


def finish(event: str, data: dict, state: str, session: str) -> bytes:
    """Cut the added context to what Cursor accepts, and write the answer as ASCII JSON."""
    context = data.get("additional_context")
    if isinstance(context, str) and context:
        data["additional_context"] = bounded_context(context, state, session, event)
        specific = data.get("hookSpecificOutput")
        if isinstance(specific, dict) and isinstance(specific.get("additionalContext"), str):
            specific["additionalContext"] = data["additional_context"]
    return json.dumps(data).encode("utf-8")
