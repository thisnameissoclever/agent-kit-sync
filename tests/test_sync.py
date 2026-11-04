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

    def test_one_app_block_with_hook_reaches_only_that_app(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guarded"\n',
            "modules/guarded/module.toml": """
                description = "A subagent with its own hook"

                [[agents]]
                name = "reviewer"
                file = "agents/reviewer.md"

                [[hooks]]
                name = "guard"
                file = "hooks/guard.py"
                events = []
            """,
            "modules/guarded/hooks/guard.py": "import sys\nsys.exit(0)\n",
            "modules/guarded/agents/reviewer.md": """
                ---
                name: reviewer
                description: Reviews
                claude.hooks: {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "{{kit.hook guarded/guard}}"}]}]}
                codex.sandbox_mode: read-only
                ---
                Review the work.

                {{kit.only claude cursor}}
                A hook runs each Bash command through {{kit.hook guarded/guard}}.

                {{kit.end}}
                Report what you find.
            """,
        })
        a = self.machine("a")
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        claude = a.read(".claude", "agents", "reviewer.md")
        self.assertIn("run-hook guarded/guard", claude)
        self.assertIn("A hook runs each Bash command", claude)
        self.assertNotIn("kit.", claude)
        codex = toml.loads(a.read(".codex", "agents", "reviewer.toml"))
        self.assertEqual(codex["developer_instructions"], "Review the work.\n\nReport what you find.\n")
        self.assertNotIn("hooks", codex)
        path = a.path(".codex", "agents", "reviewer.toml")
        a.write(path, path.read_text().replace("Report what you find.", "Report every finding."))
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        source = (a.layout.config / "modules/guarded/agents/reviewer.md").read_text()
        self.assertIn("Report every finding.", source)
        self.assertIn("{{kit.only claude cursor}}", source)
        self.assertIn("through {{kit.hook guarded/guard}}.", source)
        self.assertIn("Report every finding.", a.read(".claude", "agents", "reviewer.md"))

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


class AdoptionTests(TwoMachines):
    def test_existing_identical_file_survives_removal(self):
        self.standard_config()
        a = self.machine("a")
        skill = a.path(".claude", "skills", "review", "SKILL.md")
        skill.parent.mkdir(parents=True)
        skill.write_text((self.seed / "modules/writing/skills/review/SKILL.md").read_text())
        a.sync()
        data = a.machine_data()
        data.setdefault("overrides", {})["writing/review"] = {"kind": "disable"}
        a.write_machine(data)
        a.sync()
        self.assertTrue(skill.is_file())
        self.assertFalse(a.path(".agents", "skills", "review").exists())

    def test_incoming_item_does_not_overwrite_a_different_unmanaged_file(self):
        self.standard_config()
        a = self.machine("a")
        mine = a.path(".claude", "skills", "review", "SKILL.md")
        mine.parent.mkdir(parents=True)
        mine.write_text("my own review skill\n")
        engine, report = a.sync()
        self.assertEqual(mine.read_text(), "my own review skill\n")
        self.assertTrue([e for e in engine.held.all() if e["kind"] == "collision"])
        self.assertIn("Read the draft", a.read(".agents", "skills", "review", "SKILL.md"))

    def test_size_alert_from_an_earlier_version_is_closed(self):
        self.standard_config()
        a = self.machine("a")
        engine, _report = a.sync()
        engine.alerts.raise_("size:codex", "AGENTS.md is 35,000 bytes, past 32 KiB.", "agent-kit-sync status")
        engine, _report = a.sync()
        self.assertFalse([x for x in engine.alerts.open() if x["key"].startswith("size:")])

    def test_resolving_each_apps_collision_closes_its_alert(self):
        self.standard_config()
        a = self.machine("a")
        for parts in ((".claude", "agents", "helper.md"), (".codex", "agents", "helper.toml")):
            mine = a.path(*parts)
            mine.parent.mkdir(parents=True, exist_ok=True)
            mine.write_text("my own helper\n")
        engine, _report = a.sync()
        collisions = [e for e in engine.held.all() if e["kind"] == "collision"]
        self.assertEqual(sorted(e["app"] for e in collisions), ["claude", "codex"])
        from agent_kit_sync.resolve import resolve
        with a.env():
            resolve(a.layout, collisions[0]["id"], "config-replaces-local")
            open_keys = [x["key"] for x in engine.alerts.open()]
            self.assertNotIn(f"collision:{collisions[0]['app']}|writing/helper", open_keys)
            self.assertIn(f"collision:{collisions[1]['app']}|writing/helper", open_keys)
            resolve(a.layout, collisions[1]["id"], "config-replaces-local")
        self.assertFalse([x for x in engine.alerts.open() if x["key"].startswith("collision:")])
        a.sync()
        self.assertNotEqual(a.read(".claude", "agents", "helper.md"), "my own helper\n")
        self.assertNotEqual(a.read(".codex", "agents", "helper.toml"), "my own helper\n")


