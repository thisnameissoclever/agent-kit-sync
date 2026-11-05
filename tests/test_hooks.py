"""The dispatcher, hook registration, and the launcher, run as an agent app would run them."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

from support import ROOT, TwoMachines, git, remove_tree
from agent_kit_sync import dispatch


HOOK_MODULE = """
    description = "Guards"

    [[hooks]]
    name = "no-rm"
    file = "hooks/no_rm.py"
    tests = ["tests/test_pass.py"]
    events = ["pre_tool_use"]
    tools = ["shell"]
    protocol = "native"
    timeout = 5
    on_failure = "open"

    [[hooks]]
    name = "kit-ctx"
    file = "hooks/kit_ctx.py"
    tests = ["tests/test_pass.py"]
    events = ["session_start"]
    protocol = "kit"

    [[hooks]]
    name = "crash"
    file = "hooks/crash.py"
    tests = ["tests/test_pass.py"]
    events = ["user_prompt"]
    protocol = "native"
    on_failure = "closed"
"""

NO_RM = """
    import json, sys
    data = json.load(sys.stdin)
    if "rm -rf" in data.get("tool_input", {}).get("command", ""):
        sys.stderr.write("Refusing rm -rf.")
        sys.exit(2)
"""

KIT_CTX = """
    import json, sys
    data = json.load(sys.stdin)
    print(json.dumps({"decision": "allow", "agent_context": "app=" + data["app"]}))
"""

CRASH = """
    raise SystemExit(3)
