"""Cursor's rule files: output written from the instruction sections, never read back."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

from support import TwoMachines
from agent_kit_sync import processes
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
        return processes.run(["git", "-C", str(self.a.layout.config), "rev-parse", "HEAD"], stdout=subprocess.PIPE, check=True).stdout.decode().strip()

    def test_each_section_becomes_a_rule_with_imports_filled_in(self):
        _engine, report = self.a.sync()
        self.assertEqual(report.errors, [])
        plain = self.rule("plain").read_text(encoding="utf-8")
        self.assertTrue(plain.startswith("---\ndescription: "))
        self.assertIn("alwaysApply: true\n---\n\n<!-- agent-kit-sync rule file=agent-kit-sync--writing--plain.mdc item=writing/plain sha256=", plain)
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
        self.seed_files({"modules/writing/sections/plain.md": "@/somewhere/else.md\nWrite plainly.\n@Tim, hello.\n\n```\n@/in/a/fence.md\n```\n"}, message="bad import")
        engine, _report = self.a.sync()
        messages = [a["message"] for a in engine.alerts.open() if "name no file agent-kit-sync installs" in a["message"]]
        self.assertEqual(len(messages), 1)
        self.assertIn("/somewhere/else.md", messages[0])
        self.assertNotIn("fence", messages[0])
        self.assertNotIn("Tim", messages[0])

    def test_uninstall_removes_only_agent_kit_syncs_rules(self):
        self.a.sync()
        (self.rules() / "my-rule.mdc").write_text("Mine.\n", encoding="utf-8")
        from types import SimpleNamespace
        from agent_kit_sync.commands import cmd_uninstall
        with self.a.env():
            self.assertEqual(cmd_uninstall(SimpleNamespace(yes=True), self.a.layout), 0)
        self.assertEqual(sorted(p.name for p in self.rules().iterdir()), ["my-rule.mdc"])
        self.assertNotIn("Write plainly.", self.a.read(".claude", "CLAUDE.md"))

    def test_cursor_leaving_the_machine_closes_its_alerts(self):
        self.seed_files({"modules/writing/sections/plain.md": "@/somewhere/else.md\n"}, message="bad import")
        engine, _report = self.a.sync()
        self.assertTrue(any("Cursor" in a["message"] for a in engine.alerts.open()))
        data = self.a.machine_data()
        data["apps"] = ["claude", "codex"]
        self.a.write_machine(data)
        engine, _report = self.a.sync()
        self.assertFalse(any("Cursor" in a["message"] for a in engine.alerts.open()))

    def test_a_header_only_edit_is_saved_too(self):
        self.a.sync()
        for change in (("alwaysApply: true", "alwaysApply: false"), ("---\n\n<!--", "---\n\nMy note.\n<!--")):
            with self.subTest(change=change[1][:20]):
                path = self.rule("plain")
                self.a.write(path, path.read_text(encoding="utf-8").replace(*change, 1))
                self.a.sync()
                self.assertEqual(path.read_text(encoding="utf-8").count("alwaysApply: true"), 1)
                backups = sorted((self.a.layout.state / "backups" / "cursor-rules").iterdir())
                self.assertTrue(any(change[1] in b.read_text(encoding="utf-8") for b in backups))

    def test_backups_in_the_same_second_do_not_overwrite_each_other(self):
        from agent_kit_sync.cursor_rules import Outcome, _backup
        path = self.tmp / "agent-kit-sync--writing--plain.mdc"
        outcome = Outcome()
        for text in (b"first", b"second", b"\xff not utf-8"):
            self.assertTrue(_backup(path, text, self.tmp / "backups", outcome))
        self.assertEqual(sorted(Path(b).read_bytes() for b in outcome.backed_up), [b"first", b"second", b"\xff not utf-8"])

    def test_a_copy_holding_our_marker_under_another_name_is_left_alone(self):
        self.a.sync()
        copy = self.rules() / "agent-kit-sync--writing--plain-mine.mdc"
        copy.write_bytes(self.rule("plain").read_bytes())
        self.a.sync()
        self.assertTrue(copy.exists())

    def test_cursor_leaving_the_machine_removes_its_rules(self):
        self.a.sync()
        (self.rules() / "my-rule.mdc").write_text("Mine.\n", encoding="utf-8")
        data = self.a.machine_data()
        data["apps"] = ["claude", "codex"]
        self.a.write_machine(data)
        self.a.sync()
        self.assertEqual(sorted(p.name for p in self.rules().iterdir()), ["my-rule.mdc"])

    def test_an_import_of_a_failed_modules_file_keeps_its_text(self):
        self.seed_files({
            "kit.toml": CONFIG["kit.toml"] + """
                [[modules]]
                name = "rules"
            """,
            "modules/rules/module.toml": """
                description = "Imports the writing standard"

                [[sections]]
                name = "uses-style"
                file = "sections/uses.md"
            """,
            "modules/rules/sections/uses.md": "@{{kit.home}}/.agent-kit-sync/installed/writing/style.md\n",
        }, message="cross-module import")
        self.a.sync()
        rule = self.rules() / "agent-kit-sync--rules--uses-style.mdc"
        self.assertIn("Use short sentences.", rule.read_text(encoding="utf-8"))
        self.seed_files({"modules/writing/style.md": "Broken {{kit.nothing}}.\n"}, message="break the writing module")
        engine, report = self.a.sync()
        self.assertTrue(report.errors)
        self.assertIn("Use short sentences.", rule.read_text(encoding="utf-8"))
        self.assertFalse(any("name no file agent-kit-sync installs" in a["message"] for a in engine.alerts.open()))

    def test_a_frozen_item_keeps_its_rule(self):
        self.a.sync()
        before = self.rule("plain").read_text(encoding="utf-8")
        from agent_kit_sync.engine import Engine
        from agent_kit_sync.plan import Plan
        with self.a.env():
            engine = Engine(self.a.layout)
            engine.frozen = lambda key: key == "writing/plain"
            plan = Plan()
            engine.write_cursor_rules(plan)
        self.assertEqual(self.rule("plain").read_text(encoding="utf-8"), before)
        self.assertFalse(self.rule("import").exists())

    def test_a_shorter_section_removes_its_old_parts(self):
        paragraph = ("Long text for the size test. " * 40).strip() + "\n\n"
        self.seed_files({"modules/writing/sections/plain.md": paragraph * 160}, message="long")
        self.a.sync()
        self.assertTrue(list(self.rules().glob("agent-kit-sync--writing--plain--part*.mdc")))
        self.seed_files({"modules/writing/sections/plain.md": "Short now.\n"}, message="short")
        self.a.sync()
        self.assertEqual(list(self.rules().glob("agent-kit-sync--writing--plain--part*.mdc")), [])
        self.assertIn("Short now.", self.rule("plain").read_text(encoding="utf-8"))

    def test_an_edited_rule_alert_closes_after_a_day(self):
        self.a.sync()
        self.a.write(self.rule("plain"), self.rule("plain").read_text(encoding="utf-8") + "Edit.\n")
        engine, _report = self.a.sync()
        self.assertTrue(any("were replaced" in a["message"] for a in engine.alerts.open()))
        engine, _report = self.a.sync()
        self.assertTrue(any("were replaced" in a["message"] for a in engine.alerts.open()))
        path = self.a.layout.state / "alerts.json"
        alerts = json.loads(path.read_text(encoding="utf-8"))
        for alert in alerts:
            if alert.get("key") == "cursor-rule-edited":
                alert["time"] -= 2 * 86400
        path.write_text(json.dumps(alerts), encoding="utf-8")
        engine, _report = self.a.sync()
        self.assertFalse(any("were replaced" in a["message"] for a in engine.alerts.open()))

    def test_a_machine_without_cursor_writes_no_rules(self):
        b = self.machine("b")
        b.sync()
        self.assertFalse(b.path(".cursor").exists())


    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions and a user who is not root")
    def test_an_unreadable_rules_folder_does_not_stop_the_sync(self):
        self.a.sync()
        os.chmod(self.rules(), 0)
        try:
            self.seed_files({"modules/writing/sections/plain.md": "Write plainly, always.\n"}, message="upstream")
            engine, report = self.a.sync()
            self.assertIn("Write plainly, always.", self.a.read(".claude", "CLAUDE.md"))
            self.assertTrue(any("Cursor's rule files" in a["message"] for a in engine.alerts.open()))
        finally:
            os.chmod(self.rules(), 0o755)

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symbolic links")
    def test_a_link_at_our_name_is_left_alone(self):
        self.rules().mkdir(parents=True)
        target = self.tmp / "elsewhere" / "target.mdc"
        try:
            os.symlink(target, self.rule("plain"))
        except OSError:
            self.skipTest("cannot create symbolic links here")
        engine, _report = self.a.sync()
        self.assertFalse(target.exists())
        self.assertTrue(os.path.islink(self.rule("plain")))
        self.assertTrue(any("did not write these Cursor rule files" in a["message"] for a in engine.alerts.open()))

    def test_a_trailing_space_after_the_marker_still_marks_our_file(self):
        self.a.sync()
        path = self.rule("plain")
        text = path.read_text(encoding="utf-8").replace("-->\n", "--> \n", 1)
        self.a.write(path, text)
        engine, _report = self.a.sync()
        self.assertFalse(any("did not write these Cursor rule files" in a["message"] for a in engine.alerts.open()))

    def test_backup_notice_survives_when_cursor_leaves(self):
        self.a.sync()
        self.a.write(self.rule("plain"), self.rule("plain").read_text(encoding="utf-8") + "Edit.\n")
        data = self.a.machine_data()
        data["apps"] = ["claude", "codex"]
        self.a.write_machine(data)
        engine, _report = self.a.sync()
        self.assertTrue(any("saved the ones that had been edited" in a["message"] for a in engine.alerts.open()))
        engine, _report = self.a.sync()
        self.assertTrue(any("saved the ones that had been edited" in a["message"] for a in engine.alerts.open()))


class NameTests(unittest.TestCase):
    def test_a_file_that_changes_while_writing_is_left_for_the_next_run(self):
        import tempfile
        from unittest import mock
        folder = Path(tempfile.mkdtemp())
        rule = cursor_rules.compose("writing", "plain", "New text.\n")
        old = cursor_rules.compose("writing", "plain", "Old text.\n")
        path = folder / rule.name
        path.write_text(old.text, encoding="utf-8")
        real_read = cursor_rules._read

        def read_then_edit(target, outcome):
            result = real_read(target, outcome)
            target.write_text(old.text + "Edit made just now.\n", encoding="utf-8")
            return result
        with mock.patch.object(cursor_rules, "_read", read_then_edit):
            outcome = cursor_rules.write_rules(folder, [rule], set(), folder / "backups")
        self.assertIn("Edit made just now.", path.read_text(encoding="utf-8"))
        self.assertEqual(outcome.written, [])

    def test_inline_triple_backticks_are_not_a_fence(self):
        text, unmatched, _ = cursor_rules.expand_imports("```x``` marks inline code\n@/x/y.md\n", {"/x/y.md": b"Y\n"})
        self.assertEqual((text, unmatched), ("```x``` marks inline code\nY\n", []))

    def test_only_line_feeds_end_lines(self):
        body = ("a\u2028b\x0cc\n\n" * 6000)
        pieces = cursor_rules.split(body, 20000)
        self.assertGreater(len(pieces), 1)
        self.assertLessEqual("".join(pieces).count("\n"), body.count("\n"))  # A cut may drop the blank line it falls on.
        self.assertEqual("".join(pieces).count("\u2028"), body.count("\u2028"))
        text, _, _ = cursor_rules.expand_imports("note\u2028@/x/y.md\n", {"/x/y.md": b"Y\n"})
        self.assertEqual(text, "note\u2028@/x/y.md\n")


    def test_names_are_short_and_the_marker_names_the_owner(self):
        self.assertEqual(cursor_rules.file_name("writing", "plain"), "agent-kit-sync--writing--plain.mdc")
        long = cursor_rules.file_name("a" * 80, "b" * 80)
        self.assertLessEqual(len(long), cursor_rules.NAME_LIMIT)
        self.assertNotEqual(long, cursor_rules.file_name("a" * 80, "b" * 79 + "c"))
        rule = cursor_rules.compose("a" * 80, "b" * 80, "Text.\n")
        mark = cursor_rules.read_mark(rule.text)
        self.assertEqual((mark.name, mark.module, mark.item, mark.edited), (long, "a" * 80, "b" * 80, False))
        self.assertIsNone(cursor_rules.read_mark("Mine.\n"))

    def test_a_split_inside_a_code_fence_closes_and_reopens_it(self):
        body = "Intro.\n\n```python\n" + "x = 1\n" * 30000 + "```\n\nAfter.\n"
        parts = cursor_rules.split(body, 50000)
        self.assertGreater(len(parts), 1)
        for part in parts:
            fences = cursor_rules.Fences()
            for line in part.splitlines(keepends=True):
                fences.step(line)
            self.assertIsNone(fences.open)
            self.assertLessEqual(len(part.encode("utf-8")), 50000)
        self.assertTrue(parts[1].startswith("```python\n"))


if __name__ == "__main__":
    unittest.main()
