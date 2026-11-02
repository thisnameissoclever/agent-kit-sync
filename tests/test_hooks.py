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

from support import ROOT, TwoMachines, git
from agent_kit_sync import dispatch


HOOK_MODULE = """
    description = "Guards"

    [[hooks]]
    name = "no-rm"
    file = "hooks/no_rm.py"
    events = ["pre_tool_use"]
    tools = ["shell"]
    protocol = "native"
    timeout = 5
    on_failure = "open"

    [[hooks]]
    name = "kit-ctx"
    file = "hooks/kit_ctx.py"
    events = ["session_start"]
    protocol = "kit"

    [[hooks]]
    name = "crash"
    file = "hooks/crash.py"
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

    def test_empty_stop_in_codex_prints_json(self):
        _code, out = dispatch.emit("codex", "stop", dispatch.combine("codex", "stop", []), {}, False)
        self.assertEqual(out, b"{}")


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
        self.assertEqual(pre["hooks"][0]["args"][-2:], ["claude", "pre_tool_use"])
        codex = json.loads(self.a.read(".codex", "hooks.json"))
        self.assertIn("PreToolUse", codex["hooks"])
        # A blocked command
        payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}, "cwd": str(self.a.home)}).encode()
        result = self.cli("hook", "claude", "pre_tool_use", stdin=payload)
        out = json.loads(result.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Refusing rm -rf.", out["hookSpecificOutput"]["permissionDecisionReason"])
        # An allowed command prints nothing
        payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}}).encode()
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
        self.assertIn(str(self.a.layout.launcher), text)
        self.assertTrue(self.a.path(".agents", "skills", "agent-kit-sync-setup", "SKILL.md").is_file())

    def test_doctor_and_status(self):
        self.install()
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("hooks: 3", result.stdout.decode())
        result = self.cli("doctor")
        self.assertIn("Codex has not approved", result.stdout.decode())

    def test_missing_version_prints_repair_command(self):
        self.install()
        import shutil
        shutil.rmtree(self.a.layout.tool)
        payload = json.dumps({"session_id": "s1"}).encode()
        result = self.cli("hook", "claude", "session_start", stdin=payload)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"setup again", result.stderr)

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
