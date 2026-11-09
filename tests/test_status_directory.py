"""Small filesystem fixtures for data-preserving status-directory migration."""

import json
import os
import tempfile
import sys
import io
import contextlib
import stat
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, as_home
from agent_kit_sync import cli, model, render, status_directory as migration, toml
from agent_kit_sync.engine import Engine
from agent_kit_sync.paths import Layout
from agent_kit_sync.state import run_lock


class StatusDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aks-status-")
        self.root = Path(self.temp.name)
        self.projects = self.root / "Projects"
        self.projects.mkdir()
        self.layout = Layout(self.root / "kit")
        self.old = self.projects / "_status"
        self.canonical = self.projects / "status"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, data=b"original"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def prepare(self, **kwargs):
        return migration.migrate_status(self.layout, self.projects, required=True, **kwargs)

    def finish(self, prepared, **kwargs):
        return self.prepare(finalize=True, plan_id=prepared["plan_id"], **kwargs)

    def test_prepare_preserves_sources_then_finalize_retains_whole_tree(self):
        self.write(self.old / "state/commitments.md")
        (self.old / "empty").mkdir()
        prepared = self.prepare()
        self.assertEqual(prepared["state"], "prepared")
        self.assertEqual((self.canonical / "state/commitments.md").read_bytes(), b"original")
        self.assertTrue(self.old.exists())
        result = self.finish(prepared)
        self.assertEqual(result["state"], "complete")
        self.assertFalse(self.old.exists())
        retained = Path(result["retired"][0]["retained"])
        self.assertEqual((retained / "state/commitments.md").read_bytes(), b"original")
        self.assertTrue((retained / "empty").is_dir())
        self.assertEqual(retained.parent, self.old.parent)
        self.assertEqual(self.prepare()["state"], "complete")

    def test_all_aliases_preflight_before_any_copy_or_retirement(self):
        self.write(self.old / "exclusions.md", b"first")
        sibling = self.projects.parent / (self.projects.name + "_status")
        self.write(sibling / "exclusions.md", b"second")
        result = self.prepare()
        self.assertEqual(result["state"], "pending")
        self.assertFalse(self.canonical.exists())
        self.assertEqual((self.old / "exclusions.md").read_bytes(), b"first")
        self.assertEqual((sibling / "exclusions.md").read_bytes(), b"second")

    def test_existing_canonical_data_is_never_overwritten(self):
        self.write(self.old / "exclusions.md", b"legacy")
        self.write(self.canonical / "exclusions.md", b"canonical")
        self.assertEqual(self.prepare()["state"], "pending")
        self.assertEqual((self.old / "exclusions.md").read_bytes(), b"legacy")
        self.assertEqual((self.canonical / "exclusions.md").read_bytes(), b"canonical")

    def test_duplicate_files_from_multiple_aliases_are_preserved(self):
        self.write(self.old / "notes.md")
        sibling = self.projects.parent / (self.projects.name + "_status")
        self.write(sibling / "notes.md")
        result = self.finish(self.prepare())
        self.assertEqual(result["state"], "complete")
        self.assertEqual(len(result["retired"]), 2)
        self.assertTrue(all((Path(e["retained"]) / "notes.md").read_bytes() == b"original" for e in result["retired"]))

    def test_installed_legacy_reference_blocks_retirement(self):
        self.write(self.old / "notes.md")
        prepared = self.prepare()
        installed = self.root / "AGENTS.md"
        installed.write_text("Read " + str(self.old / "notes.md"), encoding="utf-8")
        self.assertEqual(self.finish(prepared, active_paths=[installed])["state"], "pending")
        self.assertTrue(self.old.exists())

    def test_reference_scan_covers_utf16_json_and_markdown_paths(self):
        path = self.root / "settings.json"
        source = Path(r"\\server\share\Projects\_status")
        for text in (json.dumps({"path": str(source)}), "[old path](" + str(source) + ")"):
            path.write_bytes(text.encode("utf-16"))
            with self.assertRaises(migration.PendingMigration):
                migration._old_references([path], [source])

    def test_completed_rename_is_recovered_after_interrupted_receipt_update(self):
        self.write(self.old / "notes.md")
        prepared = self.prepare()
        original = Path.rename
        def interrupted(path, target):
            original(path, target)
            raise SystemExit("simulated interruption")
        with mock.patch.object(Path, "rename", interrupted), self.assertRaises(SystemExit):
            self.finish(prepared)
        recovered = self.prepare()
        self.assertEqual(recovered["state"], "complete")
        self.assertEqual(recovered["retired"][0]["phase"], "retained")
        self.assertEqual((Path(recovered["retired"][0]["retained"]) / "notes.md").read_bytes(), b"original")

    def test_source_edit_after_prepare_keeps_both_versions(self):
        self.write(self.old / "notes.md")
        prepared = self.prepare()
        self.write(self.old / "notes.md", b"latest")
        self.assertEqual(self.finish(prepared)["state"], "pending")
        self.assertEqual((self.old / "notes.md").read_bytes(), b"latest")
        self.assertEqual((self.canonical / "notes.md").read_bytes(), b"original")

    def test_late_write_at_rename_is_retained_and_reported(self):
        self.write(self.old / "notes.md")
        prepared = self.prepare()
        original = Path.rename
        def concurrent_rename(path, target):
            result = original(path, target)
            (target / "notes.md").write_bytes(b"late writer")
            return result
        with mock.patch.object(Path, "rename", concurrent_rename):
            result = self.finish(prepared)
        self.assertEqual(result["state"], "pending")
        self.assertEqual((Path(result["retired"][0]["retained"]) / "notes.md").read_bytes(), b"late writer")
        self.assertEqual((self.canonical / "notes.md").read_bytes(), b"original")
        self.assertEqual(self.prepare()["state"], "pending")

    def test_failed_rename_is_retryable_without_losing_data(self):
        self.write(self.old / "notes.md")
        prepared = self.prepare()
        with mock.patch.object(Path, "rename", side_effect=PermissionError("writer holds directory")):
            self.assertEqual(self.finish(prepared)["state"], "pending")
        retry = self.prepare()
        self.assertEqual(self.finish(retry)["state"], "complete")

    def test_explicit_old_location_is_local_and_supported(self):
        old_location = self.root / "other-volume/Previous_status"
        self.write(old_location / "notes.md")
        prepared = self.prepare(explicit_sources=[old_location])
        result = self.finish(prepared)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["explicit_sources"], [str(old_location)])
        self.assertFalse(old_location.exists())

    @unittest.skipUnless(os.environ.get("AKS_STATUS_TEST_VOLUME"), "requires an explicitly authorized second-volume test root")
    def test_native_cross_volume_copy_and_same_volume_retention(self):
        base = Path(os.environ["AKS_STATUS_TEST_VOLUME"]).resolve()
        self.assertTrue(base.is_absolute() and base.is_dir())
        with tempfile.TemporaryDirectory(prefix=".agent-kit-sync-status-test-", dir=base) as folder:
            owned = Path(folder)
            self.assertEqual(owned.resolve().parent, base)
            projects = owned / "Projects"
            projects.mkdir()
            if projects.stat().st_dev == self.root.stat().st_dev:
                self.skipTest("the selected root is not a different filesystem device")
            old_location = self.root / "previous/Old_status"
            self.write(old_location / "notes.md", b"cross-volume fixture")
            prepared = migration.migrate_status(self.layout, projects, required=True, explicit_sources=[old_location])
            self.assertEqual(prepared["state"], "prepared")
            result = migration.migrate_status(self.layout, projects, required=True, finalize=True, plan_id=prepared["plan_id"])
            self.assertEqual(result["state"], "complete")
            self.assertEqual((projects / "status/notes.md").read_bytes(), b"cross-volume fixture")
            retained = Path(result["retired"][0]["retained"])
            self.assertEqual(retained.parent, old_location.parent)
            self.assertEqual((retained / "notes.md").read_bytes(), b"cross-volume fixture")

    def test_missing_explicit_location_is_not_reported_complete(self):
        result = self.prepare(explicit_sources=[self.root / "unmounted/Old_status"])
        self.assertEqual(result["state"], "pending")
        self.assertFalse(self.canonical.exists())

    def test_overlapping_source_and_canonical_are_rejected(self):
        self.write(self.canonical / "notes.md")
        result = self.prepare(explicit_sources=[self.canonical])
        self.assertEqual(result["state"], "pending")
        self.assertEqual((self.canonical / "notes.md").read_bytes(), b"original")

    def test_corrupt_receipt_is_preserved_before_any_data_change(self):
        self.write(self.old / "notes.md")
        receipt = self.layout.state / "status-directory-migration.json"
        self.write(receipt, b"{broken")
        self.assertEqual(self.prepare()["state"], "pending")
        self.assertEqual(receipt.read_bytes(), b"{broken")
        self.assertTrue(self.old.exists())
        self.assertFalse(self.canonical.exists())

    def test_cli_receipt_is_read_only_through_the_bounded_loader(self):
        self.write(self.old / "notes.md")
        receipt = self.layout.state / "status-directory-migration.json"
        self.write(receipt, b" " * 17)
        machine = SimpleNamespace(projects=self.projects, app_homes={})
        for args in (["migrate-status"], ["migrate-status", "--finalize"]):
            output = io.StringIO()
            with mock.patch.object(cli, "layout_from_env", return_value=self.layout), mock.patch.object(model, "load_machine", return_value=machine), mock.patch.object(migration, "MAX_RECEIPT_BYTES", 16), mock.patch.object(Path, "read_text", side_effect=AssertionError("Unbounded receipt reader")), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(args), 1)
            self.assertEqual(json.loads(output.getvalue())["state"], "pending")
        self.assertEqual(receipt.read_bytes(), b" " * 17)
        self.assertTrue(self.old.exists())

    def test_deeply_nested_malformed_receipt_is_pending(self):
        receipt = self.layout.state / "status-directory-migration.json"
        raw = b"[" * 1100 + b"0" + b"]" * 1100
        self.write(receipt, raw)
        self.assertEqual(self.prepare()["state"], "pending")
        self.assertEqual(receipt.read_bytes(), raw)

    def test_lock_loser_does_not_write_the_receipt(self):
        self.write(self.old / "notes.md")
        self.prepare()
        receipt = self.layout.state / "status-directory-migration.json"
        before = receipt.read_bytes()
        with run_lock(Layout(self.layout.state / "status-directory-migration")):
            self.assertEqual(self.prepare()["state"], "pending")
        self.assertEqual(receipt.read_bytes(), before)

    def test_resource_bound_does_not_retire_sources(self):
        self.write(self.old / "notes.md")
        with mock.patch.object(migration, "MAX_FILE_BYTES", 1):
            self.assertEqual(self.prepare()["state"], "pending")
        self.assertTrue(self.old.exists())
        self.assertFalse(self.canonical.exists())

    def test_read_only_file_permission_is_preserved(self):
        self.write(self.old / "notes.md")
        (self.old / "notes.md").chmod(0o400)
        prepared = self.prepare()
        self.assertEqual(prepared["state"], "prepared")
        self.assertFalse((self.canonical / "notes.md").stat().st_mode & stat.S_IWRITE)
        self.assertEqual(self.finish(prepared)["state"], "complete")

    def test_repository_and_links_are_not_followed(self):
        self.write(self.old / ".git/config")
        self.assertEqual(self.prepare()["state"], "pending")
        self.assertTrue((self.old / ".git/config").exists())


