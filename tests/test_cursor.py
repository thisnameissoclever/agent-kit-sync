"""Cursor's calls to the hooks registered in Claude Code's settings.

The inputs here have the fields and names Cursor 3.27.22 and its command-line agent
2026.11.05 sent to a hook on 2026-11-06, read from the hooks' own logs.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from support import ROOT, remove_tree  # noqa: F401  (support puts the tool on the path)
from agent_kit_sync import cursor, dispatch, register


def cursor_payload(event, **fields):
    base = {"conversation_id": "c1", "generation_id": "c1", "model": "default", "session_id": "c1", "hook_event_name": event,
            "cursor_version": "3.27.22", "workspace_roots": [], "user_email": "person@example.com", "transcript_path": None}
    base.update(fields)
    return base


def cursor_transcript(path, rows):
    Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = str(self.tmp / "state")
        self.project = self.tmp / "project"
        self.project.mkdir()

    def tearDown(self):
        remove_tree(self.tmp)

    def test_only_cursor_version_in_the_input_marks_a_cursor_call(self):
        self.assertTrue(cursor.called_by_cursor("claude", cursor_payload("preToolUse")))
        self.assertFalse(cursor.called_by_cursor("claude", {"hook_event_name": "PreToolUse"}))
        self.assertFalse(cursor.called_by_cursor("codex", cursor_payload("preToolUse")))
        # Claude Code started from Cursor's terminal inherits CURSOR_VERSION; it is still Claude Code.
        os.environ["CURSOR_VERSION"] = "3.27.22"
        try:
            self.assertEqual(dispatch.caller("claude", {"hook_event_name": "PreToolUse"}), "claude")
        finally:
            del os.environ["CURSOR_VERSION"]
        self.assertEqual(dispatch.caller("claude", cursor_payload("stop")), "cursor")

    def test_event_and_tool_names_become_claude_codes(self):
        cases = [
            ("session_start", "sessionStart", {}, "SessionStart", None),
            ("user_prompt", "beforeSubmitPrompt", {"prompt": "hi"}, "UserPromptSubmit", None),
            ("pre_tool_use", "preToolUse", {"tool_name": "Shell", "tool_input": {"command": "ls", "cwd": "", "timeout": 30000}}, "PreToolUse", "Bash"),
            ("post_tool_use", "postToolUse", {"tool_name": "Read", "tool_input": {"file_path": "/x"}}, "PostToolUse", "Read"),
            ("stop", "stop", {"status": "completed", "loop_count": 0}, "Stop", None),
        ]
        for event, native, fields, claude_event, tool in cases:
            with self.subTest(event=event):
                out = cursor.to_claude(event, cursor_payload(native, **fields))
                self.assertEqual(out["hook_event_name"], claude_event)
                self.assertEqual(out.get("tool_name"), tool)
                self.assertEqual(out["cursor_version"], "3.27.22")  # Cursor's own fields stay.

    def test_cwd_comes_from_the_workspace_when_cursor_sends_none(self):
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "ls"}, cwd="", workspace_roots=[str(self.project)]))
        self.assertEqual(out["cwd"], str(self.project))
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "ls"}, cwd="", workspace_roots=[str(self.tmp / "missing")]))
        self.assertNotIn("cwd", out)

    def test_session_start_gets_a_source_and_stop_gets_stop_hook_active(self):
        self.assertEqual(cursor.to_claude("session_start", cursor_payload("sessionStart"))["source"], "startup")
        self.assertFalse(cursor.to_claude("stop", cursor_payload("stop", loop_count=0))["stop_hook_active"])
        self.assertTrue(cursor.to_claude("stop", cursor_payload("stop", loop_count=1))["stop_hook_active"])

    def test_a_write_to_an_existing_file_reads_as_an_edit(self):
        target = self.project / "notes.md"
        target.write_text("old text\n", encoding="utf-8")
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Write", tool_input={"file_path": str(target), "content": "old text\nnew line\n"}))
        self.assertEqual(out["tool_name"], "Edit")
        self.assertEqual(out["tool_input"], {"file_path": str(target), "old_string": "old text\n", "new_string": "old text\nnew line\n"})
        # A new file stays a Write; so does the same tool after the edit.
        new = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Write", tool_input={"file_path": str(self.project / "new.md"), "content": "x"}))
        self.assertEqual(new["tool_name"], "Write")
        after = cursor.to_claude("post_tool_use", cursor_payload("postToolUse", tool_name="Write", tool_input={"file_path": str(target), "content": "y"}))
        self.assertEqual(after["tool_name"], "Write")

    def test_transcript_becomes_claude_codes_format(self):
        source = self.tmp / "cursor.jsonl"
        cursor_transcript(source, [
            {"role": "user", "message": {"content": [{"type": "text", "text": "<timestamp>Friday</timestamp>\n<user_query>\nFix the parser.\n</user_query>"}]}},
            {"role": "assistant", "message": {"content": [{"type": "text", "text": "Fixing it."},
                                                          {"type": "tool_use", "name": "Write", "input": {"path": "/p/out.txt", "contents": "done\n"}},
                                                          {"type": "tool_use", "name": "AskQuestion", "input": {"questions": []}}]}},
            {"type": "turn_ended", "status": "success"},
        ])
        out = cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(source))), self.state)
        rows = [json.loads(line) for line in Path(out["transcript_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["type"] for row in rows], ["user", "assistant"])
        self.assertEqual(rows[0]["message"]["content"], [{"type": "text", "text": "Fix the parser."}])
        tools = [block for block in rows[1]["message"]["content"] if block["type"] == "tool_use"]
        self.assertEqual(tools[0]["name"], "Write")
        self.assertEqual(tools[0]["input"]["file_path"], "/p/out.txt")
        self.assertEqual(tools[0]["input"]["content"], "done\n")
        self.assertEqual(tools[1]["name"], "AskUserQuestion")
        # The copy is rebuilt when the transcript changes, including to the same size.
        before = Path(out["transcript_path"]).read_text(encoding="utf-8")
        cursor_transcript(source, [{"role": "user", "message": {"content": [{"type": "text", "text": "Fix the lexer."}]}}])
        os.utime(source, ns=(os.stat(source).st_atime_ns, os.stat(source).st_mtime_ns + 1_000_000))
        again = cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(source))), self.state)
        self.assertNotEqual(Path(again["transcript_path"]).read_text(encoding="utf-8"), before)
        # A transcript that cannot be read is left out rather than passed on in Cursor's format.
        gone = cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(self.tmp / "missing.jsonl"))), self.state)
        self.assertNotIn("transcript_path", gone)
        # Cursor's null transcript_path is left out too.
        self.assertNotIn("transcript_path", cursor.with_transcripts(cursor_payload("stop"), self.state))
        # Each version has its own copy, and a new copy removes the old one.
        copies = [p for p in Path(self.state, "cursor").iterdir() if p.suffix == ".jsonl"]
        self.assertEqual(len(copies), 1)

    def test_answers_carry_cursors_fields(self):
        deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "No."}}
        out = json.loads(cursor.stdout("pre_tool_use", json.dumps(deny).encode(), self.state, "s"))
        self.assertEqual((out["permission"], out["agent_message"], out["user_message"]), ("deny", "No.", "No."))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        block = {"decision": "block", "reason": "Finish the tests."}
        self.assertEqual(json.loads(cursor.stdout("stop", json.dumps(block).encode(), self.state, "s"))["followup_message"], "Finish the tests.")
        prompt = json.loads(cursor.stdout("user_prompt", json.dumps(block).encode(), self.state, "s"))
        self.assertEqual((prompt["continue"], prompt["user_message"]), (False, "Finish the tests."))
        start = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "Read the rules."}}
        self.assertEqual(json.loads(cursor.stdout("session_start", json.dumps(start).encode(), self.state, "s"))["additional_context"], "Read the rules.")
        # Cursor counts empty output as a failed hook.
        self.assertEqual(cursor.stdout("pre_tool_use", b"", self.state, "s"), b"{}")
        self.assertEqual(json.loads(cursor.stdout("session_start", b"Plain context.\n", self.state, "s")), {"additional_context": "Plain context."})

    def test_a_changed_edit_goes_back_to_cursor_as_a_whole_write(self):
        target = self.project / "notes.md"
        target.write_text("one\ntwo\n", encoding="utf-8")
        sent = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Write", tool_input={"file_path": str(target), "content": "one\ntwo\nthree\n"}))
        changed = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                          "updatedInput": {"file_path": str(target), "old_string": "one\ntwo\n", "new_string": "one\ntwo\n3\n"}}}
        out = json.loads(cursor.stdout("pre_tool_use", json.dumps(changed).encode(), self.state, "s", sent))
        self.assertEqual(out["updated_input"], {"file_path": str(target), "content": "one\ntwo\n3\n"})

    def test_long_context_is_cut_to_what_cursor_accepts_and_saved_whole(self):
        text = "\n".join(f"Line {n}: " + "x" * 60 for n in range(400))
        out = json.loads(cursor.stdout("session_start", json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}).encode(), self.state, "s/1"))
        kept = out["additional_context"]
        self.assertLessEqual(len(kept.encode("utf-16-le")) // 2, cursor.CONTEXT_LIMIT)
        self.assertTrue(kept.startswith("Line 0: "))
        self.assertIn("characters were left out", kept)
        saved = Path(kept.rsplit("Read the whole text in ", 1)[1].rstrip(".]"))
        self.assertEqual(saved.read_text(encoding="utf-8"), text)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], kept)
        # Characters outside the Basic Multilingual Plane count twice, as in JavaScript.
        wide = "\U0001F600" * 6000
        self.assertLessEqual(len(cursor.bounded_context(wide, self.state, "s", "session_start").encode("utf-16-le")) // 2, cursor.CONTEXT_LIMIT)

    def test_matchers_name_cursors_tools_too(self):
        self.assertEqual(dispatch.matcher_for("claude", ["shell"]), "^(?:Bash|PowerShell|Shell)$")
        self.assertEqual(dispatch.matcher_for("codex", ["shell"]), "^(?:Bash)$")
        events = register.needed([{"events": ["pre_tool_use"], "tools": ["shell"]}, {"events": ["pre_tool_use"], "tools": ["mcp"]}], "claude")
        self.assertEqual(events["pre_tool_use"]["matcher"], "^(?:Bash|MCP:.*|PowerShell|Shell|mcp__.*)$")
        self.assertEqual(dispatch.tool_kind("claude", "MCP:search"), "mcp")


HOOK = """
    import json, os, sys
    data = json.load(sys.stdin)
    if data.get("hook_event_name") == "PreToolUse" and data.get("tool_name") == "Bash" and "rm -rf" in data["tool_input"].get("command", ""):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "Refusing rm -rf."}}))
    elif data.get("hook_event_name") == "SessionStart":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "app=" + os.environ.get("AGENT_KIT_SYNC_APP", "")}}))
