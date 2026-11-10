"""Sync must preserve existing project directories and their path references."""

import contextlib
import io
import json
import tempfile
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from support import ROOT, as_home
from agent_kit_sync import cli, render, toml
from agent_kit_sync.engine import Engine
from agent_kit_sync.paths import Layout
from agent_kit_sync.plan import Plan


class DirectoryPreservationTests(unittest.TestCase):
    def test_guard_matches_raw_and_json_escaped_network_share_paths(self):
        engine = Engine.__new__(Engine)
        engine.layout = Layout(Path("/kit"))
        engine.machine = SimpleNamespace(projects=Path("//server/share/Projects"), os="windows")
        target = str(engine.machine.projects / "_status/data").replace("/", "\\")
        for text in (target, json.dumps(target)):
            plan = Plan(cursor_blocks=[("rules", "rule", text)])
            with mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(Path, "is_dir", return_value=True), mock.patch.object(Path, "exists", return_value=False):
                engine._preserve_migrated_references(plan)
            self.assertEqual(plan.failed, ["rules"])

    def test_guard_reports_cursor_variant_override_and_effective_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            projects = root / "Projects"
            (projects / "status").mkdir(parents=True)
            layout = Layout(root / "kit")
            layout.state.mkdir(parents=True)
            (layout.state / "status-directory-migration.json").write_text('{}', encoding="utf-8")
            module_root = layout.config / "modules/rules"
            module_root.mkdir(parents=True)
            (module_root / "base.md").write_text("Current checkout is clean", encoding="utf-8")
            item = SimpleNamespace(name="rule", data={"file": "base.md", "variants": {"cursor": "cursor.md"}})
            module = SimpleNamespace(name="rules", root=module_root, items=[item])
            for kind in ("replace", "prepend", "append"):
                engine = Engine.__new__(Engine)
                engine.layout = layout
                engine.machine = SimpleNamespace(projects=projects, os="linux", overrides={"rules/rule": {"kind": kind, "file": "overrides/local.md"}})
                plan = Plan(cursor_blocks=[("rules", "rule", str(projects / "_status" / "data"))])
                engine._preserve_migrated_references(plan, [module], {"rules/rule": SimpleNamespace(commit="older-revision")})
                self.assertEqual(plan.failed, ["rules"])
                message = plan.errors[0]
                self.assertIn(str(module_root / "cursor.md"), message)
                self.assertNotIn(str(module_root / "base.md"), message)
                self.assertIn(str(layout.root / "overrides/local.md"), message)
                self.assertIn("older-revision", message)
                self.assertIn("Resolve the held decision or pin", message)

    def test_guard_accepts_markdown_delimiters_and_ignores_similar_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            projects = root / "Projects"
            (projects / "status").mkdir(parents=True)
            layout = Layout(root / "kit")
            layout.state.mkdir(parents=True)
            (layout.state / "status-directory-migration.json").write_text('{}', encoding="utf-8")
            engine = Engine.__new__(Engine)
            engine.layout = layout
            engine.machine = SimpleNamespace(projects=projects, os="linux")
            for suffix, blocked in (("]", True), (">", True), ("", True), ("/data", True), ("-other/data", False), ("_extra/data", False)):
                with self.subTest(suffix=suffix):
                    plan = Plan(cursor_blocks=[("rules", "rule", str(projects / "_status") + suffix)])
                    engine._preserve_migrated_references(plan)
                    self.assertEqual(bool(plan.failed), blocked)

    def test_migrated_reference_guard_covers_copies_cursor_and_escaped_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            projects = root / "Prójècts"
            (projects / "status").mkdir(parents=True)
            layout = Layout(root / "kit")
            layout.state.mkdir(parents=True)
            (layout.state / "status-directory-migration.json").write_text('{}', encoding="utf-8")
            engine = Engine.__new__(Engine)
            engine.layout = layout
            engine.machine = SimpleNamespace(projects=projects, os="windows")
            target = str(projects / "_status" / "data").replace("/", "\\")
            plans = [Plan(copies=[SimpleNamespace(module="rules", item="rule", app="kit", kind="file", data=target.encode(), source_rel="rule.md")]),
                     Plan(cursor_blocks=[("rules", "rule", target)]),
                     Plan(settings=[SimpleNamespace(item_key="rules/path", app="codex", value=target)]),
                     Plan(hooks=[SimpleNamespace(module="rules", name="guard", args=[target])])]
            for plan in plans:
                with self.subTest(plan=plan):
                    engine._preserve_migrated_references(plan)
                    self.assertIn("rules", plan.failed)
                    self.assertTrue(plan.errors)

    def test_previously_migrated_machine_keeps_working_references_until_source_is_corrected(self):
        with tempfile.TemporaryDirectory(prefix="aks-migrated-") as folder:
            home = Path(folder).resolve()
            projects = home / "Projects"
            active = projects / "status"
            active.mkdir(parents=True)
            (active / "commitments.md").write_bytes(b"existing data")
            layout = Layout(home / "kit")
            module = layout.config / "modules/rules"
            module.mkdir(parents=True)
            (home / ".codex").mkdir()
            layout.machine_file.write_text(toml.dumps({"name": "fixture", "apps": ["codex"], "projects": str(projects), "python": sys.executable}), encoding="utf-8")
            (layout.config / "kit.toml").write_text('format = 2\n[[modules]]\nname = "rules"\n', encoding="utf-8")
            (module / "module.toml").write_text('[[sections]]\nname = "rule"\nfile = "rule.md"\n', encoding="utf-8")
            source = module / "rule.md"
            source.write_text('Read {{kit.projects}}/status/commitments.md.\n', encoding="utf-8")
            with as_home(home), mock.patch.object(Engine, "commit_local"):
                first = Engine(layout)
                self.assertEqual(first.run(network=False).errors, [])
                installed = home / ".codex/AGENTS.md"
                original = installed.read_bytes()
                receipt = layout.state / "status-directory-migration.json"
                receipt.write_text(json.dumps({"version": 2, "state": "complete", "posix_move": {"phase": "complete"}}), encoding="utf-8")
                source.write_text('Read {{kit.projects}}/_status/commitments.md.\n', encoding="utf-8")
                blocked = Engine(layout)
                errors = blocked.run(network=False).errors
                self.assertTrue(errors)
                self.assertIn(str(source), errors[0])
                self.assertIn(str(projects / "_status"), errors[0])
                self.assertEqual(installed.read_bytes(), original)
                self.assertFalse((projects / "_status").exists())
                source.write_text('Read {{kit.projects}}/status/commitments.md.\n', encoding="utf-8")
                recovered = Engine(layout)
                self.assertEqual(recovered.run(network=False).errors, [])
                self.assertNotIn("module:rules", [a["key"] for a in recovered.alerts.open()])
            self.assertEqual((active / "commitments.md").read_bytes(), b"existing data")

    def test_sync_ignores_old_migration_flag_and_preserves_existing_data(self):
        with tempfile.TemporaryDirectory(prefix="aks-preserve-") as folder:
            home = Path(folder).resolve()
            projects = home / "Projects"
            old = projects / "_status"
            old.mkdir(parents=True)
            data = old / "commitments.md"
            data.write_bytes(b"existing private data")
            original_identity = old.stat().st_ino
            canonical = projects / "status"
            canonical.mkdir()
            (canonical / "unrelated").write_bytes(b"separate user directory")
            layout = Layout(home / "kit")
            module = layout.config / "modules/rules"
            module.mkdir(parents=True)
            (home / ".codex").mkdir()
            layout.machine_file.write_text(toml.dumps({"name": "fixture", "apps": ["codex"], "projects": str(projects), "python": sys.executable}), encoding="utf-8")
            (layout.config / "kit.toml").write_text('format = 2\n[migrations]\nproject_status_directory = 1\n[[modules]]\nname = "rules"\n', encoding="utf-8")
            (module / "module.toml").write_text('description = "Existing paths"\n[[sections]]\nname = "rule"\nfile = "rule.md"\n', encoding="utf-8")
            (module / "rule.md").write_text('Read {{kit.projects}}/_status/commitments.md.\n', encoding="utf-8")
            with as_home(home), mock.patch.object(Engine, "commit_local"):
                engine = Engine(layout)
                engine.alerts.raise_("config", "Old format error", "agent-kit-sync status")
                engine.alerts.raise_("status-directory-migration", "Old migration error", "agent-kit-sync status")
                report = engine.run(network=False)
            self.assertEqual(report.errors, [])
            self.assertEqual(old.stat().st_ino, original_identity)
            self.assertFalse(old.is_symlink())
            self.assertEqual(data.read_bytes(), b"existing private data")
            self.assertEqual((canonical / "unrelated").read_bytes(), b"separate user directory")
            self.assertIn(str(old / "commitments.md"), (home / ".codex/AGENTS.md").read_text())
            self.assertFalse((layout.state / "status-directory-migration.json").exists())
            keys = [alert["key"] for alert in engine.alerts.open()]
            self.assertNotIn("config", keys)
            self.assertNotIn("status-directory-migration", keys)

    def test_migration_command_cannot_move_existing_directories(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            cli.main(["migrate-status"])
        self.assertEqual(error.exception.code, 2)

    def test_render_preserves_each_existing_directory_name(self):
        values = render.Values(home="/home", projects="/Projects", worktrees="/work", module="/module", app="codex", os="linux", cli="aks", hooks={})
        for name in ("_status", "status", "Projects_status", "custom"):
            self.assertEqual(render.substitute("{{kit.projects}}/" + name + "/file", values), "/Projects/" + name + "/file")


if __name__ == "__main__":
    unittest.main()