class StatusRenderingTests(unittest.TestCase):
    def values(self, projects, enabled):
        system = "windows" if ":" in projects or projects.startswith("\\\\") else "linux"
        return render.Values(home="/h", projects=projects, worktrees="/w", module="/m", app="codex", os=system, cli="aks", hooks={}, canonical_status=enabled)

    def test_opt_in_corrects_old_and_concatenated_templates(self):
        for root in ("/mnt/projects", "D:\\Projects", "/Volumes/Data/Projects"):
            for source in ("{{kit.projects}}/_status/state/commitments.md", "{{kit.projects}}_status/exclusions.md"):
                text = render.render_text(source, self.values(root, True)).text
                self.assertIn(root + "/status/", text)
                self.assertNotIn("_status", text)
            text = render.render_text("{{kit.projects}}/projects_status/exclusions.md", self.values(root, True)).text
            self.assertEqual(text, root + "/status/exclusions.md")

    def test_unrelated_users_and_literal_examples_are_unchanged(self):
        source = "{{kit.projects}}/_status/notes.md"
        self.assertIn("/_status/", render.render_text(source, self.values("/p", False)).text)
        self.assertEqual(render.render_text("{{kit.literal projects}}/_status", self.values("/p", True)).text, "{{kit.projects}}/_status")

    def test_carry_back_preserves_unedited_original_source(self):
        source = "Read {{kit.projects}}/_status/notes.md.\nSecond line.\n"
        values = self.values("/p", True)
        shown = render.render_text(source, values).text
        updated = render.carry_back_text(source, shown, shown.replace("Second", "Changed"), values)
        self.assertIn("{{kit.projects}}/_status/notes.md", updated)
        self.assertIn("Changed line", updated)