class ReviewRegressionTests(TwoMachines):
    def test_merges_do_not_create_endless_commits(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        path = a.path(".claude", "skills", "review", "SKILL.md")
        a.write(path, path.read_text() + "More.\n")
        a.sync()
        for _ in range(3):
            b.sync()
            a.sync()
        count = int(git(self.remote, "rev-list", "--count", "main").strip())
        self.assertLessEqual(count, 3)

    def test_skill_script_change_is_held_as_code(self):
        self.standard_config()
        self.seed_files({"modules/writing/skills/review/run": "#!/bin/sh\necho hi\n"}, "script")
        a = self.machine("a")
        b = self.machine("b", policy={"new_items": "apply", "code_changes": "ask", "other_changes": "apply", "outgoing": "push"})
        a.sync()
        b.sync()
        script = a.path(".claude", "skills", "review", "run")
        a.write(script, "#!/bin/sh\necho changed\n")
        a.sync()
        engine, _report = b.sync()
        self.assertEqual(b.read(".claude", "skills", "review", "run"), "#!/bin/sh\necho hi\n")
        self.assertTrue([e for e in engine.held.all() if e["kind"] == "incoming"])

    def test_delete_on_one_machine_and_edit_on_another_keeps_item(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        import shutil
        shutil.rmtree(a.path(".claude", "skills", "review"))
        a.sync()
        path = b.path(".claude", "skills", "review", "SKILL.md")
        b.write(path, path.read_text().replace("report problems", "report B"))
        b.sync()
        self.assertIn("report B", b.read(".claude", "skills", "review", "SKILL.md"))
        self.assertIn('name = "review"', (b.layout.config / "modules/writing/module.toml").read_text())
        a.sync()
        self.assertIn("report B", a.read(".claude", "skills", "review", "SKILL.md"))

    def test_unrenderable_module_keeps_previous_copies_and_alerts(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        agent = self.seed / "modules/writing/agents/helper.md"
        self.seed_files({"modules/writing/agents/helper.md": "---\nname: helper\ndescription: |\n  multi\n  line\n---\nBody.\n"}, "bad")
        engine, report = a.sync()
        self.assertTrue(a.path(".claude", "skills", "review", "SKILL.md").exists())
        self.assertTrue(a.path(".codex", "agents", "helper.toml").exists())
        self.assertTrue([x for x in engine.alerts.open() if x["key"] == "module:writing"])

    def test_contains_setting_tolerates_added_entries(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "perms"\n',
            "modules/perms/module.toml": '[[settings]]\napp = "claude"\nkey = "permissions.allow"\nvalue = ["Read"]\nmode = "contains"\n',
        })
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "settings.json")
        data = json.loads(path.read_text())
        data["permissions"]["allow"].append("Bash(npm test)")
        a.write(path, json.dumps(data, indent=2))
        engine, _report = a.sync()
        self.assertEqual([e for e in engine.held.all() if e["kind"] == "setting"], [])

    def test_codex_toml_list_field_edit_is_held_not_stringified(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        path = a.path(".codex", "agents", "helper.toml")
        a.write(path, path.read_text() + 'skills_config = ["a", "b"]\n')
        engine, report = a.sync()
        self.assertTrue(report.held)
        self.assertNotIn("skills_config", (a.layout.config / "modules/writing/agents/helper.md").read_text())

    def test_variant_path_outside_module_is_rejected(self):
        self.standard_config()
        self.seed_files({"modules/writing/module.toml": (self.seed / "modules/writing/module.toml").read_text().replace('file = "sections/plain.md"', 'file = "sections/plain.md"\nvariants = { codex = "../../kit.toml" }')}, "traversal")
        a = self.machine("a")
        engine, report = a.sync()
        self.assertTrue(report.errors)
        self.assertNotIn("format = 1", a.read(".codex", "AGENTS.md") if a.path(".codex", "AGENTS.md").exists() else "")

    def test_failing_checkout_file_does_not_block_merges(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        bad = b.layout.config / "modules/writing/skills/review/SKILL.md"
        bad.write_text(bad.read_text() + "/Users/someone/secret\n")
        path = a.path(".claude", "skills", "review", "SKILL.md")
        a.write(path, path.read_text() + "From A.\n")
        a.sync()
        _engine, report = b.sync()
        self.assertIn("From A.", b.read(".claude", "skills", "review", "SKILL.md"))
        self.assertNotIn("someone", (b.layout.config / "modules/writing/skills/review/SKILL.md").read_text())

    def test_append_override_on_a_skill(self):
        self.standard_config()
        a = self.machine("a")
        extra = a.layout.root / "local" / "review-extra.md"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("Only on this machine.\n")
        data = a.machine_data()
        data.setdefault("overrides", {})["writing/review"] = {"kind": "append", "file": str(extra)}
        a.write_machine(data)
        a.sync()
        self.assertIn("Only on this machine.", a.read(".claude", "skills", "review", "SKILL.md"))
        self.assertNotIn("Only on this machine.", (a.layout.config / "modules/writing/skills/review/SKILL.md").read_text())


    def test_hook_from_a_local_module_never_becomes_a_shared_placeholder(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        mine = a.layout.local_modules / "mine"
        (mine / "hooks").mkdir(parents=True)
        (mine / "hooks" / "guard.py").write_text("import sys\nsys.exit(0)\n")
        (mine / "module.toml").write_text('description = "Mine"\n\n[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = []\n')
        engine, report = a.sync()
        self.assertEqual(report.errors, [])
        from agent_kit_sync.plan import hook_command
        command = hook_command(a.layout, engine.machine, "mine", "guard")
        path = a.path(".claude", "CLAUDE.md")
        a.write(path, path.read_text().replace("Write plainly.", f"Write plainly. Check with `{command}` first."))
        _engine, report = a.sync()
        self.assertEqual(len(report.held), 1)
        source = (a.layout.config / "modules/writing/sections/plain.md").read_text()
        self.assertNotIn("kit.hook", source)
        self.assertNotIn("run-hook", source)
        _engine, report = b.sync()
        self.assertEqual(report.errors, [])
        self.assertIn("Write plainly.", b.read(".claude", "CLAUDE.md"))

    def test_typed_block_in_a_copy_with_local_text_is_held(self):
        self.standard_config()
        a = self.machine("a")
        extra = a.layout.root / "local" / "helper-extra.md"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("Only on this machine.\n")
        data = a.machine_data()
        data.setdefault("overrides", {})["writing/helper"] = {"kind": "append", "file": str(extra)}
        a.write_machine(data)
        a.sync()
        path = a.path(".claude", "agents", "helper.md")
        a.write(path, path.read_text().replace("Help with the task.\n", "Help with the task.\n{{kit.only codex}}\nSecret.\n{{kit.end}}\n"))
        _engine, report = a.sync()
        self.assertEqual(len(report.held), 1)
        self.assertNotIn("Secret.", (a.layout.config / "modules/writing/agents/helper.md").read_text())
        self.assertNotIn("Secret.", a.read(".codex", "agents", "helper.toml"))


    def test_header_edit_codex_cannot_hold_is_held(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "agents", "helper.md")
        a.write(path, path.read_text().replace("description: Helps\n", "description: >\n  Helps with reviews.\n"))
        _engine, report = a.sync()
        self.assertEqual(len(report.held), 1)
        self.assertIn("description: Helps\n", (a.layout.config / "modules/writing/agents/helper.md").read_text())


    def test_header_check_uses_only_the_modules_apps(self):
        self.standard_config()
        manifest = self.seed / "modules/writing/module.toml"
        self.seed_files({"modules/writing/module.toml": 'apps = ["claude", "cursor"]\n' + manifest.read_text()}, "claude and cursor only")
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "agents", "helper.md")
        a.write(path, path.read_text().replace("description: Helps\n", "description: >\n  Helps with reviews.\n"))
        _engine, report = a.sync()
        self.assertEqual(report.held, [])
        self.assertIn("  Helps with reviews.\n", (a.layout.config / "modules/writing/agents/helper.md").read_text())


if __name__ == "__main__":
    unittest.main()