"""


class DispatcherTests(unittest.TestCase):
    """Run the dispatcher on Cursor's input with a native hook written for Claude Code."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.kit = self.tmp / "kit"
        (self.kit / "state").mkdir(parents=True)
        hook = self.tmp / "hook.py"
        hook.write_text(textwrap.dedent(HOOK), encoding="utf-8")
        index = {"hooks": [{"module": "m", "name": "h", "path": str(hook), "events": ["pre_tool_use", "session_start"], "protocol": "native",
                            "runtime": "python", "timeout": 20, "on_failure": "open", "apps": [], "order": 0}]}
        (self.kit / "state" / "index.json").write_text(json.dumps(index), encoding="utf-8")

    def tearDown(self):
        remove_tree(self.tmp)

    def run_dispatcher(self, event, payload):
        import io
        import subprocess
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from agent_kit_sync.dispatch import main; "
            "sys.exit(main(sys.argv[2], sys.argv[3], 'claude', sys.argv[4]))"
        )
        env = dict(os.environ, AGENT_KIT_SYNC_NO_BACKGROUND="1")
        env.pop("AGENT_KIT_SYNC_APP", None)
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT), str(self.kit), str(self.kit / "launcher.py"), event],
                                input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        return result.returncode, json.loads(result.stdout or b"null")

    def test_a_claude_code_hook_blocks_cursors_shell_command(self):
        code, out = self.run_dispatcher("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "rm -rf /tmp/x", "cwd": "", "timeout": 30000}))
        self.assertEqual(code, 0)
        self.assertEqual(out["permission"], "deny")
        self.assertIn("Refusing rm -rf.", out["agent_message"])

    def test_a_claude_code_hook_adds_context_at_cursors_session_start(self):
        code, out = self.run_dispatcher("session_start", cursor_payload("sessionStart", is_background_agent=False))
        self.assertEqual((code, out["additional_context"]), (0, "app=cursor"))

    def test_an_allowed_command_answers_with_json(self):
        code, out = self.run_dispatcher("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "ls"}))
        self.assertEqual((code, out), (0, {}))

    def test_claude_codes_own_calls_are_unchanged(self):
        code, out = self.run_dispatcher("pre_tool_use", {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}})
        self.assertEqual(code, 0)
        self.assertNotIn("permission", out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