"""


class RecognizeOwnEntriesTests(unittest.TestCase):
    def test_launcher_path_is_recognized_plain_and_quoted(self):
        import shlex as _shlex
        import subprocess as _subprocess
        from agent_kit_sync.register import ours
        for path in ("/home/o'brien/.agent-kit-sync/launcher.py", "/Users/a b/.agent-kit-sync/launcher.py", r"C:\Users\a b\.agent-kit-sync\launcher.py"):
            launcher = Path(path)
            for command in (
                "python3 -I -S " + _shlex.quote(str(launcher)) + " hook claude stop",
                _subprocess.list2cmdline(["python.exe", "-I", "-S", str(launcher), "hook", "codex", "stop"]),
                "python3 -I -S " + str(launcher) + " hook claude stop",
            ):
                with self.subTest(path=path, command=command):
                    self.assertTrue(ours(command, launcher))
            self.assertFalse(ours("python3 /elsewhere/launcher.py hook claude stop", launcher))

    def test_entry_running_only_the_interpreter_is_detected_plain_and_quoted(self):
        import shlex as _shlex
        import subprocess as _subprocess
        from agent_kit_sync.register import interpreter_only
        for python in ("/opt/homebrew/bin/python3", "/Users/a b/bin/python3", r"C:\Program Files\Python39\python.exe"):
            for command in (python, _shlex.quote(python), _subprocess.list2cmdline([python]), " " + python + " "):
                with self.subTest(python=python, command=command):
                    self.assertTrue(interpreter_only({"type": "command", "command": command}, python))
            self.assertFalse(interpreter_only({"type": "command", "command": python, "args": ["-I", "launcher.py"]}, python))
            self.assertFalse(interpreter_only({"type": "command", "command": python + " -I -S launcher.py hook claude stop"}, python))
            self.assertFalse(interpreter_only({"type": "command", "command": "/usr/bin/other-python"}, python))


class DispatchUnitTests(unittest.TestCase):
    def test_matcher_and_tool_kinds(self):
        self.assertEqual(dispatch.tool_kind("claude", "Bash"), "shell")
        self.assertEqual(dispatch.tool_kind("codex", "apply_patch"), "write")
        self.assertEqual(dispatch.tool_kind("claude", "mcp__x__y"), "mcp")
        self.assertEqual(dispatch.matcher_for("claude", ["shell"]), "^(?:Bash|PowerShell)$")
        self.assertEqual(dispatch.matcher_for("claude", []), "*")

    def test_closed_failure_blocks_and_open_failure_warns(self):
        hook = {"module": "m", "name": "h", "protocol": "native", "on_failure": "closed"}
        combined = dispatch.combine("claude", "pre_tool_use", [{"hook": hook, "code": 1, "stdout": b"", "stderr": b"boom"}])
        code, out = dispatch.emit("claude", "pre_tool_use", combined, {}, False)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        hook["on_failure"] = "open"
        combined = dispatch.combine("claude", "pre_tool_use", [{"hook": hook, "code": 1, "stdout": b"", "stderr": b"boom"}])
        code, out = dispatch.emit("claude", "pre_tool_use", combined, {}, False)
        self.assertNotIn("hookSpecificOutput", json.loads(out))
        self.assertIn("failed", json.loads(out)["systemMessage"])

    def test_deny_wins_and_reasons_are_kept(self):
        a = {"hook": {"module": "m", "name": "a", "protocol": "kit"}, "code": 0, "stdout": json.dumps({"decision": "ask", "reason": "check"}).encode(), "stderr": b""}
        b = {"hook": {"module": "m", "name": "b", "protocol": "native"}, "code": 2, "stdout": b"", "stderr": b"no"}
        combined = dispatch.combine("claude", "pre_tool_use", [a, b])
        _code, out = dispatch.emit("claude", "pre_tool_use", combined, {}, False)
        specific = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertIn("check", specific["permissionDecisionReason"])
        self.assertIn("no", specific["permissionDecisionReason"])

    def test_ask_becomes_deny_where_it_is_not_enforced(self):
        a = {"hook": {"module": "m", "name": "a", "protocol": "kit"}, "code": 0, "stdout": json.dumps({"decision": "ask", "reason": "check"}).encode(), "stderr": b""}
        combined = dispatch.combine("claude", "pre_tool_use", [a])
        _code, out = dispatch.emit("claude", "pre_tool_use", combined, {}, True)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_stop_continue(self):
        a = {"hook": {"module": "m", "name": "a", "protocol": "kit"}, "code": 0, "stdout": json.dumps({"decision": "continue", "reason": "finish the tests"}).encode(), "stderr": b""}
        _code, out = dispatch.emit("codex", "stop", dispatch.combine("codex", "stop", [a]), {}, False)
        self.assertEqual(json.loads(out), {"decision": "block", "reason": "finish the tests"})

    def native(self, code=0, stdout=b"", stderr=b"", protocol="native"):
        return {"hook": {"module": "m", "name": "h", "protocol": protocol, "on_failure": "open"}, "code": code, "stdout": stdout, "stderr": stderr}

    def test_lone_native_hook_output_passes_through_unchanged(self):
        printed = b'{"systemMessage": "Writing check: x", "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "x"}}\n'
        for result, expected in (
            (self.native(0, printed, b"note"), (0, printed, b"note", [])),
            (self.native(2, b"", b"Refusing."), (2, b"", b"Refusing.", [])),
            (self.native(0, b"plain context\n"), (0, b"plain context\n", b"", [])),
            (self.native(0, b"", b"only a note"), (0, b"", b"only a note", [])),
        ):
            with self.subTest(result=result):
                self.assertEqual(dispatch.pass_through("claude", "pre_tool_use", [result], [], False), expected)

    def test_hooks_the_combining_path_must_handle(self):
        two = [self.native(0, b"{}"), self.native(0, b"{}")]
        for results, app, event in (
            (two, "claude", "pre_tool_use"),
            ([self.native(1, b"", b"boom")], "claude", "pre_tool_use"),
            ([self.native(0, b'{"decision": "allow"}', protocol="kit")], "claude", "pre_tool_use"),
            ([self.native(0, b"")], "codex", "stop"),
            ([], "claude", "pre_tool_use"),
        ):
            with self.subTest(app=app, event=event, n=len(results)):
                self.assertIsNone(dispatch.pass_through(app, event, results, [], False))

    def test_alerts_join_a_lone_hooks_json(self):
        alert = {"id": "1", "message": "Something failed.", "command": "agent-kit-sync status"}
        printed = json.dumps({"systemMessage": "Mine.", "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "Context."}}).encode()
        code, out, _err, delivered = dispatch.pass_through("claude", "user_prompt", [self.native(0, printed)], [alert], False)
        data = json.loads(out)
        self.assertEqual(data["systemMessage"], "Mine.\nagent-kit-sync: Something failed.")
        self.assertTrue(data["hookSpecificOutput"]["additionalContext"].startswith("Context.\n\nagent-kit-sync raised 1 alert"))
        self.assertEqual((code, delivered), (0, [alert]))
        # Exit code 2 and plain text cannot carry an alert, so it waits for the next event.
        self.assertEqual(dispatch.pass_through("claude", "pre_tool_use", [self.native(2, b"", b"No.")], [alert], False), (2, b"", b"No.", []))
        # Plain text on session_start is context, so the combining path keeps it and adds the alert.
        self.assertIsNone(dispatch.pass_through("claude", "session_start", [self.native(0, b"Text.\n")], [alert], False))
        combined = dispatch.combine("claude", "session_start", [self.native(0, b"Text.\n")])
        self.assertEqual(combined.context, ["Text."])
        # With no output to keep, the combining path delivers it.
        self.assertIsNone(dispatch.pass_through("claude", "pre_tool_use", [self.native(0, b"")], [alert], False))

    def test_lone_hooks_ask_becomes_deny_where_it_is_not_enforced(self):
        printed = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": "Quoted dash."}}).encode()
        for app, ask_is_deny in (("claude", True), ("codex", False)):
            with self.subTest(app=app):
                _code, out, _err, _delivered = dispatch.pass_through(app, "pre_tool_use", [self.native(0, printed)], [], ask_is_deny)
                specific = json.loads(out)["hookSpecificOutput"]
                self.assertEqual(specific["permissionDecision"], "deny")
                # The same wording emit() uses for each app.
                combined = dispatch.combine(app, "pre_tool_use", [self.native(0, printed)])
                expected = json.loads(dispatch.emit(app, "pre_tool_use", combined, {}, ask_is_deny)[1])["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertEqual(specific["permissionDecisionReason"], expected)
        self.assertEqual(dispatch.pass_through("claude", "pre_tool_use", [self.native(0, printed)], [], False)[1], printed)

    def test_empty_stop_in_codex_prints_json(self):
        _code, out = dispatch.emit("codex", "stop", dispatch.combine("codex", "stop", []), {}, False)
        self.assertEqual(out, b"{}")


def dispatch_event(native):
    """The tool's name for one of Claude Code's events."""
    return next(key for key, value in dispatch.APP_EVENTS["claude"].items() if value == native)


class InstalledToolTests(TwoMachines):
    """Install the tool into a fake kit folder and call it through the launcher."""

    def setUp(self):
        super().setUp()
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n',
            "modules/guards/module.toml": HOOK_MODULE,
            "modules/guards/hooks/no_rm.py": NO_RM,
            "modules/guards/hooks/kit_ctx.py": KIT_CTX,
            "modules/guards/hooks/crash.py": CRASH,
            "modules/guards/tests/test_pass.py": "import sys\nsys.exit(0)\n",
        })
        self.a = self.machine("a")
        tool = self.tmp / "tool"
        git(None, "clone", "-q", str(ROOT), str(tool))
        # The tool repo clone must have this working tree's current code.
        for path in ROOT.rglob("*.py"):
            if ".git" in path.parts:
                continue
            rel = path.relative_to(ROOT)
            target = tool / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        for path in (ROOT / "kit").rglob("*"):
            if path.is_file():
                target = tool / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        git(tool, "checkout", "-q", "-B", "main")
        git(tool, "add", "-A")
        git(tool, "commit", "-q", "--allow-empty", "-m", "current")
        self.tool_url = str(tool)

    def cli(self, *args, stdin=b"", extra_env=None):
        env = dict(os.environ)
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "AGENT_KIT_SYNC_HOME"):
            env.pop(name, None)
        # Keep Git on the path but hide the user's own claude and codex programs.
        git_dir = os.path.dirname(shutil.which("git") or "/usr/bin/git")
        env.update({"HOME": str(self.a.home), "USERPROFILE": str(self.a.home), "AGENT_KIT_SYNC_NO_BACKGROUND": "1", "PATH": os.pathsep.join([git_dir, os.path.dirname(sys.executable)])})
        env.update(extra_env or {})
        launcher = self.a.layout.launcher
        cmd = [sys.executable, "-I", "-S", str(launcher)] if launcher.exists() else [sys.executable, "-I", "-S", str(ROOT / "bootstrap.py")]
        return subprocess.run(cmd + list(args), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)

    def install(self):
        result = self.cli("setup", "init", "--tool-url", self.tool_url)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        result = self.cli("sync")
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())

    def test_registration_and_dispatch_through_launcher(self):
        self.install()
        settings = json.loads(self.a.read(".claude", "settings.json"))
        events = sorted(settings["hooks"])
        self.assertEqual(events, ["PostToolUse", "PreToolUse", "SessionStart", "Stop", "UserPromptSubmit"])
        pre = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(pre["matcher"], "^(?:Bash|PowerShell)$")
        handler = pre["hooks"][0]
        if os.name == "nt":
            self.assertEqual(handler["args"][-2:], ["claude", "pre_tool_use"])
        else:
            # One command string, so a program that keeps only type and command keeps a working entry.
            self.assertEqual(sorted(handler), ["command", "timeout", "type"])
            self.assertTrue(handler["command"].endswith(" hook claude pre_tool_use"), handler["command"])
        codex = json.loads(self.a.read(".codex", "hooks.json"))
        self.assertIn("PreToolUse", codex["hooks"])
        # A blocked command: the only hook's exit code 2 and its error text reach the app unchanged.
        payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}, "cwd": str(self.a.home)}).encode()
        result = self.cli("hook", "claude", "pre_tool_use", stdin=payload)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (2, b"", b"Refusing rm -rf."))
        # The open Codex approval alert could not ride on that answer, so the next event shows it, once.
        payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}}).encode()
        self.assertIn("Codex", json.loads(self.cli("hook", "claude", "pre_tool_use", stdin=payload).stdout)["systemMessage"])
        # An allowed command prints nothing
        self.assertEqual(self.cli("hook", "claude", "pre_tool_use", stdin=payload).stdout, b"")
        # A kit hook adds context and names the calling app
        payload = json.dumps({"session_id": "s2", "hook_event_name": "SessionStart"}).encode()
        out = json.loads(self.cli("hook", "codex", "session_start", stdin=payload).stdout)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("app=codex"), context)
        self.assertIn("Codex", out.get("systemMessage", ""))  # The open Codex approval alert, shown once.
        # A closed hook that crashes blocks the prompt
        payload = json.dumps({"session_id": "s3", "prompt": "hi"}).encode()
        out = json.loads(self.cli("hook", "claude", "user_prompt", stdin=payload).stdout)
        self.assertEqual(out["decision"], "block")

    def test_registration_keeps_other_entries_and_is_idempotent(self):
        other = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "other-tool capture"}]}]}, "theme": "dark"}
        self.a.path(".claude", "settings.json").write_text(json.dumps(other, indent=2))
        self.install()
        first = self.a.read(".claude", "settings.json")
        data = json.loads(first)
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "other-tool capture")
        self.cli("sync")
        self.assertEqual(self.a.read(".claude", "settings.json"), first)

    def test_uninstall_restores(self):
        original = "# Mine\n\nKeep this.\n"
        self.a.path(".claude", "CLAUDE.md").write_text(original)
        other = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
        self.a.path(".claude", "settings.json").write_text(json.dumps(other, indent=2) + "\n")
        self.install()
        self.assertIn("agent-kit-sync begin agent-kit-sync/rules", self.a.read(".claude", "CLAUDE.md"))
        result = self.cli("uninstall", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertEqual(self.a.read(".claude", "CLAUDE.md"), original)
        self.assertEqual(json.loads(self.a.read(".claude", "settings.json")), other)
        self.assertFalse(self.a.path(".claude", "skills", "portable-scripts").exists())

    def test_bundled_rules_and_skills_are_installed(self):
        self.install()
        text = self.a.read(".codex", "AGENTS.md")
        self.assertIn("disable MODULE/ITEM", text)
        if sys.platform.startswith("win"):
            wrapper = self.a.layout.bin / "agent-kit-sync.cmd"
            self.assertIn(wrapper.as_posix(), text)
            cmd = ["cmd", "/c", str(wrapper), "--version"]
        else:
            wrapper = self.a.layout.bin / "agent-kit-sync"
            self.assertIn(str(wrapper), text)
            cmd = [str(wrapper), "--version"]
        self.assertTrue(wrapper.is_file())
        result = subprocess.run(cmd, stdout=subprocess.PIPE, env=dict(os.environ, HOME=str(self.a.home), USERPROFILE=str(self.a.home)), timeout=30)
        self.assertEqual(result.returncode, 0)
        from agent_kit_sync import VERSION
        self.assertIn(VERSION, result.stdout.decode())
        self.assertTrue(self.a.path(".agents", "skills", "agent-kit-sync-setup", "SKILL.md").is_file())

    def test_doctor_and_status(self):
        self.install()
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("hooks: 3", result.stdout.decode())
        result = self.cli("doctor")
        self.assertIn("Codex has not approved", result.stdout.decode())

    def test_entries_survive_a_rewrite_that_keeps_only_type_and_command(self):
        self.install()
        path = self.a.path(".claude", "settings.json")
        data = json.loads(path.read_text())
        # Another program rewrites the file and keeps two fields of each handler, as DevBar did.
        for groups in data["hooks"].values():
            for group in groups:
                group["hooks"] = [{"type": h["type"], "command": h["command"]} for h in group["hooks"]]
        path.write_text(json.dumps(data, indent=2))
        if os.name != "nt":
            # Every rewritten entry still runs the launcher for its own event.
            for event, groups in json.loads(path.read_text())["hooks"].items():
                for group in groups:
                    for h in group["hooks"]:
                        self.assertIn("launcher.py", h["command"], (event, h))
                        self.assertTrue(h["command"].endswith(" " + dispatch_event(event)), (event, h))
        result = self.cli("sync")
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        after = json.loads(path.read_text())
        for event, groups in after["hooks"].items():
            mine = [h for g in groups for h in g["hooks"] if "launcher.py" in h["command"] + " ".join(h.get("args", []))]
            self.assertEqual(len(mine), 1, (event, groups))
            self.assertEqual(mine[0].get("timeout"), 60, (event, mine[0]))
        result = self.cli("doctor")
        self.assertIn("Claude Code: %d agent-kit-sync hook entries" % len(after["hooks"]), result.stdout.decode())

    def test_exec_form_entries_from_an_earlier_version_are_replaced(self):
        self.install()
        path = self.a.path(".claude", "settings.json")
        data = json.loads(path.read_text())
        # Entries in the form earlier versions wrote: the program, its arguments, and a timeout.
        for groups in data["hooks"].values():
            for group in groups:
                for h in group["hooks"]:
                    if "launcher.py" in h["command"] and "args" not in h:
                        import shlex as _shlex
                        parts = _shlex.split(h["command"])
                        h.clear()
                        h.update({"type": "command", "command": parts[0], "args": parts[1:], "timeout": 60})
        path.write_text(json.dumps(data, indent=2))
        result = self.cli("sync")
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        after = json.loads(path.read_text())
        for event, groups in after["hooks"].items():
            mine = [h for g in groups for h in g["hooks"] if "launcher.py" in h["command"] + " ".join(h.get("args", []))]
            self.assertEqual(len(mine), 1, (event, groups))
            if os.name != "nt":
                self.assertNotIn("args", mine[0])

    def test_doctor_reports_an_entry_that_runs_only_python(self):
        self.install()
        path = self.a.path(".claude", "settings.json")
        data = json.loads(path.read_text())
        from agent_kit_sync import toml
        python = toml.load_file(self.a.layout.machine_file)["python"]
        data["hooks"]["Stop"].append({"matcher": "", "hooks": [{"type": "command", "command": python}]})
        path.write_text(json.dumps(data, indent=2))
        out = self.cli("doctor").stdout.decode()
        self.assertIn("1 hook entries that run only", out)
        self.assertIn("(Stop)", out)

    def test_missing_version_prints_repair_command(self):
        self.install()
        remove_tree(self.a.layout.tool)
        payload = json.dumps({"session_id": "s1"}).encode()
        result = self.cli("hook", "claude", "session_start", stdin=payload)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"setup again", result.stderr)

    def test_self_update_installs_a_new_commit_and_follows_origin(self):
        self.install()
        from agent_kit_sync import gitops as g
        origin = g.run(self.a.layout.tool_repo, "remote", "get-url", "origin").stdout.decode().strip()
        self.assertEqual(Path(origin).resolve(), Path(self.tool_url).resolve())
        before = (self.a.layout.tool / "current").read_text().strip()
        tool = Path(self.tool_url)
        (tool / "NOTES.md").write_text("new commit\n")
        git(tool, "add", "-A")
        git(tool, "commit", "-q", "-m", "next")
        result = self.cli("update")
        self.assertIn(b"updated", result.stdout, result.stdout + result.stderr)
        after = (self.a.layout.tool / "current").read_text().strip()
        self.assertNotEqual(before, after)

    def test_self_update_keeps_old_version_when_self_test_fails(self):
        self.install()
        before = (self.a.layout.tool / "current").read_text().strip()
        tool = Path(self.tool_url)
        (tool / "agent_kit_sync" / "selftest.py").write_text("import sys\ndef main():\n    return 1\n")
        git(tool, "add", "-A")
        git(tool, "commit", "-q", "-m", "broken")
        result = self.cli("update")
        self.assertIn(b"failed", result.stdout)
        self.assertEqual((self.a.layout.tool / "current").read_text().strip(), before)

    def test_lone_hooks_error_text_survives_when_an_alert_is_added(self):
        self.install()
        note = self.a.layout.config / "modules/guards/hooks/note.py"
        manifest = self.a.layout.config / "modules/guards/module.toml"
        manifest.write_text(manifest.read_text() + '\n[[hooks]]\nname = "note"\nfile = "hooks/note.py"\nevents = ["stop"]\nprotocol = "native"\ntests = ["tests/test_pass.py"]\n')
        note.write_text("import sys\nsys.stdin.read()\nsys.stderr.write('stop note')\n")
        git(self.a.layout.config, "add", "-A")
        git(self.a.layout.config, "commit", "-q", "-m", "note hook")
        self.assertEqual(self.cli("sync", "--offline").returncode, 0)
        payload = json.dumps({"session_id": "s9", "hook_event_name": "Stop"}).encode()
        result = self.cli("hook", "codex", "stop", stdin=payload)
        self.assertEqual(result.stderr, b"stop note")
        self.assertIn("Codex", json.loads(result.stdout)["systemMessage"])  # The open approval alert.
        result = self.cli("hook", "codex", "stop", stdin=payload)
        self.assertEqual((result.stdout, result.stderr), (b"{}", b"stop note"))

    def test_dispatcher_timing(self):
        self.install()
        payload = json.dumps({"session_id": "t", "tool_name": "Read", "tool_input": {}}).encode()
        times = []
        for _ in range(5):
            started = time.perf_counter()
            self.cli("hook", "claude", "pre_tool_use", stdin=payload)
            times.append(time.perf_counter() - started)
        sys.stderr.write(f"\nempty dispatcher path: median {sorted(times)[2] * 1000:.0f} ms\n")


if __name__ == "__main__":
    unittest.main()
