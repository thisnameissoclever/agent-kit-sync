"""Sync between apps on one machine and between machines."""

import json
import unittest

from support import TwoMachines, git
from agent_kit_sync import toml


class InstallTests(TwoMachines):
    def test_installs_every_item_in_each_app(self):
        self.standard_config()
        a = self.machine("a")
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        self.assertIn("Write plainly.", a.read(".claude", "CLAUDE.md"))
        self.assertIn(str(a.home / "Projects"), a.read(".codex", "AGENTS.md"))
        self.assertIn("Read the draft", a.read(".claude", "skills", "review", "SKILL.md"))
        self.assertIn("Read the draft", a.read(".agents", "skills", "review", "SKILL.md"))
        claude_agent = a.read(".claude", "agents", "helper.md")
        self.assertIn("tools: Read, Grep", claude_agent)
        self.assertNotIn("sandbox_mode", claude_agent)
        codex_agent = toml.loads(a.read(".codex", "agents", "helper.toml"))
        self.assertEqual(codex_agent["sandbox_mode"], "read-only")
        self.assertIn("Help with the task.", codex_agent["developer_instructions"])
        settings = json.loads(a.read(".claude", "settings.json"))
        self.assertEqual(settings["env"]["MAX_TOKENS"], "10000")

    def test_codex_only_machine_gets_only_codex_copies(self):
        self.standard_config()
        a = self.machine("a", apps=("codex",))
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        self.assertTrue(a.path(".codex", "AGENTS.md").is_file())
        self.assertFalse(a.path(".claude").exists())

    def test_second_run_changes_nothing(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        _engine, report = a.sync()
        self.assertEqual((report.installed, report.edits, report.removed, report.pushed), ([], [], [], False))

    def test_keeps_text_outside_markers(self):
        self.standard_config()
        a = self.machine("a")
        a.path(".claude", "CLAUDE.md").write_text("# Mine\n\nMy own rule.\n", encoding="utf-8")
        a.sync()
        text = a.read(".claude", "CLAUDE.md")
        self.assertTrue(text.startswith("# Mine\n\nMy own rule.\n"))
        self.assertIn("<!-- agent-kit-sync begin writing/plain -->", text)


class EditTests(TwoMachines):
    def test_codex_skill_edit_reaches_claude_and_other_machine(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        codex_skill = a.path(".agents", "skills", "review", "SKILL.md")
        a.write(codex_skill, codex_skill.read_text().replace("report problems", "report every problem"))
        _engine, report = a.sync()
        self.assertEqual(report.edits, ["writing/review"])
        self.assertTrue(report.pushed)
        self.assertIn("report every problem", a.read(".claude", "skills", "review", "SKILL.md"))
        b.sync()
        self.assertIn("report every problem", b.read(".claude", "skills", "review", "SKILL.md"))
        self.assertIn("report every problem", b.read(".agents", "skills", "review", "SKILL.md"))

    def test_section_edit_keeps_placeholders(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        path = a.path(".claude", "CLAUDE.md")
        a.write(path, path.read_text().replace("Write plainly.", "Write plainly and briefly."))
        a.sync()
        source = (a.layout.config / "modules/writing/sections/plain.md").read_text()
        self.assertIn("{{kit.projects}}", source)
        self.assertIn("briefly", a.read(".codex", "AGENTS.md"))
        b.sync()
        text = b.read(".codex", "AGENTS.md")
        self.assertIn("briefly", text)
        self.assertIn(str(b.home / "Projects"), text)

    def test_codex_subagent_toml_edit_is_carried_back(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        path = a.path(".codex", "agents", "helper.toml")
        a.write(path, path.read_text().replace('"read-only"', '"workspace-write"').replace("Help with the task.", "Help carefully."))
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        source = (a.layout.config / "modules/writing/agents/helper.md").read_text()
        self.assertIn("codex.sandbox_mode: workspace-write", source)
        self.assertIn("Help carefully.", source)
        self.assertIn("Help carefully.", a.read(".claude", "agents", "helper.md"))

    def test_edit_with_machine_path_is_held(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "skills", "review", "SKILL.md")
        a.write(path, path.read_text() + "See /Users/someone/notes.txt\n")
        engine, report = a.sync()
        self.assertEqual(len(report.held), 1)
        self.assertFalse(report.pushed)
        self.assertNotIn("someone", (a.layout.config / "modules/writing/skills/review/SKILL.md").read_text())

    def test_blocklisted_term_is_held(self):
        self.standard_config()
        a = self.machine("a")
        a.layout.blocklist.write_text("acme corp\n", encoding="utf-8")
        a.sync()
        path = a.path(".claude", "skills", "review", "SKILL.md")
        a.write(path, path.read_text() + "Ask Acme Corp first.\n")
        _engine, report = a.sync()
        self.assertEqual(len(report.held), 1)
        self.assertNotIn("Acme", (a.layout.config / "modules/writing/skills/review/SKILL.md").read_text())

    def test_new_file_in_skill_folder_syncs(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        a.write(a.path(".claude", "skills", "review", "notes.md"), "Extra notes.\n")
        a.write(a.path(".claude", "skills", "review", ".DS_Store"), "junk")
        a.sync()
        b.sync()
        self.assertEqual(b.read(".agents", "skills", "review", "notes.md"), "Extra notes.\n")
        self.assertFalse(b.path(".agents", "skills", "review", ".DS_Store").exists())


class DeletionTests(TwoMachines):
    def test_deleting_a_skill_deletes_it_everywhere(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        import shutil
        shutil.rmtree(a.path(".claude", "skills", "review"))
        _engine, report = a.sync()
        self.assertEqual(report.deletions, ["writing/review"])
        self.assertFalse(a.path(".agents", "skills", "review").exists())
        b.sync()
        self.assertFalse(b.path(".claude", "skills", "review").exists())

    def test_missing_app_folder_holds_deletions(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        import shutil
        shutil.rmtree(a.path(".claude"))
        _engine, report = a.sync()
        self.assertEqual(report.deletions, [])
        self.assertTrue(report.held)
        self.assertTrue((a.layout.config / "modules/writing/skills/review/SKILL.md").exists())


class PolicyTests(TwoMachines):
    def test_new_item_waits_for_approval(self):
        self.standard_config()
        a = self.machine("a")
        b = self.machine("b", policy={"new_items": "ask", "code_changes": "apply", "other_changes": "apply", "outgoing": "push"})
        a.sync()
        b.sync()
        self.assertFalse(b.path(".claude", "skills", "review").exists())
        engine, _report = b.sync()
        held = [entry for entry in engine.held.all() if entry["kind"] == "new-item"]
        self.assertTrue(held)

    def test_incoming_code_change_is_held_and_never_installed(self):
        self.standard_config()
        self.seed_files({
            "modules/writing/module.toml": (self.seed / "modules/writing/module.toml").read_text() + '\n[[hooks]]\nname = "check"\nfile = "hooks/check.py"\nevents = ["stop"]\n',
            "modules/writing/hooks/check.py": "print('v1')\n",
        }, "add hook")
        a = self.machine("a")
        b = self.machine("b", policy={"new_items": "apply", "code_changes": "ask", "other_changes": "apply", "outgoing": "push"})
        a.sync()
        b.sync()
        installed = b.layout.installed / "writing" / "hooks" / "check.py"
        self.assertEqual(installed.read_text(), "print('v1')\n")
        a_hook = a.layout.installed / "writing" / "hooks" / "check.py"
        a.write(a_hook, "print('v2')\n")
        a.sync()
        engine, _report = b.sync()
        self.assertEqual(installed.read_text(), "print('v1')\n")
        self.assertTrue([entry for entry in engine.held.all() if entry["kind"] == "incoming"])

    def test_settings_changed_by_other_software_are_held(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "settings.json")
        data = json.loads(path.read_text())
        data["env"]["MAX_TOKENS"] = "5000"
        data["other"] = True
        a.write(path, json.dumps(data, indent=2))
        engine, report = a.sync()
        self.assertTrue([entry for entry in engine.held.all() if entry["kind"] == "setting"])
        self.assertEqual(json.loads(path.read_text())["env"]["MAX_TOKENS"], "5000")
        self.assertIn('value = "10000"', (a.layout.config / "modules/writing/module.toml").read_text())


class ConflictTests(TwoMachines):
    def test_same_lines_edited_on_two_machines(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        pa = a.path(".claude", "skills", "review", "SKILL.md")
        a.write(pa, pa.read_text().replace("report problems", "report A"))
        pb = b.path(".claude", "skills", "review", "SKILL.md")
        b.write(pb, pb.read_text().replace("report problems", "report B"))
        a.sync()
        engine, _report = b.sync()
        self.assertIn("report A", b.read(".claude", "skills", "review", "SKILL.md"))
        held = [entry for entry in engine.held.all() if entry["kind"] == "merge-conflict"]
        self.assertEqual(len(held), 1)
        self.assertIn(b"report B", engine.held.blob(held[0], "ours"))


if __name__ == "__main__":
    unittest.main()
