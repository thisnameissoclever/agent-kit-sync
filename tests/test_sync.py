"""Sync between apps on one machine and between machines."""

import json
import os
import unittest
from pathlib import Path

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
                tests = ["hooks/test_guard.py"]
            """,
            "modules/guarded/hooks/guard.py": "import sys\nsys.exit(0)\n",
            "modules/guarded/hooks/test_guard.py": "import sys\nsys.exit(0)\n",
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
    def test_disabled_item_removed_from_manifest_closes_its_old_request(self):
        self.standard_config()
        machine = self.machine("a", policy={"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "ask"})
        engine, _ = machine.sync()
        self.assertTrue([e for e in engine.held.all() if e.get("item_key") == "writing/review"])
        manifest = self.seed / "modules/writing/module.toml"
        text = manifest.read_text(encoding="utf-8")
        self.seed_files({"modules/writing/module.toml": text.replace('[[skills]]\nname = "review"\ndir = "skills/review"\n', '')}, "Remove review from inventory")
        engine, _ = machine.sync()
        self.assertTrue([e for e in engine.held.all() if e.get("item_key") == "writing/review"])
        self.assertNotIn('name = "review"', (machine.layout.config / "modules/writing/module.toml").read_text(encoding="utf-8"))
        data = machine.machine_data()
        data["overrides"] = {"writing/review": {"kind": "disable"}}
        machine.write_machine(data)
        engine, report = machine.sync()
        self.assertFalse(report.errors)
        self.assertFalse([e for e in engine.held.all() if e.get("item_key") == "writing/review"])
        self.assertFalse([a for a in engine.alerts.open() if a.get("key") == "incoming:writing/review"])

    def test_disabled_referenced_hook_keeps_render_inventory_and_is_removed(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guarded"\n',
            "modules/guarded/module.toml": 'description = "Referenced hook"\n\n[[sections]]\nname = "rule"\nfile = "rule.md"\n\n[[hooks]]\nname = "guard"\nfile = "guard.py"\nevents = ["pre_tool_use"]\ntests = ["test_guard.py"]\n',
            "modules/guarded/rule.md": 'Hook command: {{kit.hook guarded/guard}}.\n',
            "modules/guarded/guard.py": 'import sys\nsys.exit(0)\n',
            "modules/guarded/test_guard.py": 'import sys\nsys.exit(0)\n',
        })
        machine = self.machine("a")
        engine, report = machine.sync()
        self.assertFalse(report.errors)
        self.assertTrue(engine.index.hooks)
        data = machine.machine_data()
        data["overrides"] = {"guarded/guard": {"kind": "disable"}}
        machine.write_machine(data)
        engine, report = machine.sync()
        self.assertFalse(report.errors)
        self.assertEqual(engine.index.hooks, [])
        self.assertFalse((machine.layout.installed / "guarded/guard.py").exists())
        self.assertIn("run-hook guarded/guard", machine.read(".claude", "CLAUDE.md"))

    def test_declined_new_item_stays_quiet_until_enabled(self):
        self.standard_config()
        machine = self.machine("a", policy={"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "ask"})
        engine, _ = machine.sync()
        entry = next(e for e in engine.held.all() if e.get("item_key") == "writing/review")
        from agent_kit_sync.resolve import resolve
        with machine.env():
            self.assertEqual(resolve(machine.layout, entry["id"], "decline"), 0)
        for _ in range(2):
            engine, report = machine.sync()
            self.assertFalse(report.errors)
            self.assertFalse([e for e in engine.held.all() if e.get("item_key") == "writing/review"])
            self.assertFalse([a for a in engine.alerts.open() if a.get("key") == "incoming:writing/review"])
            self.assertFalse(machine.path(".agents", "skills", "review").exists())
        data = machine.machine_data()
        del data["overrides"]["writing/review"]
        machine.write_machine(data)
        engine, _ = machine.sync()
        self.assertTrue([e for e in engine.held.all() if e.get("item_key") == "writing/review"])
        self.assertFalse(machine.path(".agents", "skills", "review").exists())

    def test_disabling_module_clears_only_its_incoming_holds(self):
        self.standard_config()
        machine = self.machine("a", policy={"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "ask"})
        engine, _ = machine.sync()
        engine.held.add("edit:writing/review", "outgoing", "Saved local edit", ["approve", "discard"], {"0": b"saved"}, item_key="writing/review")
        data = machine.machine_data()
        (machine.layout.root / "unused.md").write_text("Local addition.\n", encoding="utf-8")
        data["overrides"] = {"writing": {"kind": "disable"}, "writing/review": {"kind": "append", "file": "unused.md"}}
        machine.write_machine(data)
        engine, report = machine.sync()
        self.assertFalse(report.errors)
        self.assertEqual([e["kind"] for e in engine.held.all()], ["outgoing"])
        self.assertFalse([a for a in engine.alerts.open() if a.get("key", "").startswith("incoming:writing/")])
        self.assertFalse(machine.path(".agents", "skills", "review").exists())

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

    def test_an_item_this_machine_adds_is_not_held_here(self):
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.machine("a", policy=ask)
        b = self.machine("b", policy=ask)
        engine, _report = a.sync()
        for entry in engine.held.all():
            with a.env():
                from agent_kit_sync.resolve import resolve
                resolve(a.layout, entry["id"], "apply")
        a.sync()
        from agent_kit_sync.commands import cmd_remember
        from types import SimpleNamespace
        with a.env():
            self.assertEqual(cmd_remember(SimpleNamespace(text="Keep replies short."), a.layout), 0)
        engine, _report = a.sync()
        self.assertEqual([e["item_key"] for e in engine.held.all()], [])
        self.assertIn("Keep replies short.", a.read(".claude", "CLAUDE.md"))
        # Another machine still asks before installing it.
        engine, _report = b.sync()
        self.assertIn("shared-memory/entries", [e.get("item_key") for e in engine.held.all()])

    def test_a_module_this_machine_lists_after_committing_its_files_is_not_held_here(self):
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.machine("a", policy=ask)
        b = self.machine("b", policy=ask)
        engine, _report = a.sync()
        from agent_kit_sync.resolve import resolve
        for entry in engine.held.all():
            with a.env():
                resolve(a.layout, entry["id"], "apply")
        a.sync()
        config = a.layout.config
        (config / "modules/extra/sections").mkdir(parents=True)
        (config / "modules/extra/module.toml").write_text('description = "x"\n\n[[sections]]\nname = "s"\nfile = "sections/s.md"\n', encoding="utf-8")
        (config / "modules/extra/sections/s.md").write_text("Extra rule.\n", encoding="utf-8")
        a.sync()  # The module's files are committed before kit.toml lists it.
        kit = config / "kit.toml"
        kit.write_text(kit.read_text(encoding="utf-8") + '\n[[modules]]\nname = "extra"\n', encoding="utf-8")
        engine, _report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        self.assertIn("Extra rule.", a.read(".claude", "CLAUDE.md"))
        # Another machine still asks before installing it.
        engine, _report = b.sync()
        self.assertIn("extra/s", [e.get("item_key") for e in engine.held.all()])

    def approved_machine(self, name, **kwargs):
        machine = self.machine(name, **kwargs)
        engine, _report = machine.sync()
        from agent_kit_sync.resolve import resolve
        for entry in engine.held.all():
            with machine.env():
                resolve(machine.layout, entry["id"], "apply")
        machine.sync()
        return machine

    def test_a_section_file_committed_before_its_manifest_entry_is_not_held_here(self):
        # An agent writes a new section file, a sync commits it, and the agent lists it in
        # module.toml seconds later, so the next sync commits the entry on its own.
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.approved_machine("a", policy=ask)
        b = self.approved_machine("b", policy=ask)
        config = a.layout.config
        (config / "modules/writing/sections/later.md").write_text("Later rule.\n", encoding="utf-8")
        engine, _report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        manifest = config / "modules/writing/module.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + '\n[[sections]]\nname = "later"\nfile = "sections/later.md"\n', encoding="utf-8")
        engine, _report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        self.assertEqual([a for a in engine.alerts.open() if a["key"].startswith("incoming:")], [])
        self.assertIn("Later rule.", a.read(".claude", "CLAUDE.md"))
        # The file is tracked through its item now, so the separate record of it is gone.
        from agent_kit_sync.state import Index
        self.assertNotIn("modules/writing/sections/later.md", Index(a.layout).own_files)
        # Another machine still asks before installing it.
        engine, _report = b.sync()
        self.assertIn("writing/later", [e.get("item_key") for e in engine.held.all()])

    def test_another_machines_edit_to_a_file_committed_here_before_listing_is_held(self):
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.approved_machine("a", policy=ask)
        b = self.approved_machine("b")
        (a.layout.config / "modules/writing/sections/later.md").write_text("Later rule.\n", encoding="utf-8")
        a.sync()
        b.sync()
        (b.layout.config / "modules/writing/sections/later.md").write_text("Changed by b.\n", encoding="utf-8")
        b.sync()
        a.sync()
        manifest = a.layout.config / "modules/writing/module.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + '\n[[sections]]\nname = "later"\nfile = "sections/later.md"\n', encoding="utf-8")
        engine, _report = a.sync()
        self.assertIn("writing/later", [e.get("item_key") for e in engine.held.all()])
        self.assertNotIn("Changed by b.", a.read(".claude", "CLAUDE.md"))

    def test_a_file_another_machine_wrote_and_this_machine_edited_is_held_when_listed(self):
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.approved_machine("a", policy=ask)
        b = self.approved_machine("b")
        (b.layout.config / "modules/writing/sections/later.md").write_text("Written by b.\n", encoding="utf-8")
        b.sync()
        a.sync()
        path = a.layout.config / "modules/writing/sections/later.md"
        path.write_text(path.read_text(encoding="utf-8") + "Added by a.\n", encoding="utf-8")
        a.sync()
        from agent_kit_sync.state import Index
        self.assertNotIn("modules/writing/sections/later.md", Index(a.layout).own_files)
        manifest = a.layout.config / "modules/writing/module.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + '\n[[sections]]\nname = "later"\nfile = "sections/later.md"\n', encoding="utf-8")
        engine, _report = a.sync()
        self.assertIn("writing/later", [e.get("item_key") for e in engine.held.all()])

    def test_a_held_alert_names_the_item_and_what_to_do(self):
        self.standard_config()
        a = self.machine("a", policy={"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"})
        engine, _report = a.sync()
        alert = next(x for x in engine.alerts.open() if x["key"] == "incoming:writing/plain")
        self.assertIn("before it installs writing/plain, a new item from your config repo.", alert["message"])
        self.assertIn("`agent-kit-sync held`", alert["message"])

    def extra_module(self, machine, hook=False):
        """Write a module into the checkout with CRLF line breaks, as a Windows editor does; Git stores them as LF."""
        config = machine.layout.config
        (config / "modules/extra/sections").mkdir(parents=True, exist_ok=True)
        manifest = 'description = "x"\n\n[[sections]]\nname = "s"\nfile = "sections/s.md"\n'
        if hook:
            manifest += '\n[[hooks]]\nname = "h"\nfile = "hooks/h.py"\nevents = ["stop"]\ntests = ["hooks/test_h.py"]\n'
            (config / "modules/extra/hooks").mkdir(parents=True, exist_ok=True)
            (config / "modules/extra/hooks/h.py").write_bytes("import sys\nsys.exit(0)\n".replace("\n", "\r\n").encode("utf-8"))
            (config / "modules/extra/hooks/test_h.py").write_bytes("import sys\nsys.exit(0)\n".replace("\n", "\r\n").encode("utf-8"))
        (config / "modules/extra/module.toml").write_bytes(manifest.replace("\n", "\r\n").encode("utf-8"))
        (config / "modules/extra/sections/s.md").write_bytes("Extra rule.\n".replace("\n", "\r\n").encode("utf-8"))

    def list_extra(self, machine):
        kit = machine.layout.config / "kit.toml"
        kit.write_text(kit.read_text(encoding="utf-8") + '\n[[modules]]\nname = "extra"\n', encoding="utf-8")

    def test_a_hook_in_a_module_listed_later_installs_here_without_asking(self):
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.machine("a", policy=ask)
        engine, _report = a.sync()
        from agent_kit_sync.resolve import resolve
        for entry in engine.held.all():
            with a.env():
                resolve(a.layout, entry["id"], "apply")
        a.sync()
        self.extra_module(a, hook=True)
        a.sync()
        self.list_extra(a)
        engine, _report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        self.assertTrue((a.layout.installed / "extra" / "hooks" / "h.py").exists())

    def test_another_machines_edit_before_listing_is_held_here(self):
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.machine("a", policy=ask)
        b = self.machine("b")
        engine, _report = a.sync()
        from agent_kit_sync.resolve import resolve
        for entry in engine.held.all():
            with a.env():
                resolve(a.layout, entry["id"], "apply")
        a.sync()
        self.extra_module(a)
        a.sync()
        b.sync()
        (b.layout.config / "modules/extra/sections/s.md").write_text("Changed by b.\n", encoding="utf-8")
        b.sync()
        self.list_extra(a)
        engine, _report = a.sync()
        self.assertIn("extra/s", [e.get("item_key") for e in engine.held.all()])
        self.assertNotIn("Changed by b.", a.read(".claude", "CLAUDE.md"))
        # a's own version stayed approved; only b's change is waiting.
        from agent_kit_sync.state import Index
        self.assertTrue(Index(a.layout).approvals.get("extra/s"))

    def test_files_another_machine_committed_are_held_when_this_machine_lists_them(self):
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.machine("a", policy=ask)
        b = self.machine("b")
        engine, _report = a.sync()
        from agent_kit_sync.resolve import resolve
        for entry in engine.held.all():
            with a.env():
                resolve(a.layout, entry["id"], "apply")
        a.sync()
        b.sync()
        self.extra_module(b)
        b.sync()
        a.sync()
        self.list_extra(a)
        engine, _report = a.sync()
        self.assertIn("extra/s", [e.get("item_key") for e in engine.held.all()])

    def test_a_manifest_written_here_for_files_another_machine_committed_is_held(self):
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.machine("a", policy=ask)
        b = self.machine("b")
        engine, _report = a.sync()
        from agent_kit_sync.resolve import resolve
        for entry in engine.held.all():
            with a.env():
                resolve(a.layout, entry["id"], "apply")
        a.sync()
        b.sync()
        (b.layout.config / "modules/extra/sections").mkdir(parents=True)
        (b.layout.config / "modules/extra/sections/s.md").write_text("Written by b.\n", encoding="utf-8")
        b.sync()  # b's files are committed with no module.toml.
        a.sync()
        (a.layout.config / "modules/extra/module.toml").write_text('description = "x"\n\n[[sections]]\nname = "s"\nfile = "sections/s.md"\n', encoding="utf-8")
        self.list_extra(a)
        engine, _report = a.sync()
        self.assertIn("extra/s", [e.get("item_key") for e in engine.held.all()])
        self.assertNotIn("Written by b.", a.read(".claude", "CLAUDE.md"))

    def test_a_module_listed_from_another_machine_is_held_here(self):
        self.standard_config()
        ask = {"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.machine("a")
        b = self.machine("b", policy=ask)
        a.sync()
        config = a.layout.config
        (config / "modules/extra/sections").mkdir(parents=True)
        (config / "modules/extra/module.toml").write_text('description = "x"\n\n[[sections]]\nname = "s"\nfile = "sections/s.md"\n', encoding="utf-8")
        (config / "modules/extra/sections/s.md").write_text("Extra rule.\n", encoding="utf-8")
        kit = config / "kit.toml"
        kit.write_text(kit.read_text(encoding="utf-8") + '\n[[modules]]\nname = "extra"\n', encoding="utf-8")
        a.sync()
        # b pulls a's module, then commits its own unrelated file in that module; a's items do not become b's.
        engine, _report = b.sync()
        (b.layout.config / "modules/extra/notes.txt").write_text("b's note\n", encoding="utf-8")
        engine, _report = b.sync()
        self.assertIn("extra/s", [e.get("item_key") for e in engine.held.all()])
        self.assertNotIn("Extra rule.", b.read(".claude", "CLAUDE.md"))

    def test_own_commit_does_not_approve_another_machines_held_change(self):
        self.standard_config()
        ask = {"new_items": "apply", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.machine("a")
        b = self.machine("b", policy=ask)
        a.sync()
        b.sync()
        a.write(a.path(".claude", "skills", "review", "SKILL.md"), a.read(".claude", "skills", "review", "SKILL.md").replace("report problems", "report each problem"))
        a.sync()
        engine, _report = b.sync()
        self.assertIn("writing/review", [e.get("item_key") for e in engine.held.all()])
        # b now edits a different item of the same module; its commit must not approve a's change.
        b.write(b.path(".claude", "agents", "helper.md"), b.read(".claude", "agents", "helper.md").replace("Help with the task.", "Help with the whole task."))
        engine, _report = b.sync()
        self.assertIn("writing/review", [e.get("item_key") for e in engine.held.all()])
        self.assertNotIn("report each problem", b.read(".claude", "skills", "review", "SKILL.md"))

    def test_repairing_a_broken_module_does_not_approve_a_held_change(self):
        self.standard_config()
        ask = {"new_items": "apply", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"}
        a = self.machine("a")
        b = self.machine("b", policy=ask)
        a.sync()
        b.sync()
        a.write(a.path(".claude", "skills", "review", "SKILL.md"), a.read(".claude", "skills", "review", "SKILL.md").replace("report problems", "report each problem"))
        a.sync()
        # Another machine then breaks the module's manifest.
        manifest = a.layout.config / "modules/writing/module.toml"
        good = manifest.read_text()
        manifest.write_text(good + "\n[[skills]]\nname = \"gone\"\ndir = \"skills/gone\"\n")
        git(a.layout.config, "commit", "-qam", "break")
        git(a.layout.config, "push", "-q")
        engine, report = b.sync()
        self.assertTrue([x for x in engine.alerts.open() if x["key"] == "config"], report.errors)
        # This machine repairs it by hand in its checkout.
        (b.layout.config / "modules/writing/module.toml").write_text(good)
        engine, _report = b.sync()
        self.assertNotIn("report each problem", b.read(".claude", "skills", "review", "SKILL.md"))
        self.assertIn("writing/review", [e.get("item_key") for e in engine.held.all()])

    def test_incoming_code_change_is_held_and_never_installed(self):
        self.standard_config()
        self.seed_files({
            "modules/writing/module.toml": (self.seed / "modules/writing/module.toml").read_text() + '\n[[hooks]]\nname = "check"\nfile = "hooks/check.py"\nevents = ["stop"]\ntests = ["hooks/test_check.py"]\n',
            "modules/writing/hooks/test_check.py": "import sys\nsys.exit(0)\n",
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

    def test_support_file_templates_get_this_machines_values_and_carry_back(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "rules"\n',
            "modules/rules/module.toml": """
                [[files]]
                name = "guide"
                path = "guide.md"
                templates = true

                [[files]]
                name = "docs"
                path = "docs"
                templates = ["index.md"]
            """,
            "modules/rules/guide.md": "Read {{kit.projects}}/notes.md first.\n",
            "modules/rules/docs/index.md": "See {{kit.module}}/guide.md.\n",
            "modules/rules/docs/raw.md": "Keep {{kit.projects}} as typed.\n",
        })
        a = self.machine("a")
        a.sync()
        guide = a.layout.installed / "rules" / "guide.md"
        self.assertEqual(guide.read_text(), f"Read {a.home / 'Projects'}/notes.md first.\n")
        self.assertEqual((a.layout.installed / "rules" / "docs" / "index.md").read_text(), f"See {a.layout.installed / 'rules'}/guide.md.\n")
        self.assertEqual((a.layout.installed / "rules" / "docs" / "raw.md").read_text(), "Keep {{kit.projects}} as typed.\n")
        a.write(guide, guide.read_text().replace("first.", "before you start."))
        a.sync()
        self.assertEqual((a.layout.config / "modules/rules/guide.md").read_text(), "Read {{kit.projects}}/notes.md before you start.\n")
        b = self.machine("b")
        b.sync()
        self.assertEqual((b.layout.installed / "rules" / "guide.md").read_text(), f"Read {b.home / 'Projects'}/notes.md before you start.\n")

    def test_app_placeholder_in_a_support_file_is_refused(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "rules"\n',
            "modules/rules/module.toml": '[[files]]\nname = "guide"\npath = "guide.md"\ntemplates = true\n',
            "modules/rules/guide.md": "For {{kit.app}} only.\n",
        })
        _engine, report = self.machine("a").sync()
        self.assertTrue(any("one copy serves every app" in e for e in report.errors), report.errors)

    def test_template_lists_are_checked(self):
        for files, manifest, expected in (
            ({"modules/rules/docs/index.md": "x\n"}, '[[files]]\nname = "docs"\npath = "docs"\ntemplates = true\n', "is a folder"),
            ({"modules/rules/docs/index.md": "x\n"}, '[[files]]\nname = "docs"\npath = "docs"\ntemplates = ["nope.md"]\n', "lists template nope.md"),
            ({"modules/rules/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\nx\n"}, '[[skills]]\nname = "s"\ndir = "skills/s"\ntemplates = ["SKILL.mdx"]\n', "lists template SKILL.mdx"),
        ):
            with self.subTest(expected=expected):
                self.tearDown()
                self.setUp()
                self.seed_files(dict(files, **{"kit.toml": 'format = 1\n\n[[modules]]\nname = "rules"\n', "modules/rules/module.toml": manifest}))
                _engine, report = self.machine("a").sync()
                self.assertTrue(any(expected in e for e in report.errors), report.errors)

    def test_templates_on_a_file_must_be_a_list_or_true(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "rules"\n',
            "modules/rules/module.toml": '[[files]]\nname = "guide"\npath = "guide.md"\ntemplates = "guide.md"\n',
            "modules/rules/guide.md": "Text.\n",
        })
        a = self.machine("a")
        _engine, report = a.sync()
        self.assertTrue(any("templates" in e for e in report.errors), report.errors)

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


class DefaultFolderTests(TwoMachines):
    def drop_folders(self, machine):
        data = machine.machine_data()
        data.pop("projects", None)
        data.pop("worktrees", None)
        machine.write_machine(data)

    def test_a_machine_with_no_projects_folder_uses_the_kit_default(self):
        self.standard_config()
        kit = self.seed / "kit.toml"
        self.seed_files({"kit.toml": kit.read_text(encoding="utf-8") + '\n[defaults]\nprojects_folder_name = "Code"\nworktrees_folder_name = { macos = "_wt", linux = "_wt", windows = ".wt" }\n'}, "defaults")
        a = self.machine("a")
        self.drop_folders(a)
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        self.assertIn("Keep projects in " + str(a.home / "Code") + ".", a.read(".claude", "CLAUDE.md"))
        with a.env():
            from agent_kit_sync.model import load_machine
            machine = load_machine(a.layout)
        self.assertEqual(machine.worktrees, a.home / "Code" / (".wt" if os.name == "nt" else "_wt"))
        self.assertEqual(machine.defaulted, ["projects", "worktrees"])

    def test_without_kit_defaults_the_folder_is_projects_in_the_home_folder(self):
        self.standard_config()
        a = self.machine("a")
        self.drop_folders(a)
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        self.assertIn("Keep projects in " + str(a.home / "Projects") + ".", a.read(".claude", "CLAUDE.md"))

    def test_a_folder_the_machine_file_sets_is_kept_and_not_reported(self):
        self.standard_config()
        a = self.machine("a")
        with a.env():
            from agent_kit_sync.model import load_machine
            machine = load_machine(a.layout)
        self.assertEqual((machine.projects, machine.defaulted), (a.home / "Projects", []))

    def test_a_default_name_with_a_path_in_it_is_ignored(self):
        self.standard_config()
        kit = self.seed / "kit.toml"
        self.seed_files({"kit.toml": kit.read_text(encoding="utf-8") + '\n[defaults]\nprojects_folder_name = "../elsewhere"\n'}, "defaults")
        a = self.machine("a")
        self.drop_folders(a)
        a.sync()
        with a.env():
            from agent_kit_sync.model import load_machine
            machine = load_machine(a.layout)
        self.assertEqual(machine.projects, a.home / "Projects")

    def test_a_defaulted_worktrees_folder_goes_inside_the_projects_folder_the_machine_file_sets(self):
        self.standard_config()
        a = self.machine("a")
        data = a.machine_data()
        data["projects"] = str(a.home / "elsewhere" / "code")
        data.pop("worktrees", None)
        a.write_machine(data)
        with a.env():
            from agent_kit_sync.model import load_machine
            machine = load_machine(a.layout)
        self.assertEqual(machine.worktrees, a.home / "elsewhere" / "code" / (".worktrees" if os.name == "nt" else "_worktrees"))
        self.assertEqual(machine.defaulted, ["worktrees"])

    def test_defaults_that_are_not_a_table_do_not_stop_the_machine_loading(self):
        self.standard_config()
        kit = self.seed / "kit.toml"
        self.seed_files({"kit.toml": "defaults = 5\n" + kit.read_text(encoding="utf-8")}, "bad defaults")
        a = self.machine("a")
        self.drop_folders(a)
        with a.env():
            from agent_kit_sync.model import load_machine
            machine = load_machine(a.layout)
        self.assertEqual(machine.projects, a.home / "Projects")

    def test_folder_names_that_fail_on_some_system_are_refused(self):
        from agent_kit_sync.model import folder_name
        for bad in ("../x", "a\\b", "C:", "a:b", "CON", "nul.txt", "foo.", "foo ", " foo", "x\ny", "", ".", 7, None, {"nosuch": "Code"}):
            with self.subTest(name=bad):
                self.assertIsNone(folder_name(bad))
        for good in ("Code", "my projects", "_worktrees", ".worktrees", "Projects-2026"):
            with self.subTest(name=good):
                self.assertEqual(folder_name(good), good)

    def test_an_edit_is_not_carried_back_into_a_defaulted_folder_placeholder(self):
        self.standard_config()
        a = self.machine("a")
        self.drop_folders(a)
        a.sync()
        path = a.path(".claude", "CLAUDE.md")
        typed = "See " + str(a.home / "Projects" / "notes.md") + " too."
        a.write(path, path.read_text(encoding="utf-8").replace("Write plainly.", "Write plainly.\n" + typed))
        a.sync()
        source = (a.layout.config / "modules/writing/sections/plain.md").read_text(encoding="utf-8")
        # The typed path is under the home folder, so it becomes {{kit.home}}, never the defaulted
        # {{kit.projects}}; the line that already used the placeholder keeps it.
        self.assertIn("See {{kit.home}}" + os.sep + "Projects" + os.sep + "notes.md too.", source)
        self.assertNotIn("See {{kit.projects}}", source)
        self.assertIn("Keep projects in {{kit.projects}}.", source)

    def test_doctor_says_which_folder_came_from_the_default(self):
        self.standard_config()
        a = self.machine("a")
        self.drop_folders(a)
        a.sync()
        import contextlib, io
        from types import SimpleNamespace
        from agent_kit_sync.commands import cmd_doctor
        out = io.StringIO()
        with a.env(), contextlib.redirect_stdout(out):
            cmd_doctor(SimpleNamespace(), a.layout)
        self.assertIn("sets no projects folder", out.getvalue())
        self.assertIn("does not exist on this machine", out.getvalue())

    def test_doctor_reports_a_kit_default_it_ignored(self):
        self.standard_config()
        kit = self.seed / "kit.toml"
        self.seed_files({"kit.toml": kit.read_text(encoding="utf-8") + '\n[defaults]\nprojects_folder_name = "CON"\n'}, "bad name")
        a = self.machine("a")
        a.sync()
        import contextlib, io
        from types import SimpleNamespace
        from agent_kit_sync.commands import cmd_doctor
        out = io.StringIO()
        with a.env(), contextlib.redirect_stdout(out):
            cmd_doctor(SimpleNamespace(), a.layout)
        self.assertIn("defaults.projects_folder_name", out.getvalue())


class HookArgumentTests(TwoMachines):
    def test_hook_args_get_this_machines_values_and_kit_app_is_left_for_the_dispatcher(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n',
            "modules/guards/module.toml": '[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\nargs = ["--app", "{{kit.app}}", "--home", "{{kit.home}}"]\ntests = ["hooks/test_guard.py"]\n',
            "modules/guards/hooks/guard.py": "import sys\nsys.exit(0)\n",
            "modules/guards/hooks/test_guard.py": "import sys\nsys.exit(0)\n",
        })
        a = self.machine("a")
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        index = json.loads((a.layout.state / "index.json").read_text(encoding="utf-8"))
        args = next(h["args"] for h in index["hooks"] if h["name"] == "guard")
        from agent_kit_sync import dispatch
        self.assertEqual(args, ["--app", dispatch.APP_ARG, "--home", str(a.home)])
        self.assertEqual(dispatch.hook_args({"args": args}, "cursor"), ["--app", "cursor", "--home", str(a.home)])

    def test_dispatcher_puts_the_calling_app_into_kit_app(self):
        from agent_kit_sync import dispatch
        import os, sys, tempfile
        folder = Path(tempfile.mkdtemp(dir=self.tmp))
        script = folder / "echo_args.py"
        script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        hook = {"module": "m", "name": "h", "path": str(script), "runtime": "python", "args": ["--app", dispatch.APP_ARG], "timeout": 30}
        for env_app, expected in (("cursor", "cursor"), ("", "codex")):
            env = dict(os.environ, AGENT_KIT_SYNC_APP=env_app)
            result = dispatch.run_one(hook, "codex", "stop", b"{}", {}, env)
            self.assertEqual(json.loads(result["stdout"]), ["--app", expected], env_app)

    def test_kit_literal_app_reaches_the_hook_as_text(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n',
            "modules/guards/module.toml": '[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\nargs = ["{{kit.literal app}}", "{{kit.app}}"]\ntests = ["hooks/test_guard.py"]\n',
            "modules/guards/hooks/guard.py": "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
            "modules/guards/hooks/test_guard.py": "import sys\nsys.exit(0)\n",
        })
        a = self.machine("a")
        a.sync()
        from agent_kit_sync import dispatch
        import os
        index = json.loads((a.layout.state / "index.json").read_text(encoding="utf-8"))
        hook = next(h for h in index["hooks"] if h["name"] == "guard")
        result = dispatch.run_one(hook, "claude", "stop", b"{}", {}, dict(os.environ, AGENT_KIT_SYNC_APP="codex"))
        self.assertEqual(json.loads(result["stdout"]), ["{{kit.app}}", "codex"])

    def test_a_bad_placeholder_in_args_names_the_hook(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n',
            "modules/guards/module.toml": '[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\nargs = ["{{kit.nope}}"]\ntests = ["hooks/test_guard.py"]\n',
            "modules/guards/hooks/guard.py": "import sys\nsys.exit(0)\n",
            "modules/guards/hooks/test_guard.py": "import sys\nsys.exit(0)\n",
        })
        a = self.machine("a")
        _engine, report = a.sync()
        self.assertTrue(any("hook guard args" in error and "kit.nope" in error for error in report.errors), report.errors)


class HookTestRequirementTests(TwoMachines):
    """Every synced hook has tests, and each machine runs them before installing it."""

    def hook_config(self, tests='tests = ["hooks/test_guard.py"]\n', test_body="import sys\nsys.exit(0)\n", extra=""):
        files = {
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n',
            "modules/guards/module.toml": f'[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\n{tests}{extra}',
            "modules/guards/hooks/guard.py": "import sys\nsys.exit(0)\n",
        }
        if test_body is not None:
            files["modules/guards/hooks/test_guard.py"] = test_body
        self.seed_files(files)

    def installed(self, machine):
        return (machine.layout.installed / "guards" / "hooks" / "guard.py").exists()

    def alert_messages(self, engine):
        return [alert["message"] for alert in engine.alerts.open()]

    def test_new_hook_with_passing_tests_installs(self):
        self.hook_config()
        a = self.machine("a")
        engine, report = a.sync()
        self.assertEqual(report.errors, [])
        self.assertTrue(self.installed(a))
        self.assertFalse([m for m in self.alert_messages(engine) if "guards/guard" in m])

    def test_new_hook_without_tests_is_not_installed(self):
        self.hook_config(tests="", test_body=None)
        a = self.machine("a")
        engine, _report = a.sync()
        self.assertFalse(self.installed(a))
        self.assertTrue([m for m in self.alert_messages(engine) if "guards/guard has no tests" in m])

    def test_new_hook_whose_tests_fail_is_not_installed(self):
        self.hook_config(test_body="import sys\nprint('broken on this machine')\nsys.exit(1)\n")
        a = self.machine("a")
        engine, _report = a.sync()
        self.assertFalse(self.installed(a))
        self.assertTrue([m for m in self.alert_messages(engine) if "failed its tests here" in m and "broken on this machine" in m])

    def test_hook_whose_test_cannot_run_is_not_installed(self):
        self.hook_config(tests='tests = ["hooks/test_guard.rb"]\n', test_body=None, extra="")
        self.seed_files({"modules/guards/hooks/test_guard.rb": "exit 0\n"}, "ruby test")
        a = self.machine("a")
        engine, _report = a.sync()
        self.assertFalse(self.installed(a))
        self.assertTrue([m for m in self.alert_messages(engine) if "did not run" in m])

    def test_fixed_hook_installs_and_closes_its_alert(self):
        self.hook_config(test_body="import sys\nsys.exit(1)\n")
        a = self.machine("a")
        a.sync()
        self.seed_files({"modules/guards/hooks/test_guard.py": "import sys\nsys.exit(0)\n"}, "fix test")
        engine, _report = a.sync()
        self.assertTrue(self.installed(a))
        self.assertFalse([m for m in self.alert_messages(engine) if "guards/guard" in m])

    def test_edit_to_a_hook_that_breaks_its_tests_is_held(self):
        self.hook_config(test_body="import pathlib, sys\nsys.exit(0 if 'exit(0)' in (pathlib.Path(__file__).parent / 'guard.py').read_text() else 1)\n")
        a = self.machine("a")
        a.sync()
        installed = a.layout.installed / "guards" / "hooks" / "guard.py"
        a.write(installed, "import sys\nsys.exit(3)\n")
        engine, _report = a.sync()
        self.assertTrue([e for e in engine.held.all() if e["kind"] == "edit" and e.get("item_key") == "guards/guard"])
        self.assertIn("exit(0)", (a.layout.config / "modules/guards/hooks/guard.py").read_text())

    def test_hook_added_in_the_checkout_without_tests_is_not_committed(self):
        self.hook_config()
        a = self.machine("a")
        a.sync()
        manifest = a.layout.config / "modules/guards/module.toml"
        manifest.write_text(manifest.read_text() + '\n[[hooks]]\nname = "bare"\nfile = "hooks/bare.py"\nevents = ["stop"]\n')
        (a.layout.config / "modules/guards/hooks/bare.py").write_text("import sys\nsys.exit(0)\n")
        engine, _report = a.sync()
        self.assertNotIn("bare", git(a.layout.config, "show", "HEAD:modules/guards/module.toml"))
        self.assertFalse((a.layout.config / "modules/guards/hooks/bare.py").exists())
        held = [e for e in engine.held.all() if e["kind"] == "checkout-edit"]
        self.assertTrue(held)
        self.assertTrue([m for m in self.alert_messages(engine) if "guards/bare has no tests" in m])
        from agent_kit_sync.resolve import resolve
        with a.env():
            for entry in held:
                self.assertEqual(resolve(a.layout, entry["id"], "discard"), 0)

    def test_failing_hook_sets_back_only_its_own_entry(self):
        self.hook_config()
        a = self.machine("a")
        a.sync()
        manifest = a.layout.config / "modules/guards/module.toml"
        manifest.write_text(manifest.read_text() + '\n[[hooks]]\nname = "bare"\nfile = "hooks/bare.py"\nevents = ["stop"]\n'
                            + '\n[[skills]]\nname = "t"\ndir = "skills/t"\n')
        (a.layout.config / "modules/guards/hooks/bare.py").write_text("import sys\nsys.exit(0)\n")
        (a.layout.config / "modules/guards/skills/t").mkdir(parents=True)
        (a.layout.config / "modules/guards/skills/t/SKILL.md").write_text("---\nname: t\ndescription: d\n---\nKept.\n")
        engine, _report = a.sync()
        committed = git(a.layout.config, "show", "HEAD:modules/guards/module.toml")
        self.assertIn('name = "t"', committed)
        self.assertNotIn('name = "bare"', committed)
        self.assertIn('name = "guard"', committed)
        self.assertEqual(a.read(".claude", "skills", "t", "SKILL.md").strip().splitlines()[-1], "Kept.")
        held = {e["path"]: e for e in engine.held.all() if e["kind"] == "checkout-edit"}
        self.assertIn("modules/guards/module.toml", held)
        self.assertIn('name = "bare"', engine.held.blob(held["modules/guards/module.toml"], "ours").decode())

    def test_use_this_refuses_to_replace_later_edits(self):
        self.hook_config()
        a = self.machine("a")
        a.sync()
        manifest = a.layout.config / "modules/guards/module.toml"
        manifest.write_text(manifest.read_text() + '\n[[hooks]]\nname = "bare"\nfile = "hooks/bare.py"\nevents = ["stop"]\n')
        (a.layout.config / "modules/guards/hooks/bare.py").write_text("import sys\nsys.exit(0)\n")
        engine, _report = a.sync()
        entry = next(e for e in engine.held.all() if e.get("path") == "modules/guards/module.toml")
        later = manifest.read_text() + '\n[[skills]]\nname = "later"\ndir = "skills/later"\n'
        manifest.write_text(later)
        from agent_kit_sync.resolve import resolve
        with a.env():
            self.assertEqual(resolve(a.layout, entry["id"], "use-this"), 1)
        self.assertEqual(manifest.read_text(), later)
        manifest.write_bytes(engine.held.blob(entry, "set-back"))
        with a.env():
            self.assertEqual(resolve(a.layout, entry["id"], "use-this"), 0)
        self.assertIn('name = "bare"', manifest.read_text())

    def test_untested_hook_does_not_block_other_changes_to_its_module(self):
        self.hook_config(tests="", test_body=None, extra='\n[[skills]]\nname = "s"\ndir = "skills/s"\n')
        self.seed_files({"modules/guards/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\nFirst.\n"}, "skill")
        a = self.machine("a")
        a.sync()
        manifest = a.layout.config / "modules/guards/module.toml"
        manifest.write_text(manifest.read_text() + '\n[[skills]]\nname = "t"\ndir = "skills/t"\n')
        (a.layout.config / "modules/guards/skills/t").mkdir(parents=True)
        (a.layout.config / "modules/guards/skills/t/SKILL.md").write_text("---\nname: t\ndescription: d\n---\nSecond.\n")
        engine, _report = a.sync()
        self.assertIn('name = "t"', git(a.layout.config, "show", "HEAD:modules/guards/module.toml"))
        self.assertFalse([e for e in engine.held.all() if e["kind"] == "checkout-edit"])

    def test_script_that_fails_on_windows_is_not_committed(self):
        self.hook_config()
        a = self.machine("a")
        a.sync()
        installed = a.layout.installed / "guards" / "hooks" / "guard.py"
        a.write(installed, "import fcntl, sys\nsys.exit(0)\n")
        engine, _report = a.sync()
        held = [e for e in engine.held.all() if e["kind"] == "edit" and e.get("item_key") == "guards/guard"]
        self.assertTrue(held)
        self.assertIn("fails on Windows", held[0]["summary"])

    def test_module_for_one_system_is_checked_for_that_system_only(self):
        self.hook_config(extra='\n[requires]\nos = ["macos", "linux"]\n')
        a = self.machine("a")
        a.sync()
        with a.env():
            from agent_kit_sync.engine import Engine
            module = next(m for m in Engine(a.layout).load_modules()[1] if m.name == "guards")
            self.assertEqual(Engine.module_systems(module), ("macos", "linux"))
        from agent_kit_sync import checks
        checker = checks.Checker(own_values=[])
        self.assertFalse([f for f in checker.file("modules/guards/hooks/guard.py", b"import fcntl\n", own=True, systems=("macos", "linux")) if f.check == "portability"])

    def test_add_refuses_a_synced_hook_without_tests(self):
        self.hook_config()
        a = self.machine("a")
        a.sync()
        source = a.home / "new_hook.py"
        source.write_text("import sys\nsys.exit(0)\n")
        from agent_kit_sync.cli import main
        with a.env():
            self.assertEqual(main(["add", str(source), "--module", "guards", "--events", "stop"]), 2)
            self.assertFalse((a.layout.config / "modules/guards/hooks/new_hook.py").exists())
            test = a.home / "tests" / "test_new_hook.py"
            test.parent.mkdir()
            test.write_text("import sys\nsys.exit(0)\n")
            self.assertEqual(main(["add", str(source), "--module", "guards", "--events", "stop", "--tests", str(test)]), 0)
        manifest = (a.layout.config / "modules/guards/module.toml").read_text()
        self.assertIn('tests = ["hooks/tests/test_new_hook.py"]', manifest)
        self.assertTrue((a.layout.config / "modules/guards/hooks/tests/test_new_hook.py").is_file())
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        self.assertTrue((a.layout.installed / "guards" / "hooks" / "new_hook.py").exists())


class HookStagingTests(TwoMachines):
    """A hook's tests run against the files this machine installs, not the whole config checkout."""

    ASK = {"new_items": "ask", "code_changes": "apply", "other_changes": "apply", "outgoing": "push"}
    HOOK = "import sys\nfrom helper import ok\nsys.exit(0 if ok() else 1)\n"
    TEST = ("import pathlib, subprocess, sys\n"
            "hook = pathlib.Path(__file__).resolve().parent / 'guard.py'\n"
            "sys.exit(subprocess.run([sys.executable, str(hook)]).returncode)\n")

    def base(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n',
            "modules/guards/module.toml": '[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\ntests = ["hooks/test_guard.py"]\n',
            "modules/guards/hooks/guard.py": "import sys\nsys.exit(0)\n",
            "modules/guards/hooks/test_guard.py": self.TEST,
        })

    def add_helper(self, declare=True):
        manifest = '[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\ntests = ["hooks/test_guard.py"]\n'
        if declare:
            manifest += '\n[[files]]\nname = "helper"\npath = "hooks/helper.py"\n'
        self.seed_files({
            "modules/guards/module.toml": manifest,
            "modules/guards/hooks/guard.py": self.HOOK,
            "modules/guards/hooks/helper.py": "def ok():\n    return True\n",
        }, "hook imports a new helper")

    def installed(self, machine, name):
        return machine.layout.installed / "guards" / "hooks" / name

    def test_changed_hook_waits_while_the_file_it_imports_is_held(self):
        self.base()
        a = self.machine("a", policy=self.ASK)
        engine, _report = a.sync()
        from agent_kit_sync.resolve import resolve
        with a.env():
            for entry in engine.held.all():
                resolve(a.layout, entry["id"], "apply")
        a.sync()
        self.assertIn("sys.exit(0)", self.installed(a, "guard.py").read_text())
        self.add_helper()
        engine, _report = a.sync()
        # The helper is held as a new item, so the changed hook must not install without it.
        self.assertTrue([e for e in engine.held.all() if e.get("item_key") == "guards/helper"])
        self.assertFalse(self.installed(a, "helper.py").exists())
        self.assertNotIn("from helper", self.installed(a, "guard.py").read_text())
        messages = [alert["message"] for alert in engine.alerts.open()]
        self.assertTrue([m for m in messages if "guards/guard failed its tests" in m and "guards/helper" in m])
        # Approving the helper lets both install together, and the hook then runs.
        with a.env():
            for entry in engine.held.all():
                if entry.get("item_key") == "guards/helper":
                    resolve(a.layout, entry["id"], "apply")
        engine, _report = a.sync()
        self.assertTrue(self.installed(a, "helper.py").exists())
        self.assertIn("from helper", self.installed(a, "guard.py").read_text())
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards/guard"])
        import sys
        from agent_kit_sync import processes
        self.assertEqual(processes.run([sys.executable, str(self.installed(a, "guard.py"))]).returncode, 0)

    def test_new_hook_waits_while_the_file_it_imports_is_held(self):
        self.seed_files({"kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n'})
        self.add_helper()
        self.seed_files({"modules/guards/hooks/test_guard.py": self.TEST}, "test")
        a = self.machine("a", policy=self.ASK)
        engine, _report = a.sync()
        from agent_kit_sync.resolve import resolve
        with a.env():
            for entry in engine.held.all():
                if entry.get("item_key") == "guards/guard":
                    resolve(a.layout, entry["id"], "apply")
        engine, _report = a.sync()
        self.assertFalse(self.installed(a, "guard.py").exists())
        self.assertTrue([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards/guard"])

    def test_hook_needing_an_undeclared_file_never_installs(self):
        self.base()
        a = self.machine("a")
        a.sync()
        self.add_helper(declare=False)
        engine, _report = a.sync()
        # helper.py sits in the module folder but no item installs it, so the hook would fail in every app.
        self.assertFalse(self.installed(a, "helper.py").exists())
        self.assertNotIn("from helper", self.installed(a, "guard.py").read_text())
        self.assertTrue([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards/guard"])

    def test_test_command_and_push_check_use_only_declared_files(self):
        self.base()
        a = self.machine("a")
        a.sync()
        config = a.layout.config / "modules/guards/hooks"
        (config / "helper.py").write_text("def ok():\n    return True\n")
        (config / "guard.py").write_text(self.HOOK)
        from agent_kit_sync.testing import run_module_tests
        import contextlib, io
        out = io.StringIO()
        with a.env(), contextlib.redirect_stdout(out):
            code = run_module_tests(a.layout, "guards")
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("failed: guards/hooks/test_guard.py", out.getvalue())
        # An edit to the installed hook that needs the undeclared helper is held, not pushed.
        (config / "guard.py").write_text("import sys\nsys.exit(0)\n")
        a.write(self.installed(a, "guard.py"), self.HOOK)
        engine, _report = a.sync()
        self.assertTrue([e for e in engine.held.all() if e["kind"] == "edit" and e.get("item_key") == "guards/guard"])
        self.assertNotIn("from helper", git(a.layout.config, "show", "HEAD:modules/guards/hooks/guard.py"))


    def test_hook_waits_while_the_file_it_imports_is_disabled_here(self):
        self.base()
        a = self.machine("a")
        a.sync()
        data = a.machine_data()
        data.setdefault("overrides", {})["guards/helper"] = {"kind": "disable"}
        a.write_machine(data)
        self.add_helper()
        engine, _report = a.sync()
        self.assertFalse(self.installed(a, "helper.py").exists())
        self.assertNotIn("from helper", self.installed(a, "guard.py").read_text())
        self.assertTrue([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards/guard"])

    def test_hook_tests_see_a_templated_file_as_installed(self):
        self.base()
        a = self.machine("a")
        a.sync()
        self.seed_files({
            "modules/guards/module.toml": '[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\ntests = ["hooks/test_guard.py"]\n'
                                          '\n[[files]]\nname = "helper"\npath = "hooks/helper.py"\ntemplates = true\n',
            "modules/guards/hooks/guard.py": self.HOOK,
            # The raw file is not valid Python; only the rendered copy is.
            "modules/guards/hooks/helper.py": "HOME = r\"{{kit.home}}\"\ndef ok():\n    return bool(HOME) and '{' not in HOME\n",
        }, "templated helper")
        engine, _report = a.sync()
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards/guard"])
        self.assertIn("from helper", self.installed(a, "guard.py").read_text())
        self.assertNotIn("{{kit.home}}", self.installed(a, "helper.py").read_text())

    def test_a_hook_that_needs_a_failing_hook_stays_back_too(self):
        self.base()
        a = self.machine("a")
        a.sync()
        manifest = ('[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\ntests = ["hooks/test_guard.py"]\n'
                    '\n[[hooks]]\nname = "base"\nfile = "hooks/helper.py"\nevents = ["stop"]\ntests = ["hooks/test_base.py"]\n')
        self.seed_files({
            "modules/guards/module.toml": manifest,
            "modules/guards/hooks/guard.py": self.HOOK,
            "modules/guards/hooks/helper.py": "def ok():\n    return True\n",
            "modules/guards/hooks/test_base.py": "import sys\nsys.exit(1)\n",
        }, "two hooks, the second failing")
        engine, _report = a.sync()
        keys = {alert["key"] for alert in engine.alerts.open()}
        self.assertIn("tests:guards/base", keys)
        self.assertIn("tests:guards/guard", keys)
        self.assertNotIn("from helper", self.installed(a, "guard.py").read_text())

    def test_pushing_a_hook_with_its_new_helper_in_one_edit_works(self):
        self.base()
        a = self.machine("a")
        a.sync()
        config = a.layout.config / "modules/guards"
        (config / "module.toml").write_text((config / "module.toml").read_text() + '\n[[files]]\nname = "helper"\npath = "hooks/helper.py"\n')
        (config / "hooks/helper.py").write_text("def ok():\n    return True\n")
        (config / "hooks/guard.py").write_text(self.HOOK)
        engine, report = a.sync()
        self.assertEqual(report.errors, [])
        self.assertIn("from helper", git(a.layout.config, "show", "origin/main:modules/guards/hooks/guard.py"))
        self.assertTrue(self.installed(a, "helper.py").exists())
        b = self.machine("b")
        engine, _report = b.sync()
        self.assertTrue(self.installed(b, "helper.py").exists())
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"].startswith("tests:")])


    def run_test_command(self, machine):
        from agent_kit_sync.testing import run_module_tests
        import contextlib, io
        out = io.StringIO()
        with machine.env(), contextlib.redirect_stdout(out):
            code = run_module_tests(machine.layout, "guards")
        return code, out.getvalue()

    def test_module_this_machine_skips_is_not_refused(self):
        self.base()
        self.seed_files({"modules/guards/module.toml": '[requires]\ntags = ["elsewhere"]\n\n[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\ntests = ["hooks/test_guard.py"]\n'}, "needs a tag")
        a = self.machine("a")
        engine, _report = a.sync()
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"].startswith("tests:")])
        code, out = self.run_test_command(a)
        self.assertEqual(code, 0, out)
        self.assertIn("skipped: module guards is not installed on this machine", out)

    def test_hook_disabled_here_is_not_refused(self):
        self.base()
        a = self.machine("a")
        a.sync()
        data = a.machine_data()
        data.setdefault("overrides", {})["guards/guard"] = {"kind": "disable"}
        a.write_machine(data)
        self.seed_files({"modules/guards/hooks/guard.py": "import sys\nsys.exit(0)  # changed\n"}, "change the hook")
        engine, _report = a.sync()
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"].startswith("tests:")])
        code, out = self.run_test_command(a)
        self.assertEqual(code, 0, out)
        self.assertIn("skipped: guards/guard is disabled on this machine", out)

    def test_hook_whose_runtime_is_missing_reports_the_runtime(self):
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n',
            "modules/guards/module.toml": '[[hooks]]\nname = "guard"\nfile = "hooks/guard.mjs"\nruntime = "node"\nevents = ["stop"]\ntests = ["hooks/test_guard.mjs"]\n',
            "modules/guards/hooks/guard.mjs": "process.exit(0);\n",
            "modules/guards/hooks/test_guard.mjs": "process.exit(0);\n",
        })
        a = self.machine("a")
        from unittest import mock
        import shutil as real_shutil
        which = real_shutil.which
        with mock.patch("shutil.which", lambda name, *args, **kwargs: None if name == "node" else which(name, *args, **kwargs)):
            engine, _report = a.sync()
        messages = [alert["message"] for alert in engine.alerts.open() if alert["key"] == "tests:guards/guard"]
        self.assertTrue(messages)
        self.assertIn("no node", messages[0])

    def test_edit_needing_a_file_held_here_is_not_pushed(self):
        self.base()
        a = self.machine("a", policy=self.ASK)
        engine, _report = a.sync()
        from agent_kit_sync.resolve import resolve
        with a.env():
            for entry in engine.held.all():
                resolve(a.layout, entry["id"], "apply")
        a.sync()
        self.seed_files({
            "modules/guards/module.toml": '[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\ntests = ["hooks/test_guard.py"]\n'
                                          '\n[[files]]\nname = "helper"\npath = "hooks/helper.py"\n',
            "modules/guards/hooks/helper.py": "def ok():\n    return True\n",
        }, "new helper, held on a")
        engine, _report = a.sync()
        self.assertTrue([e for e in engine.held.all() if e.get("item_key") == "guards/helper"])
        # Editing the installed hook to import the held helper must not push it to other machines.
        a.write(self.installed(a, "guard.py"), self.HOOK)
        engine, _report = a.sync()
        self.assertTrue([e for e in engine.held.all() if e["kind"] == "edit" and e.get("item_key") == "guards/guard"])
        self.assertNotIn("from helper", git(a.layout.config, "show", "origin/main:modules/guards/hooks/guard.py"))


    def test_hook_disabled_here_is_tested_when_enabled(self):
        self.base()
        a = self.machine("a")
        data = a.machine_data()
        data.setdefault("overrides", {})["guards/guard"] = {"kind": "disable"}
        a.write_machine(data)
        a.sync()
        # While the hook is off, it changes to need a file no item installs.
        self.seed_files({"modules/guards/hooks/guard.py": self.HOOK, "modules/guards/hooks/helper.py": "def ok():\n    return True\n"}, "undeclared helper")
        a.sync()
        data = a.machine_data()
        del data["overrides"]["guards/guard"]
        a.write_machine(data)
        engine, _report = a.sync()
        self.assertTrue([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards/guard"])
        self.assertFalse(self.installed(a, "guard.py").exists())
        # Once the helper is declared, the hook installs and its alert closes.
        self.seed_files({"modules/guards/module.toml": '[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\ntests = ["hooks/test_guard.py"]\n\n[[files]]\nname = "helper"\npath = "hooks/helper.py"\n'}, "declare helper")
        engine, _report = a.sync()
        self.assertIn("from helper", self.installed(a, "guard.py").read_text())
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards/guard"])


    def installed_with_helper(self):
        self.seed_files({"kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n', "modules/guards/hooks/test_guard.py": self.TEST})
        self.add_helper()
        a = self.machine("a")
        engine, _report = a.sync()
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"].startswith("tests:")])
        self.assertIn("from helper", self.installed(a, "guard.py").read_text())
        return a

    def test_change_to_a_file_an_unchanged_hook_imports_is_held_back(self):
        a = self.installed_with_helper()
        self.seed_files({"modules/guards/hooks/helper.py": "def renamed():\n    return True\n"}, "break the helper")
        engine, _report = a.sync()
        self.assertIn("def ok", self.installed(a, "helper.py").read_text())
        messages = [alert["message"] for alert in engine.alerts.open() if alert["key"] == "tests:guards"]
        self.assertTrue(messages and "kept module guards" in messages[0], messages)
        # A compatible change installs and clears the alert.
        self.seed_files({"modules/guards/hooks/helper.py": "def ok():\n    return 1 == 1\n"}, "fix the helper")
        engine, _report = a.sync()
        self.assertIn("1 == 1", self.installed(a, "helper.py").read_text())
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"].startswith("tests:")])

    def test_removing_a_file_an_unchanged_hook_imports_is_held_back(self):
        a = self.installed_with_helper()
        self.seed_files({"modules/guards/module.toml": '[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\ntests = ["hooks/test_guard.py"]\n'}, "drop the helper item")
        engine, _report = a.sync()
        self.assertTrue(self.installed(a, "helper.py").exists())
        self.assertTrue([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards"])
        import sys
        from agent_kit_sync import processes
        self.assertEqual(processes.run([sys.executable, str(self.installed(a, "guard.py"))]).returncode, 0)


    def test_hook_failing_with_its_installed_files_does_not_hold_back_its_module(self):
        a = self.installed_with_helper()
        # The installed test now fails for a reason unrelated to the module's files.
        a.write(a.layout.installed / "guards" / "hooks" / "test_guard.py", "import sys\nsys.exit(1)\n")
        self.seed_files({"modules/guards/hooks/helper.py": "def ok():\n    return 2 > 1\n"}, "compatible helper change")
        engine, _report = a.sync()
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards"])

    def test_held_back_module_keeps_a_section_edited_here(self):
        a = self.installed_with_helper()
        self.seed_files({"modules/guards/hooks/helper.py": "def renamed():\n    return True\n"}, "break the helper")
        engine, _report = a.sync()
        self.assertTrue([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards"])
        # The installed copies stay, the index keeps its entries, and a later good version installs.
        self.assertIn("def ok", self.installed(a, "helper.py").read_text())
        self.assertIn("guards/helper", engine.index.items)
        self.seed_files({"modules/guards/hooks/helper.py": "def ok():\n    return True\n\ndef renamed():\n    return True\n"}, "compatible again")
        engine, _report = a.sync()
        self.assertIn("def renamed", self.installed(a, "helper.py").read_text())
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"].startswith(("tests:", "module:"))])


    def test_disabling_a_file_an_installed_hook_imports_holds_the_module(self):
        a = self.installed_with_helper()
        data = a.machine_data()
        data.setdefault("overrides", {})["guards/helper"] = {"kind": "disable"}
        a.write_machine(data)
        engine, _report = a.sync()
        self.assertTrue(self.installed(a, "helper.py").exists())
        self.assertTrue([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards"])

    def test_a_failing_changed_hook_does_not_hold_back_an_unchanged_one(self):
        # guard imports helper.py, which is itself a hook ("base") with its own test.
        # guard is listed first, so it is tested before base has been set back.
        manifest = ('[[hooks]]\nname = "guard"\nfile = "hooks/guard.py"\nevents = ["stop"]\ntests = ["hooks/test_guard.py"]\n'
                    '\n[[hooks]]\nname = "base"\nfile = "hooks/helper.py"\nevents = ["stop"]\ntests = ["hooks/test_base.py"]\n')
        self.seed_files({
            "kit.toml": 'format = 1\n\n[[modules]]\nname = "guards"\n',
            "modules/guards/module.toml": manifest,
            "modules/guards/hooks/guard.py": self.HOOK,
            "modules/guards/hooks/test_guard.py": self.TEST,
            "modules/guards/hooks/helper.py": "def ok():\n    return True\n",
            "modules/guards/hooks/test_base.py": "import sys\nsys.exit(0)\n",
        })
        a = self.machine("a")
        engine, _report = a.sync()
        self.assertFalse([alert for alert in engine.alerts.open() if alert["key"].startswith("tests:")])
        # base changes so that it breaks guard and fails its own test. base alone stays back; guard keeps working.
        self.seed_files({"modules/guards/hooks/helper.py": "def renamed():\n    return True\n",
                         "modules/guards/hooks/test_base.py": "import sys\nsys.exit(1)\n"}, "break base")
        engine, _report = a.sync()
        keys = {alert["key"] for alert in engine.alerts.open()}
        self.assertIn("tests:guards/base", keys)
        self.assertNotIn("tests:guards", keys)
        self.assertIn("def ok", self.installed(a, "helper.py").read_text())


    def test_hook_set_back_to_its_earlier_version_is_tested_with_the_new_files(self):
        a = self.installed_with_helper()
        # The helper changes incompatibly with the installed hook, and the hook's new version fails its own test.
        self.seed_files({
            "modules/guards/hooks/helper.py": "def renamed():\n    return True\n",
            "modules/guards/hooks/guard.py": "import sys\nfrom helper import renamed\nsys.exit(1)\n",
        }, "new helper, broken new hook")
        engine, _report = a.sync()
        self.assertIn("def ok", self.installed(a, "helper.py").read_text())
        self.assertIn("from helper import ok", self.installed(a, "guard.py").read_text())
        self.assertTrue([alert for alert in engine.alerts.open() if alert["key"] == "tests:guards"])
        import sys
        from agent_kit_sync import processes
        self.assertEqual(processes.run([sys.executable, str(self.installed(a, "guard.py"))]).returncode, 0)


class EditDuringRunTests(TwoMachines):
    """A second edit saved while the run that carries the first edit back is still running."""

    def sync_with_edit_during_run(self, machine, path, old, new):
        """Run a sync that saves one more edit to `path` right after it commits the first one."""
        from agent_kit_sync.engine import Engine
        with machine.env():
            engine = Engine(machine.layout)
            commit_local = engine.commit_local

            def commit_then_edit(message):
                commit_local(message)
                machine.write(path, path.read_text(encoding="utf-8").replace(old, new))

            engine.commit_local = commit_then_edit
            return engine, engine.run()

    def test_second_section_edit_during_a_run_is_carried_back_next_run(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        path = a.path(".claude", "CLAUDE.md")
        a.write(path, path.read_text().replace("Write plainly.", "Write plainly and briefly."))
        _engine, report = self.sync_with_edit_during_run(a, path, "Keep projects", "Always keep projects")
        self.assertEqual(report.edits, ["writing/plain"])
        self.assertIn("Always keep projects", a.read(".claude", "CLAUDE.md"))
        engine, report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        self.assertEqual(report.edits, ["writing/plain"])
        source = (a.layout.config / "modules/writing/sections/plain.md").read_text()
        self.assertIn("Write plainly and briefly.", source)
        self.assertIn("Always keep projects in {{kit.projects}}.", source)
        self.assertIn("Always keep projects", a.read(".codex", "AGENTS.md"))
        b.sync()
        self.assertIn("Always keep projects", b.read(".codex", "AGENTS.md"))
        _engine, report = a.sync()
        self.assertEqual((report.edits, report.installed), ([], []))

    def test_second_skill_edit_during_a_run_is_carried_back_next_run(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "skills", "review", "SKILL.md")
        a.write(path, path.read_text().replace("report problems", "report every problem"))
        _engine, report = self.sync_with_edit_during_run(a, path, "Read the draft", "Read the whole draft")
        self.assertEqual(report.edits, ["writing/review"])
        engine, report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        self.assertEqual(report.edits, ["writing/review"])
        source = (a.layout.config / "modules/writing/skills/review/SKILL.md").read_text()
        self.assertIn("Read the whole draft and report every problem.", source)
        self.assertIn("Read the whole draft", a.read(".agents", "skills", "review", "SKILL.md"))

    def test_edit_saved_before_its_item_is_carried_back_is_kept(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "skills", "review", "SKILL.md")
        a.write(path, path.read_text().replace("report problems", "report every problem"))
        from agent_kit_sync.engine import Engine
        with a.env():
            engine = Engine(a.layout)
            carry_item = engine._carry_item

            def edit_then_carry(module, item, changes):
                if item.key == "writing/review":
                    a.write(path, path.read_text(encoding="utf-8").replace("Read the draft", "Read the whole draft"))
                return carry_item(module, item, changes)

            engine._carry_item = edit_then_carry
            engine.run()
        self.assertIn("Read the whole draft and report every problem.", a.read(".claude", "skills", "review", "SKILL.md"))
        engine, report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        source = (a.layout.config / "modules/writing/skills/review/SKILL.md").read_text()
        self.assertIn("Read the whole draft and report every problem.", source)

    def test_file_added_to_a_skill_and_edited_during_the_run_is_kept(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        notes = a.path(".claude", "skills", "review", "notes.md")
        a.write(notes, "First notes.\n")
        _engine, report = self.sync_with_edit_during_run(a, notes, "First notes.", "Second notes.")
        self.assertEqual(report.edits, ["writing/review"])
        self.assertEqual(a.read(".claude", "skills", "review", "notes.md"), "Second notes.\n")
        self.assertTrue(a.path(".claude", "skills", "review", "SKILL.md").is_file())
        engine, report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        self.assertEqual((a.layout.config / "modules/writing/skills/review/notes.md").read_text(), "Second notes.\n")
        self.assertEqual(a.read(".agents", "skills", "review", "notes.md"), "Second notes.\n")
        self.assertFalse((a.layout.state / "backups" / ".claude" / "skills" / "review" / "notes.md").exists())

    def test_copy_edited_after_it_was_checked_is_not_overwritten(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        other = b.path(".claude", "agents", "helper.md")
        b.write(other, other.read_text().replace("Help with the task.", "Help carefully."))
        b.sync()
        path = a.path(".claude", "agents", "helper.md")
        from agent_kit_sync.engine import Engine
        import agent_kit_sync.engine as engine_module
        real_write = engine_module.atomic_write

        def edit_then_write(target, data, **kwargs):
            # The edit lands after every check, just before the write, so only the write's own check sees it.
            if os.path.realpath(target) == os.path.realpath(path) and b"Help carefully." in data:
                a.write(path, path.read_text(encoding="utf-8").replace("Help with the task.", "Help with the whole task."))
            return real_write(target, data, **kwargs)

        with a.env():
            engine = Engine(a.layout)
            engine_module.atomic_write = edit_then_write
            try:
                engine.run()
            finally:
                engine_module.atomic_write = real_write
            record = engine.record.data["files"]
        self.assertIn("Help with the whole task.", a.read(".claude", "agents", "helper.md"))
        self.assertNotIn(os.path.realpath(path), [os.path.realpath(entry["path"]) for entry in record])

    def test_rebase_survives_a_write_that_fails(self):
        self.standard_config()
        manifest = self.seed / "modules/writing/module.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + '\n[[sections]]\nname = "other"\nfile = "sections/other.md"\n', encoding="utf-8")
        (self.seed / "modules/writing/sections/other.md").write_text("Other rule.\n", encoding="utf-8")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-qm", "second section")
        git(self.seed, "push", "-q")
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "CLAUDE.md")
        a.write(path, path.read_text().replace("Write plainly.", "Write plainly and briefly."))
        # A change to the other section's source makes this run rewrite CLAUDE.md, and that write fails.
        (a.layout.config / "modules/writing/sections/other.md").write_text("Other rule, changed.\n", encoding="utf-8")
        from agent_kit_sync.engine import Engine
        from agent_kit_sync.fsutil import ChangedUnderneath
        import agent_kit_sync.engine as engine_module
        with a.env():
            engine = Engine(a.layout)
            commit_local = engine.commit_local
            real_write = engine_module.atomic_write

            def commit_then_edit(message):
                commit_local(message)
                a.write(path, path.read_text(encoding="utf-8").replace("Keep projects", "Always keep projects"))

            def failing_write(target, data, **kwargs):
                if Path(target).name == "CLAUDE.md":
                    raise ChangedUnderneath(str(target))
                return real_write(target, data, **kwargs)

            engine.commit_local = commit_then_edit
            engine_module.atomic_write = failing_write
            try:
                engine.run()
            finally:
                engine_module.atomic_write = real_write
            record = engine.record.data["files"]
        self.assertNotIn("Other rule, changed.", a.read(".claude", "CLAUDE.md"))  # The write did fail.
        self.assertNotIn(os.path.realpath(path), [os.path.realpath(entry["path"]) for entry in record])
        engine, report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        source = (a.layout.config / "modules/writing/sections/plain.md").read_text()
        self.assertIn("Write plainly and briefly.", source)
        self.assertIn("Always keep projects", source)
        self.assertIn("Other rule, changed.", a.read(".claude", "CLAUDE.md"))

    def test_added_skill_file_is_not_backed_up_and_is_removed_when_retired(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        notes = a.path(".claude", "skills", "review", "notes.md")
        a.write(notes, "Notes.\n")
        a.sync()
        a.sync()
        self.assertEqual(a.read(".agents", "skills", "review", "notes.md"), "Notes.\n")
        self.assertEqual(list((a.layout.state / "backups").rglob("notes.md")) if (a.layout.state / "backups").exists() else [], [])
        b.sync()
        (b.layout.config / "modules/writing/skills/review/notes.md").unlink()
        b.sync()
        a.sync()
        self.assertFalse(notes.exists())
        self.assertFalse(a.path(".agents", "skills", "review", "notes.md").exists())
        a.sync()
        self.assertFalse(notes.exists())
        self.assertFalse((a.layout.config / "modules/writing/skills/review/notes.md").exists())

    def test_saved_index_never_lists_a_retired_hook(self):
        self.standard_config()
        manifest = self.seed / "modules/writing/module.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + '\n[[hooks]]\nname = "check"\nfile = "hooks/check.py"\nevents = ["stop"]\ntests = ["hooks/test_check.py"]\n', encoding="utf-8")
        (self.seed / "modules/writing/hooks").mkdir()
        (self.seed / "modules/writing/hooks/check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        (self.seed / "modules/writing/hooks/test_check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-qm", "hook")
        git(self.seed, "push", "-q")
        a = self.machine("a")
        a.sync()
        hook = a.layout.installed / "writing" / "hooks" / "check.py"
        self.assertTrue(hook.is_file())
        manifest = a.layout.config / "modules/writing/module.toml"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text[:text.index("[[hooks]]", text.index("[[settings]]"))], encoding="utf-8")
        from agent_kit_sync.engine import Engine
        with a.env():
            engine = Engine(a.layout)

            def failing_sections(plan):
                raise OSError("instruction file not writable")

            engine._write_sections = failing_sections
            with self.assertRaises(OSError):
                engine.run(network=False)
        self.assertFalse(hook.exists())
        index = json.loads((a.layout.state / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([h for h in index.get("hooks", []) if h["name"] == "check"], [])

    def test_edit_during_a_run_that_carried_nothing_back_stays_an_edit(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "CLAUDE.md")
        _engine, report = self.sync_with_edit_during_run(a, path, "Write plainly.", "Write plainly and briefly.")
        self.assertEqual(report.edits, [])
        self.assertIn("briefly", a.read(".claude", "CLAUDE.md"))
        _engine, report = a.sync()
        self.assertEqual(report.edits, ["writing/plain"])
        self.assertIn("briefly", (a.layout.config / "modules/writing/sections/plain.md").read_text())


class WindowsLineBreakTests(TwoMachines):
    """A copy saved again with Windows line breaks, and no other change, still receives updates."""

    def write_bytes(self, path, data):
        """Save exact bytes, with a later modification time, as an editor does."""
        path.write_bytes(data)
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        return data

    def save_with_crlf(self, path):
        return self.write_bytes(path, path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))

    def test_instruction_file_saved_with_crlf_still_gets_updates(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        saved = self.save_with_crlf(a.path(".claude", "CLAUDE.md"))
        _engine, report = a.sync()
        self.assertEqual(report.edits, [])
        self.assertEqual(a.path(".claude", "CLAUDE.md").read_bytes(), saved)  # Nothing to update, so it stays as saved.
        path = b.path(".claude", "CLAUDE.md")
        b.write(path, path.read_text().replace("Write plainly.", "Write plainly and briefly."))
        b.sync()
        engine, report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        self.assertIn("Write plainly and briefly.", a.read(".claude", "CLAUDE.md"))
        _engine, report = a.sync()
        self.assertEqual((report.edits, report.installed), ([], []))

    def test_subagent_saved_with_crlf_still_gets_updates(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        saved = self.save_with_crlf(a.path(".claude", "agents", "helper.md"))
        _engine, report = a.sync()
        self.assertEqual(report.edits, [])
        self.assertEqual(a.path(".claude", "agents", "helper.md").read_bytes(), saved)  # Nothing to update, so it stays as saved.
        path = b.path(".claude", "agents", "helper.md")
        b.write(path, path.read_text().replace("Help with the task.", "Help carefully."))
        b.sync()
        engine, report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        self.assertIn("Help carefully.", a.read(".claude", "agents", "helper.md"))
        _engine, report = a.sync()
        self.assertEqual((report.edits, report.installed), ([], []))

    def test_retired_subagent_saved_with_crlf_is_removed(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        self.save_with_crlf(a.path(".claude", "agents", "helper.md"))
        # Retire the subagent in the same run, so the copy still has Windows line breaks when it is removed.
        manifest = a.layout.config / "modules/writing/module.toml"
        text = manifest.read_text(encoding="utf-8")
        start = text.index("[[agents]]")
        end = text.index("[[settings]]")
        manifest.write_text(text[:start] + text[end:], encoding="utf-8")
        _engine, report = a.sync()
        self.assertEqual(report.errors, [])
        self.assertFalse(a.path(".claude", "agents", "helper.md").exists())
        self.assertIn("claude|writing/helper|agents/helper.md", report.removed)

    def test_copy_left_with_crlf_does_not_start_a_sync_at_every_stop(self):
        from agent_kit_sync.dispatch import copies_changed
        self.standard_config()
        a = self.machine("a")
        a.sync()
        self.save_with_crlf(a.path(".claude", "agents", "helper.md"))
        a.sync()
        index = json.loads((a.layout.state / "index.json").read_text(encoding="utf-8"))
        self.assertFalse(copies_changed(index))

    def test_crlf_save_with_a_real_edit_is_carried_back(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        path = a.path(".claude", "agents", "helper.md")
        self.save_with_crlf(path)
        self.write_bytes(path, path.read_bytes().replace(b"Help with the task.", b"Help carefully."))
        self.assertIn(b"\r\n", path.read_bytes())
        engine, report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        self.assertEqual(report.edits, ["writing/helper"])
        source = (a.layout.config / "modules/writing/agents/helper.md").read_bytes()
        self.assertIn(b"Help carefully.", source)
        self.assertNotIn(b"\r", source)

    def test_crlf_block_edit_and_an_incoming_change_both_arrive_in_one_run(self):
        self.standard_config()
        source_path = self.seed / "modules/writing/sections/plain.md"
        lines = "".join(f"Line {n}.\n" for n in range(1, 9))
        source_path.write_text(source_path.read_text(encoding="utf-8") + lines, encoding="utf-8")
        git(self.seed, "commit", "-qam", "longer section")
        git(self.seed, "push", "-q")
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        other = b.path(".claude", "CLAUDE.md")
        b.write(other, other.read_text().replace("Line 8.", "Line eight."))
        b.sync()
        path = a.path(".claude", "CLAUDE.md")
        self.save_with_crlf(path)
        self.write_bytes(path, path.read_bytes().replace(b"Write plainly.", b"Write plainly and briefly."))
        engine, report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        text = a.read(".claude", "CLAUDE.md")
        self.assertIn("Write plainly and briefly.", text)
        self.assertIn("Line eight.", text)
        _engine, report = a.sync()
        self.assertEqual(report.edits, [])
        source = (a.layout.config / "modules/writing/sections/plain.md").read_bytes()
        self.assertIn(b"Write plainly and briefly.", source)
        self.assertNotIn(b"\r", source)


class RemovalSafetyTests(TwoMachines):
    """Removing copies and blocks from this machine never reads as a deletion for every machine, and
    never loses a file the user had."""

    def disable(self, machine, item_key):
        data = machine.machine_data()
        data.setdefault("overrides", {})[item_key] = {"kind": "disable"}
        machine.write_machine(data)

    def sync_with_change_during_run(self, machine, change):
        """Run a sync that calls `change` right after it commits this machine's edits."""
        from agent_kit_sync.engine import Engine
        with machine.env():
            engine = Engine(machine.layout)
            commit_local = engine.commit_local

            def commit_then_change(message):
                commit_local(message)
                change()

            engine.commit_local = commit_then_change
            return engine, engine.run()

    def replace_own_helper(self, machine, text):
        """Give `machine` its own Claude Code helper, then let the config repo's helper replace it."""
        mine = machine.path(".claude", "agents", "helper.md")
        machine.write(mine, text)
        engine, _report = machine.sync()
        collision = next(e for e in engine.held.all() if e["kind"] == "collision" and e["app"] == "claude")
        from agent_kit_sync.resolve import resolve
        with machine.env():
            resolve(machine.layout, collision["id"], "config-replaces-local")
        machine.sync()
        self.assertNotEqual(mine.read_text(encoding="utf-8"), text)
        return mine

    def undo(self, machine):
        from types import SimpleNamespace
        from agent_kit_sync.commands import cmd_undo
        with machine.env():
            self.assertEqual(cmd_undo(SimpleNamespace(yes=True), machine.layout), 0)

    def test_run_that_stops_while_removing_copies_is_not_read_as_a_deletion(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        self.disable(a, "writing/helper")
        import agent_kit_sync.engine as engine_module
        remove_file = engine_module.remove_file

        def remove_then_stop(path, **kwargs):
            remove_file(path, **kwargs)
            raise RuntimeError("the machine lost power")

        engine_module.remove_file = remove_then_stop
        try:
            with self.assertRaises(RuntimeError):
                a.sync()
        finally:
            engine_module.remove_file = remove_file
        _engine, report = a.sync()
        self.assertEqual(report.deletions, [])
        self.assertFalse(a.path(".claude", "agents", "helper.md").exists())
        self.assertFalse(a.path(".codex", "agents", "helper.toml").exists())
        self.assertTrue((a.layout.config / "modules/writing/agents/helper.md").exists())
        b.sync()
        self.assertTrue(b.path(".claude", "agents", "helper.md").exists())

    def test_run_that_stops_after_removing_a_block_is_not_read_as_a_deletion(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        self.disable(a, "writing/plain")
        import agent_kit_sync.engine as engine_module
        atomic_write = engine_module.atomic_write

        def write_then_stop(path, data, **kwargs):
            result = atomic_write(path, data, **kwargs)
            if Path(path).name == "CLAUDE.md":
                raise RuntimeError("the machine lost power")
            return result

        engine_module.atomic_write = write_then_stop
        try:
            with self.assertRaises(RuntimeError):
                a.sync()
        finally:
            engine_module.atomic_write = atomic_write
        _engine, report = a.sync()
        self.assertEqual(report.deletions, [])
        self.assertNotIn("Write plainly.", a.read(".claude", "CLAUDE.md"))
        self.assertNotIn("Write plainly.", a.read(".codex", "AGENTS.md"))
        b.sync()
        self.assertIn("Write plainly.", b.read(".claude", "CLAUDE.md"))

    def test_undo_after_a_removal_keeps_the_users_own_file(self):
        self.standard_config()
        a = self.machine("a")
        mine = self.replace_own_helper(a, "my own helper\n")
        self.disable(a, "writing/helper")
        a.sync()
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own helper\n")
        a.write(mine, "my own helper, edited\n")
        self.undo(a)
        backup = a.layout.state / "backups" / ".claude" / "agents" / "helper.md"
        self.assertEqual(backup.read_text(encoding="utf-8"), "my own helper, edited\n")

    def test_undo_reverses_the_latest_of_two_runs_in_one_second(self):
        from agent_kit_sync.state import RunRecord
        a = self.machine("a")
        a.layout.state.mkdir(parents=True, exist_ok=True)
        path = a.path(".claude", "agents", "helper.md")
        for name, when in (("20261107-120000-ffff", 100.0), ("20261107-120000-0000", 101.0)):
            record = RunRecord(a.layout, run_id=name)
            record.data["time"] = when
            record.file(path, name.encode("utf-8"))
            record.save()
        self.assertEqual(RunRecord.latest(a.layout)["run"], "20261107-120000-0000")

    def test_run_that_writes_and_then_retires_a_copy_records_the_restored_file(self):
        from agent_kit_sync.state import RunRecord
        a = self.machine("a")
        path, backup = a.path(".claude", "agents", "helper.md"), a.layout.state / "backups" / "helper.md"
        record = RunRecord(a.layout)
        record.file(path, b"my own helper\n")
        record.file(path, b"the config repo's helper\n", kind="remove", backup=backup)
        self.assertEqual([(e["path"], e["kind"], e.get("backup")) for e in record.data["files"]], [(str(path), "copy", str(backup))])
        self.assertEqual(Path(record.data["files"][0]["saved"]).read_bytes(), b"my own helper\n")

    def test_deleting_a_copy_does_not_bring_back_the_file_it_replaced(self):
        self.standard_config()
        a = self.machine("a")
        mine = self.replace_own_helper(a, "my own helper\n")
        mine.unlink()
        _engine, report = a.sync()
        self.assertEqual(report.deletions, ["writing/helper"])
        self.assertFalse(mine.exists())
        kept = sorted((a.layout.state / "backups" / ".claude" / "agents").glob("helper.md.deleted-*"))
        self.assertEqual([path.read_text(encoding="utf-8") for path in kept], ["my own helper\n"])
        a.sync()
        self.assertFalse(mine.exists())
        # Undo has no copy to put back where the run found it already deleted, so a file made there since stays.
        a.write(mine, "a new helper\n")
        self.undo(a)
        self.assertEqual(mine.read_text(encoding="utf-8"), "a new helper\n")

    def test_copy_deleted_during_a_run_is_not_written_back(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        helper = a.path(".claude", "agents", "helper.md")
        self.sync_with_change_during_run(a, helper.unlink)
        self.assertFalse(helper.exists())
        _engine, report = a.sync()
        self.assertEqual(report.deletions, ["writing/helper"])
        self.assertFalse(a.path(".codex", "agents", "helper.toml").exists())
        b.sync()
        self.assertFalse(b.path(".claude", "agents", "helper.md").exists())

    def test_copy_edited_and_then_deleted_during_the_run_is_not_written_back(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        skill = a.path(".claude", "skills", "review", "SKILL.md")
        a.write(skill, skill.read_text(encoding="utf-8").replace("report problems", "report every problem"))
        _engine, report = self.sync_with_change_during_run(a, skill.unlink)
        self.assertEqual(report.edits, ["writing/review"])
        self.assertFalse(skill.exists())
        _engine, report = a.sync()
        self.assertEqual(report.deletions, ["writing/review"])
        self.assertFalse(a.path(".agents", "skills", "review", "SKILL.md").exists())

    def test_copy_deleted_during_a_run_comes_back_with_an_incoming_edit(self):
        # As when a merge meets a deletion: the edit wins and the item stays.
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        other = b.path(".claude", "agents", "helper.md")
        b.write(other, other.read_text(encoding="utf-8").replace("Help with the task.", "Help carefully."))
        b.sync()
        helper = a.path(".claude", "agents", "helper.md")
        engine, _report = self.sync_with_change_during_run(a, helper.unlink)
        self.assertIn("Help carefully.", helper.read_text(encoding="utf-8"))
        self.assertIn("kept:claude|writing/helper|agents/helper.md", [x["key"] for x in engine.alerts.open()])
        _engine, report = a.sync()
        self.assertEqual(report.deletions, [])

    def test_block_deleted_during_a_run_is_not_written_back(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        path = a.path(".claude", "CLAUDE.md")
        begin, end = "<!-- agent-kit-sync begin writing/plain -->", "<!-- agent-kit-sync end writing/plain -->"

        def delete_block():
            text = path.read_text(encoding="utf-8")
            a.write(path, text[:text.index(begin)] + text[text.index(end) + len(end):].lstrip("\n"))

        self.sync_with_change_during_run(a, delete_block)
        self.assertNotIn("Write plainly.", a.read(".claude", "CLAUDE.md"))
        _engine, report = a.sync()
        self.assertEqual(report.deletions, ["writing/plain"])
        b.sync()
        self.assertNotIn("Write plainly.", b.read(".codex", "AGENTS.md"))

    def test_a_copy_open_in_another_program_is_removed_by_a_later_run(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        helper = a.path(".claude", "agents", "helper.md")
        locked = os.path.realpath(helper)
        import pathlib
        unlink = pathlib.Path.unlink

        def unlink_unless_locked(path, *args, **kwargs):
            if os.path.realpath(path) == locked:
                raise PermissionError(13, "The process cannot access the file because it is being used by another process", str(path))
            return unlink(path, *args, **kwargs)

        self.disable(a, "writing/helper")
        pathlib.Path.unlink = unlink_unless_locked
        try:
            _engine, report = a.sync()
        finally:
            pathlib.Path.unlink = unlink
        self.assertTrue(helper.exists())
        self.assertFalse(a.path(".codex", "agents", "helper.toml").exists())
        _engine, report = a.sync()
        self.assertFalse(helper.exists())
        self.assertEqual(report.deletions, [])

    def test_save_while_signatures_are_taken_still_starts_a_sync(self):
        self.standard_config()
        a = self.machine("a")
        a.sync()
        helper = a.path(".claude", "agents", "helper.md")
        import agent_kit_sync.engine as engine_module
        from agent_kit_sync import dispatch
        from agent_kit_sync.engine import Engine
        read_or_none = engine_module.read_or_none
        saved = []

        def read_then_save(path):
            data = read_or_none(path)
            if not saved and os.path.realpath(path) == os.path.realpath(helper):
                saved.append(path)
                a.write(helper, data.decode("utf-8") + "More.\n")
            return data

        with a.env():
            engine = Engine(a.layout)
            engine_module.read_or_none = read_then_save
            try:
                engine.refresh_signatures()
            finally:
                engine_module.read_or_none = read_or_none
        self.assertTrue(saved)
        self.assertTrue(dispatch.copies_changed({"copies": engine.index.copies}))

    def test_block_update_keeps_the_line_breaks_of_a_crlf_file(self):
        self.standard_config()
        a, b = self.machine("a"), self.machine("b")
        a.sync()
        b.sync()
        path = a.path(".claude", "CLAUDE.md")
        path.write_bytes(b"My own line.\r\n\r\n" + path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        other = b.path(".claude", "CLAUDE.md")
        b.write(other, other.read_text(encoding="utf-8").replace("Write plainly.", "Write plainly and briefly."))
        b.sync()
        engine, _report = a.sync()
        self.assertEqual([e.get("item_key") for e in engine.held.all()], [])
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"My own line.\r\n"))
        self.assertIn(b"Write plainly and briefly.\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        _engine, report = a.sync()
        self.assertEqual((report.edits, report.installed), ([], []))
        self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
