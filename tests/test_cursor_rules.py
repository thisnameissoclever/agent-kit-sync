"""Cursor's rule files: output written from the instruction sections, never read back."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from support import TwoMachines
from agent_kit_sync import cursor_rules

CONFIG = {
    "kit.toml": """
        format = 1

        [[modules]]
        name = "writing"
    """,
    ".gitattributes": "* text=auto eol=lf\n",
    "modules/writing/module.toml": """
        description = "Writing rules"

        [[files]]
        name = "standard"
        path = "style.md"
        templates = true

        [[sections]]
        name = "plain"
        file = "sections/plain.md"

        [[sections]]
        name = "import"
        file = "sections/import.md"
    """,
    "modules/writing/style.md": """
        # Style

        Use short sentences. Projects live in {{kit.projects}}.
    """,
    "modules/writing/sections/plain.md": """
        Write plainly.
        Keep projects in {{kit.projects}}.
    """,
    "modules/writing/sections/import.md": """
        ## Writing rules

        @{{kit.module}}/style.md

        Follow the standard above.
    """,
}


class CursorRuleTests(TwoMachines):
    def setUp(self):
        super().setUp()
        self.seed_files(CONFIG)
        self.a = self.machine("a", apps=("claude", "codex", "cursor"))

    def rules(self) -> Path:
        return self.a.path(".cursor", "rules")

    def rule(self, item: str) -> Path:
        return self.rules() / f"agent-kit-sync--writing--{item}.mdc"

    def head(self) -> str:
        return subprocess.run(["git", "-C", str(self.a.layout.config), "rev-parse", "HEAD"], stdout=subprocess.PIPE, check=True).stdout.decode().strip()

    def test_each_section_becomes_a_rule_with_imports_filled_in(self):
        _engine, report = self.a.sync()
        self.assertEqual(report.errors, [])
        plain = self.rule("plain").read_text(encoding="utf-8")
        self.assertTrue(plain.startswith("---\ndescription: "))
        self.assertIn("alwaysApply: true\n---\n\n<!-- agent-kit-sync rule sha256=", plain)
        self.assertIn(f"Keep projects in {self.a.home / 'Projects'}.", plain)
        imported = self.rule("import").read_text(encoding="utf-8")
        self.assertIn(f"Use short sentences. Projects live in {self.a.home / 'Projects'}.", imported)
        self.assertIn("Follow the standard above.", imported)
        self.assertNotIn("\n@", imported)
        # Claude Code and Codex are unchanged: CLAUDE.md keeps the import line.
        self.assertIn("@", self.a.read(".claude", "CLAUDE.md"))
        self.assertFalse(self.a.path(".claude", "CLAUDE.md").read_text(encoding="utf-8").startswith("---"))
        # Nothing about Cursor is recorded for carry-back.
        index = json.loads((self.a.layout.state / "index.json").read_text(encoding="utf-8"))
        self.assertFalse([k for k, e in index["blocks"].items() if e["app"] == "cursor"])
        self.assertFalse([k for k, e in index["copies"].items() if e["app"] == "cursor"])

    def test_nothing_done_to_the_rules_changes_the_config_repo(self):
        self.a.sync()
        head = self.head()
        self.rule("plain").unlink()
        self.a.write(self.rule("import"), "Something else entirely.\n")
        for _ in range(2):
            _engine, report = self.a.sync()
            self.assertEqual((report.deletions, report.edits, report.held), ([], [], []))
        self.assertEqual(self.head(), head)
        self.assertTrue(self.rule("plain").exists())
        self.assertIn("Write plainly.", self.a.read(".claude", "CLAUDE.md"))

    def test_an_edited_rule_is_saved_then_replaced_with_an_alert(self):
        self.a.sync()
        text = self.rule("plain").read_text(encoding="utf-8").replace("Write plainly.", "Write plainly and briefly.")
        self.a.write(self.rule("plain"), text)
        engine, _report = self.a.sync()
        self.assertNotIn("briefly", self.rule("plain").read_text(encoding="utf-8"))
        backups = list((self.a.layout.state / "backups" / "cursor-rules").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertIn("briefly", backups[0].read_text(encoding="utf-8"))
        self.assertTrue(any("were replaced" in a["message"] and str(backups[0]) in a["message"] for a in engine.alerts.open()))
        self.assertNotIn("briefly", (self.a.layout.config / "modules/writing/sections/plain.md").read_text(encoding="utf-8"))

    def test_a_file_someone_else_wrote_at_our_name_is_left_alone(self):
        self.rules().mkdir(parents=True)
        mine = "---\nalwaysApply: true\n---\n\nMy own rule.\n"
        self.rule("plain").write_text(mine, encoding="utf-8")
        engine, report = self.a.sync()
        self.assertEqual(self.rule("plain").read_text(encoding="utf-8"), mine)
        self.assertTrue(any("did not write these Cursor rule files" in a["message"] for a in engine.alerts.open()))
        self.assertTrue(self.rule("import").exists())
        self.assertEqual(report.deletions, [])

    def test_other_rule_files_are_never_touched(self):
        self.rules().mkdir(parents=True)
        (self.rules() / "my-rule.mdc").write_text("Mine.\n", encoding="utf-8")
        (self.rules() / "agent-kit-sync--other--thing.mdc").write_text("Not written by agent-kit-sync.\n", encoding="utf-8")
        self.a.sync()
        self.assertEqual((self.rules() / "my-rule.mdc").read_text(encoding="utf-8"), "Mine.\n")
        self.assertTrue((self.rules() / "agent-kit-sync--other--thing.mdc").exists())

    def test_a_retired_section_removes_its_rule(self):
        self.a.sync()
        self.seed_files({"modules/writing/module.toml": CONFIG["modules/writing/module.toml"].split("[[sections]]\n        name = \"import\"")[0]}, message="retire import")
        self.a.sync()
        self.assertFalse(self.rule("import").exists())
        self.assertTrue(self.rule("plain").exists())

    def test_an_edited_rule_of_a_retired_section_is_saved_first(self):
        self.a.sync()
        self.a.write(self.rule("import"), self.rule("import").read_text(encoding="utf-8") + "My note.\n")
        self.seed_files({"modules/writing/module.toml": CONFIG["modules/writing/module.toml"].split("[[sections]]\n        name = \"import\"")[0]}, message="retire import")
        self.a.sync()
        self.assertFalse(self.rule("import").exists())
        backups = list((self.a.layout.state / "backups" / "cursor-rules").iterdir())
        self.assertTrue(any("My note." in b.read_text(encoding="utf-8") for b in backups))

    def test_a_failed_module_keeps_its_rules(self):
        self.a.sync()
        before = self.rule("plain").read_text(encoding="utf-8")
        self.seed_files({"modules/writing/sections/plain.md": "Write plainly {{kit.nothing}}.\n"}, message="break it")
        _engine, report = self.a.sync()
        self.assertTrue(report.errors)
        self.assertEqual(self.rule("plain").read_text(encoding="utf-8"), before)

    def test_a_long_section_is_split_under_cursors_limit(self):
        paragraph = ("Long text for the size test. " * 40).strip() + "\n\n"
        self.seed_files({"modules/writing/sections/plain.md": paragraph * 160}, message="long")
        engine, _report = self.a.sync()
        parts = sorted(self.rules().glob("agent-kit-sync--writing--plain--part*.mdc"))
        self.assertGreaterEqual(len(parts), 2)
        for part in parts:
            self.assertLessEqual(len(part.read_bytes()), cursor_rules.RULE_LIMIT)
        self.assertFalse(self.rule("plain").exists())
        self.assertFalse(any("still larger" in a["message"] for a in engine.alerts.open()))

    def test_windows_line_breaks_and_a_byte_order_mark_are_not_edits(self):
        self.a.sync()
        path = self.rule("plain")
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))
        engine, _report = self.a.sync()
        self.assertFalse((self.a.layout.state / "backups" / "cursor-rules").exists())
        self.assertFalse(any("were replaced" in a["message"] for a in engine.alerts.open()))
        self.assertNotIn(b"\r\n", path.read_bytes())
        self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_an_import_line_naming_no_installed_file_raises_an_alert(self):
        self.seed_files({"modules/writing/sections/plain.md": "@/somewhere/else.md\nWrite plainly.\n\n```\n@decorator\n```\n"}, message="bad import")
        engine, _report = self.a.sync()
        messages = [a["message"] for a in engine.alerts.open() if "name no file agent-kit-sync installs" in a["message"]]
        self.assertEqual(len(messages), 1)
        self.assertIn("/somewhere/else.md", messages[0])
        self.assertNotIn("decorator", messages[0])

    def test_uninstall_removes_only_agent_kit_syncs_rules(self):
        self.a.sync()
        (self.rules() / "my-rule.mdc").write_text("Mine.\n", encoding="utf-8")
        from agent_kit_sync.engine import Engine
        from agent_kit_sync.plan import Plan
        with self.a.env():
            engine = Engine(self.a.layout)
            engine.write_cursor_rules(Plan(), everything_removed=True)
        self.assertEqual(sorted(p.name for p in self.rules().iterdir()), ["my-rule.mdc"])

    def test_cursor_leaving_the_machine_closes_its_alerts(self):
        self.seed_files({"modules/writing/sections/plain.md": "@/somewhere/else.md\n"}, message="bad import")
        engine, _report = self.a.sync()
        self.assertTrue(any("Cursor" in a["message"] for a in engine.alerts.open()))
        data = self.a.machine_data()
        data["apps"] = ["claude", "codex"]
        self.a.write_machine(data)
        engine, _report = self.a.sync()
        self.assertFalse(any("Cursor" in a["message"] for a in engine.alerts.open()))

    def test_entries_from_a_version_that_read_rules_back_are_dropped(self):
        self.a.sync()
        path = self.a.layout.state / "index.json"
        index = json.loads(path.read_text(encoding="utf-8"))
        index["blocks"]["cursor|writing/plain"] = {"app": "cursor", "key": "writing/plain", "item_key": "writing/plain", "source_rel": "sections/plain.md", "hash": "0" * 64, "content": "x", "local": False}
        index["copies"]["cursor|writing/standard|style.md"] = {"path": str(self.rules() / "old.mdc"), "app": "cursor", "item_key": "writing/standard", "kind": "file", "source_rel": "style.md", "render_kind": "cursor-rule-text", "hash": "0" * 64, "executable": False, "local": False, "sig": None}
        path.write_text(json.dumps(index), encoding="utf-8")
        head = self.head()
        _engine, report = self.a.sync()
        self.assertEqual((report.deletions, report.edits), ([], []))
        self.assertEqual(self.head(), head)
        index = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("cursor|writing/plain", index["blocks"])
        self.assertNotIn("cursor|writing/standard|style.md", index["copies"])

    def test_a_machine_without_cursor_writes_no_rules(self):
        b = self.machine("b")
        b.sync()
        self.assertFalse(b.path(".cursor").exists())


class NameTests(unittest.TestCase):
    def test_names_are_short_and_owned(self):
        self.assertEqual(cursor_rules.file_name("writing", "plain"), "agent-kit-sync--writing--plain.mdc")
        self.assertEqual(cursor_rules.owner("agent-kit-sync--writing--plain--part2.mdc"), ("writing", "plain"))
        long = cursor_rules.file_name("a" * 80, "b" * 80)
        self.assertLessEqual(len(long), cursor_rules.NAME_LIMIT)
        self.assertNotEqual(long, cursor_rules.file_name("a" * 80, "b" * 79 + "c"))
        self.assertIsNone(cursor_rules.owner("my-rule.mdc"))


if __name__ == "__main__":
    unittest.main()
