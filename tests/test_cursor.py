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
import time
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
        self.assertEqual(dispatch.caller("claude", cursor_payload("preToolUse")), "cursor")
        self.assertEqual(dispatch.caller("claude", {"hook_event_name": "PreToolUse"}), "claude")
        self.assertEqual(dispatch.caller("codex", cursor_payload("preToolUse")), "codex")
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

    def test_mcp_tools_get_claude_codes_name_form(self):
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="MCP:slack_send_message", tool_input={"text": "hi"}))
        self.assertEqual(out["tool_name"], "mcp__cursor__slack_send_message")
        self.assertEqual(dispatch.tool_kind("claude", out["tool_name"]), "mcp")

    def test_a_shell_commands_own_folder_is_the_cwd(self):
        other = self.tmp / "worktree"
        other.mkdir()
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "gh pr merge 1", "cwd": str(other)}, cwd="", workspace_roots=[str(self.project)]))
        self.assertEqual(out["cwd"], str(other))
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "ls", "cwd": str(self.tmp / "gone")}, cwd="", workspace_roots=[str(self.project)]))
        self.assertEqual(out["cwd"], str(self.project))

    def test_windows_workspace_roots_lose_the_uri_slash(self):
        self.assertEqual(cursor.native_root("/c:/Users/name/project", windows=True), "c:/Users/name/project")
        self.assertEqual(cursor.native_root("/c:/Users/name/project", windows=False), "/c:/Users/name/project")
        self.assertEqual(cursor.native_root("C:\\Users\\name", windows=True), "C:\\Users\\name")

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
        target.write_bytes(b"old text\n")
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Write", tool_input={"file_path": str(target), "content": "old text\nnew line\n"}))
        self.assertEqual(out["tool_name"], "Edit")
        self.assertEqual(out["tool_input"], {"file_path": str(target), "old_string": "old text\n", "new_string": "old text\nnew line\n"})
        # A new file stays a Write; so does the same tool after the edit.
        new = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Write", tool_input={"file_path": str(self.project / "new.md"), "content": "x"}))
        self.assertEqual(new["tool_name"], "Write")
        after = cursor.to_claude("post_tool_use", cursor_payload("postToolUse", tool_name="Write", tool_input={"file_path": str(target), "content": "y"}))
        self.assertEqual(after["tool_name"], "Write")
        # A file with Windows line breaks, written back with plain ones, shows only the real change.
        target.write_bytes(b"one\r\ntwo\r\n")
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Write", tool_input={"file_path": str(target), "content": "one\ntwo\nthree\n"}))
        self.assertEqual(out["tool_input"]["old_string"], "one\ntwo\n")
        # The same line breaks on both sides are kept as they are.
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Write", tool_input={"file_path": str(target), "content": "one\r\ntwo\r\nthree\r\n"}))
        self.assertEqual(out["tool_input"]["old_string"], "one\r\ntwo\r\n")

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
        size = os.stat(source).st_size
        cursor_transcript(source, [{"role": "user", "message": {"content": [{"type": "text", "text": "<timestamp>Friday</timestamp>\n<user_query>\nFix the lexers.\n</user_query>"}]}},
                                   {"role": "assistant", "message": {"content": [{"type": "text", "text": "Fixing it."},
                                                                                 {"type": "tool_use", "name": "Write", "input": {"path": "/p/out.txt", "contents": "done\n"}},
                                                                                 {"type": "tool_use", "name": "AskQuestion", "input": {"questions": []}}]}},
                                   {"type": "turn_ended", "status": "success"}])
        self.assertEqual(os.stat(source).st_size, size)
        os.utime(source, ns=(os.stat(source).st_atime_ns, os.stat(source).st_mtime_ns + 1_000_000))
        again = cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(source))), self.state)
        self.assertNotEqual(Path(again["transcript_path"]).read_text(encoding="utf-8"), before)
        # A transcript that cannot be read is left out rather than passed on in Cursor's format.
        gone = cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(self.tmp / "missing.jsonl"))), self.state)
        self.assertNotIn("transcript_path", gone)
        # Cursor's null transcript_path is left out too.
        self.assertNotIn("transcript_path", cursor.with_transcripts(cursor_payload("stop"), self.state))
        # Each version has its own copy; an older copy stays until no hook was handed it for two minutes.
        copies = sorted(p for p in Path(self.state, "cursor").iterdir() if p.suffix == ".jsonl")
        self.assertEqual(len(copies), 2)
        old = next(p for p in copies if str(p) != again["transcript_path"])
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        cursor_transcript(source, [{"role": "user", "message": {"content": [{"type": "text", "text": "Third."}]}}])
        cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(source))), self.state)
        self.assertFalse(old.exists())

    def test_copies_do_not_pile_up_as_the_transcript_grows(self):
        source = self.tmp / "cursor.jsonl"
        folder = Path(self.state, "cursor")
        for step in range(5):
            cursor_transcript(source, [{"role": "user", "message": {"content": [{"type": "text", "text": "x" * (step + 1)}]}}])
            cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(source))), self.state)
            for old in folder.iterdir():
                os.utime(old, (time.time() - 300, time.time() - 300))
        self.assertEqual(len([p for p in folder.iterdir() if p.suffix == ".jsonl"]), 1)

    def test_handing_out_a_copy_keeps_it(self):
        source = self.tmp / "cursor.jsonl"
        cursor_transcript(source, [{"role": "user", "message": {"content": [{"type": "text", "text": "one"}]}}])
        first = cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(source))), self.state)["transcript_path"]
        os.utime(first, (time.time() - 300, time.time() - 300))
        cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(source))), self.state)  # Hands out the same copy.
        cursor_transcript(source, [{"role": "user", "message": {"content": [{"type": "text", "text": "two"}]}}])
        cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(source))), self.state)
        self.assertTrue(Path(first).exists())

    def test_relative_and_windows_tool_folders(self):
        sub = self.project / "pkg"
        sub.mkdir()
        out = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "ls", "working_directory": "pkg"}, cwd="", workspace_roots=[str(self.project)]))
        self.assertEqual(out["cwd"], str(sub))
        self.assertEqual(cursor.native_root("/D:/work", windows=True), "D:/work")

    def test_a_lone_surrogate_does_not_stop_the_copy_or_the_answer(self):
        source = self.tmp / "cursor.jsonl"
        source.write_text('{"role": "assistant", "message": {"content": [{"type": "text", "text": "cut \\ud83d here"}]}}\n', encoding="utf-8")
        out = cursor.with_transcripts(cursor.to_claude("stop", cursor_payload("stop", transcript_path=str(source))), self.state)
        rows = [json.loads(line) for line in Path(out["transcript_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["message"]["content"][0]["text"], "cut \ud83d here")
        answer = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "x\ud83d" * 6000}}).encode()
        text = json.loads(cursor.stdout("session_start", answer, self.state, "s"))["additional_context"]
        self.assertLessEqual(cursor._length(text), cursor.CONTEXT_LIMIT)

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

    def test_a_changed_edit_goes_back_to_cursor_as_a_whole_write(self):
        target = self.project / "notes.md"
        target.write_bytes(b"one\ntwo\n")
        sent = cursor.to_claude("pre_tool_use", cursor_payload("preToolUse", tool_name="Write", tool_input={"file_path": str(target), "content": "one\ntwo\nthree\n"}))
        changed = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                          "updatedInput": {"file_path": str(target), "old_string": "one\ntwo\n", "new_string": "one\ntwo\n3\n"}}}
        out = json.loads(cursor.stdout("pre_tool_use", json.dumps(changed).encode(), self.state, "s", sent))
        self.assertEqual(out["updated_input"], {"file_path": str(target), "content": "one\ntwo\n3\n"})
        # A replacement that does not fit the old text is refused, not dropped.
        changed["hookSpecificOutput"]["updatedInput"] = {"file_path": str(target), "old_string": "absent", "new_string": "x"}
        out = json.loads(cursor.stdout("pre_tool_use", json.dumps(changed).encode(), self.state, "s", sent))
        self.assertEqual(out["permission"], "deny")
        self.assertNotIn("updated_input", out)

    def test_notices_for_the_user_reach_the_agent(self):
        notice = {"systemMessage": "Writing check: something."}
        out = json.loads(cursor.stdout("pre_tool_use", json.dumps(notice).encode(), self.state, "s"))
        self.assertIn("Writing check: something.", out["additional_context"])
        # On a refusal, the lines not already said go to the message Cursor shows the user, once.
        deny = {"systemMessage": "Writing check: No.\nagent-kit-sync: the j/g hook failed (boom) and was skipped.",
                "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "No."}}
        out = json.loads(cursor.stdout("pre_tool_use", json.dumps(deny).encode(), self.state, "s"))
        self.assertNotIn("additional_context", out)
        self.assertEqual(out["user_message"], "No.\nagent-kit-sync: the j/g hook failed (boom) and was skipped.")
        two = {"systemMessage": "Writing check: A\nWriting check: B", "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "A\nB"}}
        self.assertEqual(json.loads(cursor.stdout("pre_tool_use", json.dumps(two).encode(), self.state, "s"))["user_message"], "A\nB")
        # Cursor has no place for a notice at stop.
        stop = json.loads(cursor.stdout("stop", json.dumps({"systemMessage": "Archive check: careful."}).encode(), self.state, "s"))
        self.assertEqual(stop, {"systemMessage": "Archive check: careful."})

    def test_a_post_tool_use_block_becomes_context(self):
        block = {"decision": "block", "reason": "Check the file again."}
        self.assertEqual(json.loads(cursor.stdout("post_tool_use", json.dumps(block).encode(), self.state, "s"))["additional_context"], "Check the file again.")

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
    elif data.get("hook_event_name") == "PreToolUse" and data.get("tool_name") == "Bash" and "shutdown" in data["tool_input"].get("command", ""):
        sys.stderr.write("Refusing shutdown.")
        sys.exit(2)
    elif data.get("hook_event_name") == "Stop":
        sys.stderr.write("Finish the tests.")
        sys.exit(2)
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
        index = {"hooks": [{"module": "m", "name": "h", "path": str(hook), "events": ["pre_tool_use", "session_start", "stop"], "protocol": "native",
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

    def test_a_lone_hooks_exit_code_2_reaches_cursor_as_its_own_fields(self):
        code, out = self.run_dispatcher("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "shutdown now"}))
        self.assertEqual((code, out["permission"]), (0, "deny"))
        self.assertIn("Refusing shutdown.", out["agent_message"])
        code, out = self.run_dispatcher("stop", cursor_payload("stop", status="completed", loop_count=0))
        self.assertEqual((code, out["followup_message"]), (0, "Finish the tests."))

    def test_a_failed_translation_still_answers(self):
        # Input Cursor never sends must not crash the dispatcher.
        code, out = self.run_dispatcher("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input="not an object", workspace_roots="nope"))
        self.assertEqual(code, 0)
        self.assertNotIn("permission", out)
        # The test hook itself fails on that input; the notice reaches the agent, since Cursor shows the user nothing.
        self.assertIn("failed", out["additional_context"])

    def run_patched(self, event, payload, patch):
        import subprocess
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from agent_kit_sync import cursor, dispatch\n"
            "def broken(*args, **kwargs): raise UnicodeEncodeError('utf-8', '', 0, 1, 'lone surrogate')\n"
            + patch + "\n"
            "sys.exit(dispatch.main(sys.argv[2], sys.argv[3], 'claude', sys.argv[4]))"
        )
        env = dict(os.environ, AGENT_KIT_SYNC_NO_BACKGROUND="1")
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT), str(self.kit), str(self.kit / "launcher.py"), event],
                                input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_input_translation_that_raises_still_runs_the_hooks(self):
        payload = cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "rm -rf /tmp/x"}, transcript_path="/nowhere.jsonl")
        for patch in ("cursor.to_claude = broken", "cursor.with_transcripts = broken"):
            with self.subTest(patch=patch):
                out = self.run_patched("pre_tool_use", payload, patch)
                self.assertEqual(out["permission"], "deny")

    def test_a_malformed_hook_answer_does_not_stop_the_dispatcher(self):
        hook = self.tmp / "odd.py"
        hook.write_text("print('{\"hookSpecificOutput\": \"x\"}')\n", encoding="utf-8")
        index = json.loads((self.kit / "state" / "index.json").read_text(encoding="utf-8"))
        index["hooks"].append(dict(index["hooks"][0], name="odd", path=str(hook)))
        (self.kit / "state" / "index.json").write_text(json.dumps(index), encoding="utf-8")
        code, out = self.run_dispatcher("pre_tool_use", cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "rm -rf /tmp/x"}))
        self.assertEqual((code, out["permission"]), (0, "deny"))

    def test_alerts_reach_cursors_agent_but_wait_at_stop(self):
        alert = {"id": "a1", "message": "Something failed.", "command": "agent-kit-sync status"}
        (self.kit / "state" / "alerts.json").write_text(json.dumps([alert]), encoding="utf-8")
        _code, out = self.run_dispatcher("stop", cursor_payload("stop", session_id="s9", status="completed", loop_count=1))
        self.assertNotIn("Something failed.", json.dumps(out))
        _code, out = self.run_dispatcher("pre_tool_use", cursor_payload("preToolUse", session_id="s9", tool_name="Shell", tool_input={"command": "ls"}))
        self.assertIn("Something failed.", out["additional_context"])
        _code, out = self.run_dispatcher("pre_tool_use", cursor_payload("preToolUse", session_id="s9", tool_name="Shell", tool_input={"command": "ls"}))
        self.assertNotIn("additional_context", out)

    def test_a_translation_that_raises_keeps_the_hooks_decision(self):
        import subprocess
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from agent_kit_sync import cursor, dispatch\n"
            "def broken(*args, **kwargs): raise UnicodeEncodeError('utf-8', '', 0, 1, 'lone surrogate')\n"
            "cursor.stdout = broken\n"
            "sys.exit(dispatch.main(sys.argv[2], sys.argv[3], 'claude', 'pre_tool_use'))"
        )
        env = dict(os.environ, AGENT_KIT_SYNC_NO_BACKGROUND="1")
        payload = cursor_payload("preToolUse", tool_name="Shell", tool_input={"command": "rm -rf /tmp/x"})
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT), str(self.kit), str(self.kit / "launcher.py")],
                                input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["permission"], "deny")
        self.assertIn("Refusing rm -rf.", out["agent_message"])

    def test_claude_codes_own_calls_are_unchanged(self):
        code, out = self.run_dispatcher("pre_tool_use", {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}})
        self.assertEqual(code, 0)
        self.assertNotIn("permission", out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class RulesPointerTests(unittest.TestCase):
    """Cursor loads <user folder>/.cursor/rules only for a workspace inside the user folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / ".cursor" / "rules").mkdir(parents=True)
        (self.home / ".cursor" / "rules" / "agent-kit-sync--writing--rules.mdc").write_text("---\nalwaysApply: true\n---\n\nRule.\n", encoding="utf-8")
        (self.home / ".cursor" / "rules" / "mine.mdc").write_text("Not ours.\n", encoding="utf-8")
        self.inside = self.home / "project"
        self.inside.mkdir()
        self.outside = self.tmp / "elsewhere"
        self.outside.mkdir()
        self.saved = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.home)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        remove_tree(self.tmp)

    def start(self, root, context=None):
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}} if context else {}
        sent = cursor.to_claude("session_start", cursor_payload("sessionStart", workspace_roots=[str(root)]))
        return json.loads(cursor.stdout("session_start", json.dumps(out).encode(), str(self.tmp / "state"), "s", sent))

    def test_a_workspace_outside_the_user_folder_is_told_to_read_the_rules(self):
        text = self.start(self.outside, "Other context.")["additional_context"]
        self.assertIn(str(self.home / ".cursor" / "rules" / "agent-kit-sync--writing--rules.mdc"), text)
        self.assertNotIn("mine.mdc", text)
        self.assertTrue(text.endswith("Other context."))

    def test_a_workspace_inside_the_user_folder_gets_nothing_extra(self):
        self.assertEqual(self.start(self.inside), {})
        self.assertEqual(self.start(self.inside, "Other context.")["additional_context"], "Other context.")

    def test_a_workspace_with_one_root_inside_gets_nothing(self):
        out = {}
        sent = cursor.to_claude("session_start", cursor_payload("sessionStart", workspace_roots=[str(self.outside), str(self.inside)]))
        self.assertEqual(json.loads(cursor.stdout("session_start", json.dumps(out).encode(), str(self.tmp / "state"), "s", sent)), {})

    def test_a_link_into_the_user_folder_counts_as_inside(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symbolic links")
        link = self.home / "linked"
        try:
            os.symlink(self.outside, link, target_is_directory=True)
        except OSError:
            self.skipTest("cannot create symbolic links here")
        self.assertEqual(self.start(link), {})

    def test_no_rule_files_means_no_pointer(self):
        remove_tree(self.home / ".cursor" / "rules")
        self.assertEqual(self.start(self.outside), {})


if __name__ == "__main__":
    unittest.main()
