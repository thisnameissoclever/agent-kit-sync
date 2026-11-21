"""Tests for Antigravity plugin manifest and rule files."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401 - puts repo root on sys.path
from agent_kit_sync import antigravity_rules, cursor_rules
from agent_kit_sync.paths import ANTIGRAVITY_PLUGIN_NAME


class AntigravityRulesTests(unittest.TestCase):
    def test_file_name_and_compose_use_always_on_frontmatter_and_valid_marker(self):
        self.assertEqual(antigravity_rules.file_name("writing", "rules"), "agent-kit-sync--writing--rules.md")
        self.assertEqual(antigravity_rules.file_name("writing", "rules", 2), "agent-kit-sync--writing--rules--part2.md")
        long_name = antigravity_rules.file_name("module-with-a-very-long-name", "section-with-an-equally-long-name-that-exceeds-limit")
        self.assertTrue(long_name.startswith("agent-kit-sync--"))
        self.assertTrue(long_name.endswith(".md"))
        self.assertLessEqual(len(long_name), antigravity_rules.NAME_LIMIT)

        rule = antigravity_rules.compose("writing", "rules", "Write clearly.\n")
        self.assertTrue(rule.text.startswith("---\ntrigger: always_on\n---\n\n"))
        mark = cursor_rules.read_mark(rule.text)
        self.assertIsNotNone(mark)
        self.assertFalse(mark.edited)
        self.assertEqual(mark.name, rule.name)
        self.assertEqual((mark.module, mark.item), ("writing", "rules"))

    def test_plan_rules_expands_imports_splits_under_24kb_and_flags_oversized(self):
        with tempfile.TemporaryDirectory(prefix="aks-ag-plan-") as folder:
            root = Path(folder)
            good = root / "style.md"
            missing = root / "missing.md"
            binary = root / "binary.md"
            block_text = "\n\n".join(f"Paragraph {i}: " + ("word " * 120) for i in range(60))
            support = {
                str(good): b"Imported line one.\n\nImported line two.\n",
                str(binary): b"\xff\xfe\xfd",
            }
            section = f"Intro.\n\n@{good}\n\n```\n@{missing}\n```\n\n@{missing}\n\n@{binary}\n\n{block_text}\n"
            planned = antigravity_rules.plan_rules([("writing", "rules", section)], support)

            self.assertEqual(planned.unmatched, [str(missing)])
            self.assertEqual(planned.unreadable, [str(binary)])
            self.assertEqual(planned.oversized, [])
            self.assertGreater(len(planned.rules), 1)
            for rule in planned.rules:
                self.assertLessEqual(len(rule.text.encode("utf-8")), antigravity_rules.RULE_LIMIT)
                self.assertTrue(rule.text.startswith("---\ntrigger: always_on\n---\n\n"))
            joined = "\n".join(r.text for r in planned.rules)
            self.assertIn("Imported line one.", joined)
            self.assertIn(f"```\n@{missing}\n```", joined)

            huge_word = "x" * (antigravity_rules.RULE_LIMIT + 500)
            oversized = antigravity_rules.plan_rules([("writing", "huge", huge_word)], {})
            self.assertEqual(oversized.oversized, ["agent-kit-sync--writing--huge.md"])

    def test_write_rules_handles_edits_foreign_files_keep_set_and_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="aks-ag-write-") as folder:
            root = Path(folder)
            rules_dir = root / "rules"
            backups = root / "backups"
            r1 = antigravity_rules.compose("m1", "a", "First.\n")
            r2 = antigravity_rules.compose("m2", "b", "Second.\n")
            kept = antigravity_rules.compose("failed", "c", "Keep old.\n")
            kept_new = antigravity_rules.compose("failed", "c", "Broken new.\n")

            first = antigravity_rules.write_rules(rules_dir, [r1, r2, kept], set(), backups)
            self.assertEqual(len(first.written), 3)
            self.assertEqual(antigravity_rules.owners(rules_dir), {("m1", "a"), ("m2", "b"), ("failed", "c")})

            # Edit r1 directly and place a foreign file matching r2's target name.
            p1 = rules_dir / r1.name
            p1.write_text(p1.read_text(encoding="utf-8") + "\nUser edit.\n", encoding="utf-8")
            foreign = rules_dir / "agent-kit-sync--other--f.md"
            foreign.write_text("# User written rule\n", encoding="utf-8")
            foreign_conflict = antigravity_rules.compose("other", "f", "Synced.\n")

            # Remove r2, update r1 (restoring over user edit with backup), keep ("failed", "c").
            second = antigravity_rules.write_rules(
                rules_dir,
                [r1, foreign_conflict],
                {("failed", "c")},
                backups,
            )
            self.assertIn(str(rules_dir / r2.name), second.removed)
            self.assertEqual(second.foreign, [str(foreign)])
            self.assertEqual(len(second.backed_up), 1)
            self.assertTrue(Path(second.backed_up[0]).is_file())
            self.assertEqual(p1.read_text(encoding="utf-8"), r1.text)
            self.assertEqual((rules_dir / kept.name).read_text(encoding="utf-8"), kept.text)
            self.assertEqual(foreign.read_text(encoding="utf-8"), "# User written rule\n")

            # Removing all owned rules cleans up owned files without touching the foreign file.
            cleared = antigravity_rules.write_rules(rules_dir, [], set(), backups)
            self.assertFalse(p1.exists())
            self.assertFalse((rules_dir / kept.name).exists())
            self.assertTrue(foreign.exists())
            self.assertEqual(len(cleared.removed), 2)

            # Without foreign files, clearing removes rules_dir itself even if already empty.
            foreign.unlink()
            antigravity_rules.write_rules(rules_dir, [r1], set(), backups)
            self.assertTrue(rules_dir.is_dir())
            antigravity_rules.write_rules(rules_dir, [], set(), backups)
            self.assertFalse(rules_dir.exists())
            rules_dir.mkdir()
            antigravity_rules.write_rules(rules_dir, [], set(), backups)
            self.assertFalse(rules_dir.exists())

    def test_ensure_manifest_and_remove_manifest(self):
        with tempfile.TemporaryDirectory(prefix="aks-ag-manifest-") as folder:
            plugin_dir = Path(folder) / "plugins" / ANTIGRAVITY_PLUGIN_NAME
            manifest = plugin_dir / "plugin.json"

            self.assertIsNone(antigravity_rules.ensure_manifest(plugin_dir))
            parsed = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(parsed["name"], ANTIGRAVITY_PLUGIN_NAME)
            self.assertEqual(parsed["displayName"], ANTIGRAVITY_PLUGIN_NAME)
            self.assertIsNone(antigravity_rules.ensure_manifest(plugin_dir))

            # Stale owned manifest is refreshed.
            manifest.write_text(json.dumps({"name": ANTIGRAVITY_PLUGIN_NAME, "version": "0.0.0"}), encoding="utf-8")
            self.assertIsNone(antigravity_rules.ensure_manifest(plugin_dir))
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), antigravity_rules.PLUGIN_MANIFEST)

            # Removing manifest deletes plugin.json and prunes empty plugin_dir.
            self.assertIsNone(antigravity_rules.remove_manifest(plugin_dir))
            self.assertFalse(manifest.exists())
            self.assertFalse(plugin_dir.exists())

            # Foreign plugin.json is never overwritten or removed.
            plugin_dir.mkdir(parents=True)
            manifest.write_text(json.dumps({"name": "someone-elses-plugin"}), encoding="utf-8")
            err = antigravity_rules.ensure_manifest(plugin_dir)
            self.assertIsNotNone(err)
            self.assertIn("another plugin", err)
            err_rm = antigravity_rules.remove_manifest(plugin_dir)
            self.assertIsNotNone(err_rm)
            self.assertTrue(manifest.exists())


if __name__ == "__main__":
    unittest.main()
