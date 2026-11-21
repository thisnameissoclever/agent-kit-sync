"""Tests for Antigravity sync, plugin lifecycle, hook registration, and hook dispatch."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Tuple
from unittest import mock

from support import Machine, TwoMachines, as_home, remove_tree

from agent_kit_sync import antigravity, cli, commands, dispatch, processes, register, setup
from agent_kit_sync.model import Machine as MachineModel
from agent_kit_sync.paths import ANTIGRAVITY_PLUGIN_NAME, Layout, antigravity_plugin_dir, os_name
from agent_kit_sync.state import Alerts, Held


def _write_test_launcher(layout: Layout) -> None:
    root = str(Path(__file__).resolve().parents[1])
    layout.launcher.parent.mkdir(parents=True, exist_ok=True)
    layout.launcher.write_text(
        f"import sys\nsys.path.insert(0, {root!r})\nfrom agent_kit_sync.cli import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )


def _antigravity_only_config(repo: TwoMachines) -> None:
    repo.seed_files({
        "kit.toml": """
            format = 1

            [[modules]]
            name = "writing"
        """,
        ".gitattributes": "* text=auto eol=lf\n",
        "modules/writing/module.toml": """
            apps = ["claude", "codex", "antigravity"]
            description = "Writing rules"

            [[sections]]
            name = "plain"
            file = "sections/plain.md"

            [[skills]]
            name = "review"
            dir = "skills/review"

            [[agents]]
            name = "helper"
            file = "agents/helper.md"

            [[hooks]]
            name = "guard"
            file = "hooks/guard.py"
            events = ["pre_tool_use", "post_tool_use", "user_prompt", "stop", "session_start"]
            tools = ["edit", "shell"]
            apps = ["claude", "codex", "antigravity"]
            tests = ["tests/test_guard.py"]
        """,
        "modules/writing/sections/plain.md": """
            Write plainly.
            Keep projects in {{kit.projects}}.
        """,
        "modules/writing/skills/review/SKILL.md": """
            ---
            name: review
            description: Review prose
            ---
            Read the draft and report problems.
        """,
        "modules/writing/agents/helper.md": """
            ---
            name: helper
            description: Helps
            claude.tools: Read, Grep
            codex.sandbox_mode: read-only
            antigravity.subagent: true
            antigravity.mainAgent: false
            antigravity.tools: [read_file, grep_search]
            ---
            Help with the task.
        """,
        "modules/writing/hooks/guard.py": """
            import json, sys
            data = json.load(sys.stdin)
            event = data.get("hook_event_name")
            if event == "PreToolUse":
                tool = data.get("tool_name")
                ti = data.get("tool_input") or {}
                if tool == "Bash" and "blockme" in ti.get("command", ""):
                    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "blocked by guard"}}))
                elif tool == "Edit" and ti.get("old_string") == "OLD":
                    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"file_path": ti.get("file_path", ""), "old_string": "OLD", "new_string": "NEW"}}}))
            elif event == "UserPromptSubmit" and "badprompt" in data.get("prompt", ""):
                print(json.dumps({"decision": "block", "reason": "bad prompt"}))
        """,
        "modules/writing/tests/test_guard.py": """
            import sys
            sys.exit(0)
        """,
    })


class AntigravitySyncTests(TwoMachines):
    def test_sync_installs_plugin_leaves_gemini_md_and_carries_edits_back(self):
        _antigravity_only_config(self)
        m1 = self.machine("m1", apps=("claude", "codex", "antigravity"))
        m2 = self.machine("m2", apps=("claude", "codex"))
        gemini_md = m1.path(".gemini", "GEMINI.md")
        user_gemini = "# My Own Antigravity Rules\nLeave this untouched.\n"
        m1.write(gemini_md, user_gemini)

        _eng, report = m1.sync()
        self.assertEqual(report.errors, [])
        plugin_dir = antigravity_plugin_dir(m1.home / ".gemini")
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], ANTIGRAVITY_PLUGIN_NAME)
        self.assertEqual(gemini_md.read_text(encoding="utf-8"), user_gemini)

        # Skills, agents (with antigravity.* frontmatter flattened), and rules land in plugin_dir.
        skill_file = plugin_dir / "skills" / "review" / "SKILL.md"
        agent_file = plugin_dir / "agents" / "helper.md"
        rule_file = plugin_dir / "rules" / "agent-kit-sync--writing--plain.md"
        self.assertTrue(skill_file.is_file())
        self.assertTrue(agent_file.is_file())
        self.assertTrue(rule_file.is_file())

        agent_text = agent_file.read_text(encoding="utf-8")
        self.assertIn("subagent: true", agent_text)
        self.assertIn("tools: [read_file, grep_search]", agent_text)
        self.assertNotIn("claude.tools:", agent_text)
        self.assertNotIn("codex.sandbox_mode:", agent_text)

        rule_text = rule_file.read_text(encoding="utf-8")
        self.assertTrue(rule_text.startswith("---\ntrigger: always_on\n---\n\n"))
        self.assertIn("Write plainly.", rule_text)

        # Edit skill and agent inside Antigravity plugin, plus edit output-only rule file.
        m1.write(skill_file, skill_file.read_text(encoding="utf-8") + "\nCheck punctuation too.\n")
        m1.write(agent_file, agent_text + "\nVerify edge cases.\n")
        m1.write(rule_file, rule_text + "\nLocal edit that must not carry back.\n")

        eng2, report2 = m1.sync()
        self.assertEqual(report2.errors, [])
        self.assertEqual(m2.sync()[1].errors, [])
        self.assertIn("Check punctuation too.", m2.read(".claude", "skills", "review", "SKILL.md"))
        self.assertIn("Verify edge cases.", m2.read(".claude", "agents", "helper.md"))
        # Rules are output-only: backup saved, rule restored, alert raised, Gemini MD untouched.
        self.assertEqual(rule_file.read_text(encoding="utf-8"), rule_text)
        self.assertEqual(gemini_md.read_text(encoding="utf-8"), user_gemini)
        self.assertNotIn("Local edit that must not carry back.", eng2.layout.config.joinpath("modules", "writing", "sections", "plain.md").read_text(encoding="utf-8"))
        self.assertIn("antigravity-rule-edited", [a["key"] for a in Alerts(m1.layout).open()])
        backups = list((m1.layout.state / "backups" / "antigravity-rules").iterdir())
        self.assertTrue(backups)

    def test_deletion_guard_and_uninstall_and_app_removal_lifecycle(self):
        _antigravity_only_config(self)
        policy = {"new_items": "apply", "code_changes": "apply", "other_changes": "apply", "skills": "apply", "outgoing": "push"}
        m1 = self.machine("m1", apps=("claude", "antigravity"), policy=policy)
        m1.write(m1.path(".gemini", "GEMINI.md"), "# User rules\n")
        self.assertEqual(m1.sync()[1].errors, [])

        plugin_dir = antigravity_plugin_dir(m1.home / ".gemini")
        # Vanishing the whole plugin folder triggers the deletion guard.
        remove_tree(plugin_dir)
        _eng, guarded = m1.sync()
        self.assertTrue(guarded.held)
        self.assertTrue(any(h.get("key", "").startswith("deletion:") and h.get("kind") == "deletion" for h in Held(m1.layout).all()))
        # Restore by running sync with deletion resolved / restored, or re-create clean state in m2.
        m2 = self.machine("m2", apps=("claude", "antigravity"), policy=policy)
        m2.write(m2.path(".gemini", "GEMINI.md"), "# User rules\n")
        with m2.env():
            commands.write_launcher(m2.layout)
            self.assertEqual(cli.main(["sync"]), 0)

        p2 = antigravity_plugin_dir(m2.home / ".gemini")
        hooks_path = p2 / "hooks.json"
        self.assertTrue(hooks_path.is_file())
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        data["foreign-hook"] = {"SessionStart": [{"type": "command", "command": "echo hi"}]}
        hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        # Deleting a single skill while plugin_dir still exists carries back instead of guarding.
        remove_tree(p2 / "skills" / "review")
        with m2.env():
            self.assertEqual(cli.main(["sync"]), 0)
        self.assertEqual(Held(m2.layout).all(), [])
        self.assertFalse((m2.layout.config / "modules" / "writing" / "skills" / "review").exists())

        # Dropping "antigravity" from machine.apps cleans managed entries while preserving foreign hooks and GEMINI.md.
        cfg = m2.machine_data()
        cfg["apps"] = ["claude"]
        m2.write_machine(cfg)
        with m2.env():
            self.assertEqual(cli.main(["sync"]), 0)
        self.assertFalse((p2 / "plugin.json").exists())
        self.assertFalse((p2 / "rules").exists())
        self.assertFalse((p2 / "agents").exists())
        self.assertEqual((m2.home / ".gemini" / "GEMINI.md").read_text(encoding="utf-8"), "# User rules\n")
        kept_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertNotIn(ANTIGRAVITY_PLUGIN_NAME, kept_hooks)
        self.assertIn("foreign-hook", kept_hooks)

        # Re-enable, remove foreign hook, then test cmd_uninstall prunes the entire empty plugin dir.
        hooks_path.unlink()
        cfg["apps"] = ["claude", "antigravity"]
        m2.write_machine(cfg)
        with m2.env():
            self.assertEqual(cli.main(["sync"]), 0)
            self.assertTrue((p2 / "plugin.json").is_file())
            self.assertTrue(hooks_path.is_file())
            with contextlib.redirect_stdout(io.StringIO()):
                rc = commands.cmd_uninstall(SimpleNamespace(yes=True), m2.layout)
            self.assertEqual(rc, 0)
        self.assertFalse(hooks_path.exists())
        self.assertFalse((p2 / "plugin.json").exists())
        self.assertFalse(p2.exists())
        self.assertEqual((m2.home / ".gemini" / "GEMINI.md").read_text(encoding="utf-8"), "# User rules\n")


class AntigravityRegistrationAndDispatchTests(unittest.TestCase):
    def _layout_and_machine(self, home: Path, apps=("antigravity",)) -> Tuple[Layout, MachineModel]:
        layout = Layout(home / ".agent-kit-sync")
        layout.state.mkdir(parents=True)
        _write_test_launcher(layout)
        homes = {app: home / (".gemini" if app == "antigravity" else f".{app}") for app in apps}
        for folder in homes.values():
            folder.mkdir(parents=True, exist_ok=True)
        machine = MachineModel(
            name="testbox",
            apps=list(apps),
            app_homes=homes,
            tags=[],
            python=sys.executable,
            projects=home / "Projects",
            worktrees=home / "Projects" / "_worktrees",
            git_identity={},
            policy={},
            overrides={},
        )
        return layout, machine

    def test_register_writes_flat_and_grouped_events_and_cleans_up_idempotently(self):
        with tempfile.TemporaryDirectory(prefix="aks-ag-reg-") as folder:
            home = Path(folder)
            layout, machine = self._layout_and_machine(home)
            hooks = [
                {"name": "g", "events": ["pre_tool_use", "post_tool_use", "user_prompt", "stop", "session_start"], "tools": ["edit"], "apps": ["antigravity"], "timeout": 15},
            ]
            changed = register.register(layout, machine, hooks)
            self.assertTrue(changed["antigravity"])
            self.assertFalse(register.register(layout, machine, hooks)["antigravity"])

            hooks_path = antigravity_plugin_dir(home / ".gemini") / "hooks.json"
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertNotIn("hooks", data)
            entry = data[ANTIGRAVITY_PLUGIN_NAME]
            # Flat events
            for flat_event in ("SessionStart", "PreInvocation", "Stop"):
                self.assertIsInstance(entry[flat_event], list)
                self.assertEqual(len(entry[flat_event]), 1)
                self.assertNotIn("hooks", entry[flat_event][0])
                self.assertNotIn("commandWindows", entry[flat_event][0])
            # Grouped events; edit matcher includes write_file/write_to_file too
            for grouped in ("PreToolUse", "PostToolUse"):
                self.assertIsInstance(entry[grouped], list)
                group = entry[grouped][0]
                self.assertIn("hooks", group)
                self.assertIn("edit_file", group["matcher"])
                self.assertIn("write_file", group["matcher"])
                self.assertIn("write_to_file", group["matcher"])
                self.assertNotIn("Write|", group["matcher"])

            # Add a foreign top-level hook entry, a foreign handler inside the PreToolUse group, null hooks group, and stray duplicate.
            data["user-hook"] = {"Stop": [{"type": "command", "command": "echo user"}]}
            entry["SessionStart"].append(dict(entry["SessionStart"][0]))
            entry["PreToolUse"][0]["hooks"].append({"type": "command", "command": "echo other-in-group"})
            entry["PreToolUse"].append({"matcher": "*", "hooks": None})
            hooks_path.write_text(json.dumps(data), encoding="utf-8")
            self.assertTrue(register.register(layout, machine, hooks)["antigravity"])
            updated = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual(len(updated[ANTIGRAVITY_PLUGIN_NAME]["SessionStart"]), 1)
            pre_groups = updated[ANTIGRAVITY_PLUGIN_NAME]["PreToolUse"]
            self.assertTrue(any(isinstance(g.get("hooks"), list) and any(h.get("command") == "echo other-in-group" for h in g["hooks"]) for g in pre_groups))
            self.assertIn("user-hook", updated)

            # Unregister keeps foreign entries
            self.assertTrue(register.unregister(layout, machine)["antigravity"])
            remaining = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertIn("PreToolUse", remaining[ANTIGRAVITY_PLUGIN_NAME])
            self.assertIn("user-hook", remaining)

            # Without foreign entries, unregister unlinks hooks.json
            entry["PreToolUse"] = [
                g for g in entry["PreToolUse"]
                if isinstance(g.get("hooks"), list)
            ]
            entry["PreToolUse"][0]["hooks"] = [h for h in entry["PreToolUse"][0]["hooks"] if h.get("command") != "echo other-in-group"]
            hooks_path.write_text(json.dumps({ANTIGRAVITY_PLUGIN_NAME: entry}), encoding="utf-8")
            self.assertTrue(register.unregister(layout, machine)["antigravity"])
            self.assertFalse(hooks_path.exists())

    def test_setup_scan_remove_and_restore_registrations_for_antigravity(self):
        with tempfile.TemporaryDirectory(prefix="aks-ag-setup-") as folder:
            home = Path(folder)
            layout = Layout(home / ".agent-kit-sync")
            layout.state.mkdir(parents=True)
            (home / ".gemini" / "antigravity").mkdir(parents=True)
            cfg = home / ".gemini" / "config"
            (cfg / "agents").mkdir(parents=True)
            (cfg / "skills" / "local-skill").mkdir(parents=True)
            (cfg / "skills" / "local-skill" / "SKILL.md").write_text("---\nname: local-skill\ndescription: d\n---\n", encoding="utf-8")
            (cfg / "agents" / "local-agent.md").write_text("---\nname: local-agent\n---\n", encoding="utf-8")
            (home / ".gemini" / "GEMINI.md").write_text("# User Rules\nBody\n", encoding="utf-8")
            ag_hooks = cfg / "hooks.json"
            ag_hooks.write_text(json.dumps({
                "custom-hooks": {
                    "SessionStart": [{"type": "command", "command": "python start.py"}],
                    "PreToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": "python pre.py"}]}],
                }
            }, indent=2), encoding="utf-8")
            with as_home(home):
                scanned = setup.scan_machine()
            self.assertIn("antigravity", scanned["apps"])
            self.assertTrue(any(s["name"] == "local-skill" and s["app"] == "antigravity" for s in scanned["skills"]))
            self.assertTrue(any(a["name"] == "local-agent" and a["app"] == "antigravity" for a in scanned["agents"]))
            self.assertTrue(any(i["path"].endswith("GEMINI.md") for i in scanned["instructions"]))
            found_hooks = [h for h in scanned["hooks"] if h["app"] == "antigravity"]
            self.assertEqual(len(found_hooks), 2)

            removed = setup.remove_registrations(layout, found_hooks)
            self.assertEqual(len(removed), 2)
            self.assertEqual(json.loads(ag_hooks.read_text(encoding="utf-8")), {})

            restored = setup.restore_registrations(layout)
            self.assertEqual(len(restored), 2)
            restored_data = json.loads(ag_hooks.read_text(encoding="utf-8"))
            self.assertEqual(restored_data["custom-hooks"]["SessionStart"][0]["command"], "python start.py")
            self.assertEqual(restored_data["custom-hooks"]["PreToolUse"][0]["hooks"][0]["command"], "python pre.py")

    def test_payload_and_response_translation_and_dedup_and_transcripts(self):
        with tempfile.TemporaryDirectory(prefix="aks-ag-trans-") as folder:
            root = Path(folder)
            state = str(root / "state")
            existing = root / "note.txt"
            existing.write_text("hello OLD world", encoding="utf-8")
            transcript = root / "transcript.jsonl"
            transcript.write_text(
                "\n".join([
                    json.dumps({"type": "USER_INPUT", "step_index": 1, "created_at": "2026-01-01T00:00:00Z", "content": "<USER_REQUEST>\nFix bug\n</USER_REQUEST>"}),
                    json.dumps({
                        "type": "PLANNER_RESPONSE",
                        "step_index": 2,
                        "created_at": "2026-01-01T00:00:01Z",
                        "content": "Done fixing.",
                        "tool_calls": [{"name": "run_command", "args": {"CommandLine": json.dumps("pytest")}}],
                    }),
                ]) + "\n",
                encoding="utf-8",
            )

            # Tool translations
            bash = antigravity.to_claude("pre_tool_use", {
                "conversationId": "c1", "workspacePaths": [str(root)], "transcriptPath": str(transcript),
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
            })
            self.assertEqual(bash["tool_name"], "Bash")
            self.assertEqual(bash["tool_input"]["command"], "git status")

            mcp_call = antigravity.to_claude("pre_tool_use", {
                "conversationId": "c1",
                "toolCall": {"name": "call_mcp_tool", "args": {"ServerName": "gh", "ToolName": "issue", "Arguments": '{"id": 42}'}},
            })
            self.assertEqual(mcp_call["tool_name"], "mcp__gh__issue")
            self.assertEqual(mcp_call["tool_input"], {"id": 42})

            mcp_prefixed = antigravity.to_claude("pre_tool_use", {"toolCall": {"name": "mcp_gh_list", "args": {"q": "x"}}})
            self.assertEqual(mcp_prefixed["tool_name"], "mcp__gh_list")

            ask = antigravity.to_claude("pre_tool_use", {"toolCall": {"name": "ask_question", "args": {"questions": []}}})
            self.assertEqual(ask["tool_name"], "AskUserQuestion")

            # Existing file write converts to Edit, overwrite converts back to Content / CodeContent
            overwrite_sent = antigravity.to_claude("pre_tool_use", {
                "conversationId": "c1",
                "toolCall": {"name": "write_file", "args": {"Path": str(existing), "Content": "hello NEW world", "Overwrite": True}},
            })
            self.assertEqual(overwrite_sent["tool_name"], "Edit")
            self.assertTrue(overwrite_sent.get("antigravity_converted_write"))
            reply = antigravity.answer("pre_tool_use", {
                "hookSpecificOutput": {"permissionDecision": "allow", "updatedInput": {"file_path": str(existing), "old_string": "OLD", "new_string": "BETTER"}},
            }, sent=overwrite_sent)
            self.assertEqual(reply, {"decision": "allow", "overwrite": {"Content": "hello BETTER world"}})

            # Unapplied edit falls back to deny with UNAPPLIED_EDIT (missing match or changed file_path)
            bad_overwrite = antigravity.answer("pre_tool_use", {
                "hookSpecificOutput": {"permissionDecision": "allow", "updatedInput": {"file_path": str(existing), "old_string": "MISSING", "new_string": "X"}},
            }, sent=overwrite_sent)
            self.assertEqual(bad_overwrite["decision"], "deny")
            self.assertIn("whole file", bad_overwrite["reason"])
            moved_overwrite = antigravity.answer("pre_tool_use", {
                "hookSpecificOutput": {"permissionDecision": "allow", "updatedInput": {"file_path": str(root / "other.txt"), "old_string": "OLD", "new_string": "NEW"}},
            }, sent=overwrite_sent)
            self.assertEqual(moved_overwrite["decision"], "deny")

            # multi_replace_file_content translates ReplacementChunks back while keeping line metadata
            multi_sent = antigravity.to_claude("pre_tool_use", {
                "conversationId": "c1",
                "toolCall": {
                    "name": "multi_replace_file_content",
                    "args": {
                        "TargetFile": str(existing),
                        "ReplacementChunks": [{"StartLine": 1, "EndLine": 1, "TargetContent": "OLD", "ReplacementContent": "NEW", "AllowMultiple": False}],
                    },
                },
            })
            multi_reply = antigravity.answer("pre_tool_use", {
                "hookSpecificOutput": {"permissionDecision": "allow", "updatedInput": {"file_path": str(existing), "edits": [{"old_string": "OLD", "new_string": "SAFE"}]}},
            }, sent=multi_sent)
            self.assertEqual(multi_reply, {
                "decision": "allow",
                "overwrite": {
                    "TargetFile": str(existing),
                    "ReplacementChunks": [{"StartLine": 1, "EndLine": 1, "TargetContent": "OLD", "ReplacementContent": "SAFE", "AllowMultiple": False}],
                },
            })
            multi_bad = antigravity.answer("pre_tool_use", {
                "hookSpecificOutput": {"permissionDecision": "allow", "updatedInput": {"edits": []}},
            }, sent=multi_sent)
            self.assertEqual(multi_bad["decision"], "deny")

            # Unmapped tool with Claude keys is refused cleanly instead of silently ignored
            unknown_sent = antigravity.to_claude("pre_tool_use", {"toolCall": {"name": "grep_search", "args": {"Query": "x"}}})
            self.assertEqual(antigravity.answer("pre_tool_use", {
                "hookSpecificOutput": {"permissionDecision": "allow", "updatedInput": {"pattern": "y"}},
            }, sent=unknown_sent)["decision"], "deny")

            # PreInvocation dedup
            turn1 = {"conversationId": "c1", "initialNumSteps": 0, "invocationNum": 1, "lastUserInput": "<USER_REQUEST>\nhi\n</USER_REQUEST>"}
            self.assertTrue(antigravity.should_run_user_prompt(turn1, state))
            self.assertFalse(antigravity.should_run_user_prompt(turn1, state))
            self.assertFalse(antigravity.should_run_user_prompt(dict(turn1, invocationNum=2), state))
            self.assertTrue(antigravity.should_run_user_prompt({"conversationId": "c1", "initialNumSteps": 4, "invocationNum": 1, "lastUserInput": "next"}, state))

            # Stop terminationReason gating
            self.assertTrue(antigravity.allow_stop_hooks({"terminationReason": "NO_TOOL_CALL"}))
            self.assertFalse(antigravity.allow_stop_hooks({"terminationReason": "USER_CANCELLED"}))
            self.assertEqual(antigravity.answer("stop", {"decision": "block", "reason": "fix"}, {"terminationReason": "USER_CANCELLED"}), {"decision": "allow"})
            self.assertEqual(antigravity.answer("stop", {"decision": "block", "reason": "fix"}, {"terminationReason": "NO_TOOL_CALL"}), {"decision": "continue", "reason": "fix"})

            # Transcript translation & last_assistant_message
            enriched = antigravity.with_transcripts(bash, state, event="stop")
            self.assertEqual(enriched["last_assistant_message"], "Done fixing.")
            rows = [json.loads(line) for line in Path(enriched["transcript_path"]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["message"]["content"][0]["text"], "Fix bug")
            self.assertEqual(rows[1]["message"]["content"][1]["name"], "Bash")
            self.assertEqual(rows[1]["message"]["content"][1]["input"]["command"], "pytest")

    def test_dispatcher_end_to_end_and_windows_shell_and_doctor_status(self):
        prefix = "aks ag space " if os_name() == "windows" else "aks-ag-disp-"
        with tempfile.TemporaryDirectory(prefix=prefix) as folder:
            home = Path(folder)
            layout, machine = self._layout_and_machine(home)
            (home / ".cursor").mkdir()  # Verify Antigravity keeps native "ask" even when ~/.cursor exists.
            workspace = home / "ws"
            workspace.mkdir()
            target = workspace / "demo.txt"
            target.write_text("OLD", encoding="utf-8")

            hook_script = layout.config / "modules" / "m" / "hooks" / "h.py"
            hook_script.parent.mkdir(parents=True)
            hook_script.write_text(textwrap.dedent("""
                import json, os, sys
                data = json.load(sys.stdin)
                event = data.get("hook_event_name")
                if event == "PreToolUse":
                    tool = data.get("tool_name")
                    ti = data.get("tool_input") or {}
                    if tool == "Bash" and "ask" in ti.get("command", ""):
                        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "confirm ask"}}))
                    elif tool == "Bash" and "exit2" in ti.get("command", ""):
                        sys.stderr.write("exit2 refused\\n")
                        sys.exit(2)
                    elif tool == "Edit":
                        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"file_path": ti["file_path"], "old_string": "OLD", "new_string": "NEW"}}}))
                elif event == "UserPromptSubmit":
                    if "blockprompt" in data.get("prompt", ""):
                        print(json.dumps({"decision": "block", "reason": "blocked prompt"}))
                    else:
                        print("prompt context")
                elif event == "Stop":
                    with open(os.path.join(data["cwd"], "stop_ran.txt"), "w", encoding="utf-8") as f:
                        f.write(data.get("last_assistant_message", ""))
                    print(json.dumps({"decision": "block", "reason": "keep going"}))
            """).lstrip("\n"), encoding="utf-8")

            kit_script = layout.config / "modules" / "m" / "hooks" / "k.py"
            kit_script.write_text(textwrap.dedent("""
                import json, sys
                data = json.load(sys.stdin)
                with open(sys.argv[1], "a", encoding="utf-8") as f:
                    f.write(json.dumps({"tool": data.get("tool"), "app": data.get("app")}) + "\\n")
                print(json.dumps({"decision": "allow"}))
            """).lstrip("\n"), encoding="utf-8")
            kit_log = workspace / "kit_log.jsonl"

            fail_script = layout.config / "modules" / "m" / "hooks" / "fail.py"
            fail_script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")

            hooks = [
                {"module": "m", "name": "h", "path": str(hook_script), "events": ["session_start", "user_prompt", "pre_tool_use", "post_tool_use", "stop"], "tools": ["shell", "edit", "read"], "protocol": "native", "args": [], "runtime": "python", "timeout": 10, "on_failure": "open", "apps": ["antigravity"], "order": 0},
                {"module": "m", "name": "k", "path": str(kit_script), "events": ["pre_tool_use"], "tools": ["shell", "search"], "protocol": "kit", "args": [str(kit_log)], "runtime": "python", "timeout": 10, "on_failure": "open", "apps": ["antigravity"], "order": 1},
                {"module": "m", "name": "closed", "path": str(fail_script), "events": ["pre_tool_use"], "tools": ["web"], "protocol": "native", "args": [], "runtime": "python", "timeout": 10, "on_failure": "closed", "apps": ["antigravity"], "order": 2},
            ]
            (layout.state / "index.json").write_text(json.dumps({"hooks": hooks, "copies": {}, "blocks": {}, "settings": {}, "items": {}}), encoding="utf-8")
            antigravity_rules = __import__("agent_kit_sync.antigravity_rules", fromlist=["ensure_manifest"])
            plugin_dir = antigravity_plugin_dir(home / ".gemini")
            self.assertIsNone(antigravity_rules.ensure_manifest(plugin_dir))
            self.assertTrue(register.register(layout, machine, hooks)["antigravity"])
            layout.machine_file.write_text(
                f'name = "testbox"\napps = ["antigravity"]\npython = {json.dumps(sys.executable)}\n',
                encoding="utf-8",
            )

            env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_KIT_SYNC", "ANTIGRAVITY_"))}
            env.update({"HOME": str(home), "USERPROFILE": str(home), "AGENT_KIT_SYNC_NO_BACKGROUND": "1", "AGENT_KIT_SYNC_TEST": "1"})

            # 1. Blocked UserPromptSubmit preserves open alerts in injectSteps AND does not suppress re-submission
            alerts = Alerts(layout)
            alerts.raise_("test-alert", "hello alert", "agent-kit-sync status")
            blocked_prompt = {"conversationId": "c1", "initialNumSteps": 0, "invocationNum": 1, "lastUserInput": "<USER_REQUEST>\nblockprompt\n</USER_REQUEST>", "cwd": str(workspace)}
            pb1 = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "user_prompt"],
                input=json.dumps(blocked_prompt).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(pb1.returncode, 0, pb1.stderr)
            out_b1 = json.loads(pb1.stdout.decode("utf-8"))
            self.assertEqual(out_b1.get("terminationBehavior"), "terminate")
            self.assertTrue(any("hello alert" in json.dumps(step) for step in out_b1.get("injectSteps", [])))
            pb2 = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "user_prompt"],
                input=json.dumps(blocked_prompt).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(json.loads(pb2.stdout.decode("utf-8")).get("terminationBehavior"), "terminate")

            # Allowed UserPromptSubmit runs once (invocation 1 delivers context, invocation 2 skips)
            alerts.raise_("test-alert-2", "second alert", "agent-kit-sync status")
            p1 = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "user_prompt"],
                input=json.dumps({"conversationId": "c1", "initialNumSteps": 0, "invocationNum": 1, "lastUserInput": "<USER_REQUEST>\nhi\n</USER_REQUEST>", "cwd": str(workspace)}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(p1.returncode, 0, p1.stderr)
            out1 = json.loads(p1.stdout.decode("utf-8"))
            self.assertTrue(any("second alert" in json.dumps(step) for step in out1.get("injectSteps", [])))
            self.assertTrue(any("prompt context" in json.dumps(step) for step in out1.get("injectSteps", [])))
            p2 = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "user_prompt"],
                input=json.dumps({"conversationId": "c1", "initialNumSteps": 0, "invocationNum": 2, "lastUserInput": "<USER_REQUEST>\nhi\n</USER_REQUEST>", "cwd": str(workspace)}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(json.loads(p2.stdout.decode("utf-8")), {})

            # 2. PreToolUse: native "ask", kit protocol tool kinds, exit code 2 -> deny, closed failure on web -> deny, write_file overwrite -> Edit overwrite
            ask_res = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "pre_tool_use"],
                input=json.dumps({"conversationId": "c1", "toolCall": {"name": "run_command", "args": {"CommandLine": "ask me"}}}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(json.loads(ask_res.stdout.decode("utf-8")), {"decision": "ask", "reason": "confirm ask"})
            search_res = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "pre_tool_use"],
                input=json.dumps({"conversationId": "c1", "toolCall": {"name": "grep_search", "args": {"Query": "foo"}}}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(search_res.returncode, 0)
            kit_rows = [json.loads(line) for line in kit_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(kit_rows, [{"tool": "shell", "app": "antigravity"}, {"tool": "search", "app": "antigravity"}])

            exit2_res = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "pre_tool_use"],
                input=json.dumps({"conversationId": "c1", "toolCall": {"name": "run_command", "args": {"CommandLine": "exit2"}}}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(exit2_res.returncode, 0)
            self.assertEqual(json.loads(exit2_res.stdout.decode("utf-8"))["decision"], "deny")

            closed_res = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "pre_tool_use"],
                input=json.dumps({"conversationId": "c1", "toolCall": {"name": "search_web", "args": {"query": "x"}}}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(json.loads(closed_res.stdout.decode("utf-8"))["decision"], "deny")

            ow_res = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "pre_tool_use"],
                input=json.dumps({"conversationId": "c1", "toolCall": {"name": "write_file", "args": {"Path": str(target), "Content": "OLD", "Overwrite": True}}}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(json.loads(ow_res.stdout.decode("utf-8")), {"decision": "allow", "overwrite": {"Content": "NEW"}})

            # 3. Stop gating on USER_CANCELLED skips hook vs NO_TOOL_CALL runs hook with translated transcript
            transcript = workspace / "transcript.jsonl"
            transcript.write_text(json.dumps({"type": "PLANNER_RESPONSE", "step_index": 1, "content": "Final reply text"}) + "\n", encoding="utf-8")
            cancelled = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "stop"],
                input=json.dumps({"conversationId": "c1", "terminationReason": "USER_CANCELLED", "cwd": str(workspace), "transcriptPath": str(transcript)}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(json.loads(cancelled.stdout.decode("utf-8")), {"decision": "allow"})
            self.assertFalse((workspace / "stop_ran.txt").exists())

            stopped = processes.run(
                [sys.executable, "-I", "-S", str(layout.launcher), "hook", "antigravity", "stop"],
                input=json.dumps({"conversationId": "c1", "terminationReason": "NO_TOOL_CALL", "cwd": str(workspace), "transcriptPath": str(transcript)}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
            )
            self.assertEqual(json.loads(stopped.stdout.decode("utf-8")), {"decision": "continue", "reason": "keep going"})
            self.assertEqual((workspace / "stop_ran.txt").read_text(encoding="utf-8"), "Final reply text")

            # 4. Windows shell execution with spaces in both python executable and launcher paths (via cmd /c and shell=True)
            if os_name() == "windows":
                space_py = home / "py with spaces" / "python.exe"
                space_py.parent.mkdir(parents=True)
                shutil.copy2(sys.executable, space_py)
                space_machine = MachineModel(
                    name=machine.name, apps=machine.apps, app_homes=machine.app_homes, tags=machine.tags,
                    python=str(space_py), projects=machine.projects, worktrees=machine.worktrees,
                    git_identity=machine.git_identity, policy=machine.policy, overrides=machine.overrides,
                )
                register.register(layout, space_machine, hooks)
                hooks_data = json.loads((plugin_dir / "hooks.json").read_text(encoding="utf-8"))
                pre_cmd = hooks_data[ANTIGRAVITY_PLUGIN_NAME]["PreToolUse"][0]["hooks"][0]["command"]
                self.assertTrue(pre_cmd.startswith("call "))
                for use_shell, cmd_arg in ((True, pre_cmd), (False, "cmd.exe /c " + pre_cmd)):
                    shell_res = processes.run(
                        cmd_arg,
                        shell=use_shell,
                        cwd=plugin_dir,
                        input=json.dumps({"conversationId": "c1", "toolCall": {"name": "run_command", "args": {"CommandLine": "ask me"}}}).encode("utf-8"),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env,
                    )
                    self.assertEqual(shell_res.returncode, 0, shell_res.stderr)
                    self.assertEqual(json.loads(shell_res.stdout.decode("utf-8")), {"decision": "ask", "reason": "confirm ask"})
                register.register(layout, machine, hooks)

            # 5. PostToolUse write tool sets want_sync when calling dispatch.main in-process
            with mock.patch.object(dispatch, "detach") as mocked_detach, mock.patch.dict(os.environ, {"AGENT_KIT_SYNC_NO_BACKGROUND": "", "AGENT_KIT_SYNC_TEST": "0"}):
                stdin = mock.MagicMock()
                stdin.isatty.return_value = False
                stdin.buffer.read.return_value = json.dumps({"conversationId": "c1", "toolCall": {"name": "edit_file", "args": {"Path": str(target)}}}).encode("utf-8")
                stdout = mock.MagicMock()
                with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
                    dispatch.main(layout.root, layout.launcher, "antigravity", "post_tool_use")
                mocked_detach.assert_called_once()

            # 6. Doctor passes cleanly, handles non-dict JSON, fails cleanly when plugin.json is removed; status lists plugin & unsynced
            with as_home(home):
                extra_skill = plugin_dir / "skills" / "untracked" / "SKILL.md"
                extra_skill.parent.mkdir(parents=True)
                extra_skill.write_text("---\nname: untracked\ndescription: x\n---\n", encoding="utf-8")
                extra_agent = home / ".gemini" / "config" / "agents" / "manual.md"
                extra_agent.parent.mkdir(parents=True, exist_ok=True)
                extra_agent.write_text("---\nname: manual\n---\n", encoding="utf-8")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli.main(["status"]), 0)
                self.assertIn("untracked", buf.getvalue())
                self.assertIn("manual.md", buf.getvalue())
                self.assertIn("rule file(s)", buf.getvalue())

                doc_ok = io.StringIO()
                with contextlib.redirect_stdout(doc_ok):
                    self.assertEqual(commands.cmd_doctor(SimpleNamespace(), layout), 0)
                self.assertIn("Antigravity:", doc_ok.getvalue())

                (plugin_dir / "plugin.json").write_text("[]", encoding="utf-8")
                doc_bad_json = io.StringIO()
                with contextlib.redirect_stdout(doc_bad_json):
                    self.assertEqual(commands.cmd_doctor(SimpleNamespace(), layout), 1)
                self.assertIn("plugin manifest", doc_bad_json.getvalue())

                (plugin_dir / "plugin.json").unlink()
                doc_bad = io.StringIO()
                with contextlib.redirect_stdout(doc_bad):
                    self.assertEqual(commands.cmd_doctor(SimpleNamespace(), layout), 1)
                self.assertIn("plugin manifest", doc_bad.getvalue())


if __name__ == "__main__":
    unittest.main()