class FormatGateTests(unittest.TestCase):
    def test_future_schema_does_not_start_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            layout = Layout(Path(folder))
            layout.config.mkdir(parents=True)
            (layout.config / "kit.toml").write_bytes(b'format = 3\n[migrations]\nproject_status_directory = 1\n')
            self.assertFalse(migration.migration_enabled(layout))
            with self.assertRaisesRegex(model.ConfigError, "must update first"):
                model.load_inventory(layout.config)

    def test_older_client_gate_requires_update_before_loading_modules(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "kit.toml").write_bytes(b'format = 2\n[migrations]\nproject_status_directory = 1\n')
            with mock.patch.object(model, "FORMAT", 1), self.assertRaisesRegex(model.ConfigError, "must update first"):
                model.load_inventory(root)
            self.assertEqual(model.load_inventory(root).format, 2)

    def test_migration_requires_gate_but_plain_format_one_stays_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "kit.toml"
            config.write_bytes(b'format = 1\n')
            self.assertEqual(model.load_inventory(root).format, 1)
            config.write_bytes(b'format = 1\n[migrations]\nproject_status_directory = 1\n')
            with self.assertRaisesRegex(model.ConfigError, "requires kit.toml format = 2"):
                model.load_inventory(root)


class DirectoryPrivacyTests(unittest.TestCase):
    def info(self, mode, uid=10, gid=20):
        return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=uid, st_gid=gid)

    def test_existing_public_destination_cannot_replace_private_directory(self):
        self.assertFalse(migration._posix_access_at_most(self.info(0o700), self.info(0o755), 10))
        self.assertTrue(migration._posix_access_at_most(self.info(0o700), self.info(0o700), 10))

    def test_private_ancestor_is_part_of_effective_access(self):
        sources = [self.info(0o755), self.info(0o700)]
        self.assertFalse(migration._posix_chain_private(sources, [self.info(0o755)], 10))
        self.assertTrue(migration._posix_chain_private(sources, [self.info(0o755), self.info(0o700)], 10))

    def test_unrelated_destination_group_cannot_gain_access(self):
        self.assertFalse(migration._posix_access_at_most(self.info(0o750, gid=20), self.info(0o750, gid=30), 10))

    @unittest.skipUnless(os.name == "nt", "Windows directory access-control contract")
    def test_windows_directory_acl_difference_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / "source", root / "target"
            source.mkdir()
            target.mkdir()
            with mock.patch.object(migration, "_windows_acl", side_effect=lambda path: b"private" if path == source else b"public"):
                with self.assertRaises(migration.PendingMigration):
                    migration._directory_privacy(source, target)


