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
    def test_migrated_reference_guard_covers_copies_cursor_and_escaped_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            projects = root / "Projects"
            (projects / "status").mkdir(parents=True)
            layout = Layout(root / "kit")
            layout.state.mkdir(parents=True)
            (layout.state / "status-directory-migration.json").write_text('{}', encoding="utf-8")
            engine = Engine.__new__(Engine)
            engine.layout = layout
            engine.machine = SimpleNamespace(projects=projects, os="windows")
            target = str(projects / "_status" / "data").replace("/", "\\")
            plans = [Plan(copies=[SimpleNamespace(module="rules", data=target.encode())]),
                     Plan(cursor_blocks=[("rules", "rule", target)]),
                     Plan(settings=[SimpleNamespace(item_key="rules/path", value=target)])]
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
                self.assertTrue(blocked.run(network=False).errors)
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