class EngineStatusMigrationTests(unittest.TestCase):
    def test_install_switches_references_before_whole_directory_retirement(self):
        with tempfile.TemporaryDirectory(prefix="aks-status-engine-") as folder:
            home = Path(folder)
            projects = home / "Projects"
            old = projects / "_status"
            old.mkdir(parents=True)
            (old / "exclusions.md").write_bytes(b"private local policy")
            layout = Layout(home / ".agent-kit-sync")
            module = layout.config / "modules/writing"
            module.mkdir(parents=True)
            (home / ".codex").mkdir()
            layout.machine_file.write_bytes(toml.dumps({"name":"fixture", "apps":["codex"], "projects":str(projects), "python":sys.executable}).encode())
            (layout.config / "kit.toml").write_bytes(b'format = 2\n[migrations]\nproject_status_directory = 1\n[[modules]]\nname = "writing"\n')
            (module / "module.toml").write_bytes(b'description = "Status reference"\n[[sections]]\nname = "rule"\nfile = "rule.md"\n')
            (module / "rule.md").write_bytes(b'Read {{kit.projects}}/_status/exclusions.md.\n')
            with as_home(home), mock.patch.object(Engine, "commit_local"):
                engine = Engine(layout)
                original = engine._write_sections
                def install_sections(plan):
                    self.assertTrue(old.exists())
                    return original(plan)
                with mock.patch.object(engine, "_write_sections", side_effect=install_sections):
                    report = engine.run(network=False)
            self.assertEqual(report.errors, [])
            self.assertFalse(old.exists())
            self.assertIn(str(projects) + "/status/exclusions.md", (home / ".codex/AGENTS.md").read_text(encoding="utf-8"))
            receipt = json.loads((layout.state / "status-directory-migration.json").read_text())
            self.assertEqual(receipt["state"], "complete")
            self.assertEqual((Path(receipt["retired"][0]["retained"]) / "exclusions.md").read_bytes(), b"private local policy")
