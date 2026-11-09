"""Model POSIX namespace changes without launching native processes or syscalls."""

import copy
import errno
import os
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import ROOT
from agent_kit_sync import status_posix as migration
from agent_kit_sync.status_directory import PendingMigration


class Namespace:
    def __init__(self):
        self.nodes = {}
        self.serial = 0
        self.calls = []

    def add(self, path, kind="directory", target=None):
        self.serial += 1
        self.nodes[Path(path)] = (kind, self.serial, target)

    def lstat(self, path):
        try:
            kind, ino, _ = self.nodes[Path(path)]
        except KeyError:
            raise FileNotFoundError(errno.ENOENT, "absent", str(path)) from None
        return SimpleNamespace(st_dev=9, st_ino=ino, st_mode=(stat.S_IFDIR if kind == "directory" else stat.S_IFLNK) | 0o700)

    def readlink(self, path):
        return self.nodes[Path(path)][2]

    def symlink(self, target, path):
        if Path(path) in self.nodes:
            raise FileExistsError(str(path))
        self.calls.append(("symlink", Path(path)))
        self.add(path, "link", target)

    def hardlink(self, source, target):
        if Path(target) in self.nodes:
            raise FileExistsError(str(target))
        self.calls.append(("hardlink", Path(target)))
        self.nodes[Path(target)] = self.nodes[Path(source)]

    def exchange(self, source, target):
        self.calls.append(("exchange", Path(source), Path(target)))
        self.nodes[Path(source)], self.nodes[Path(target)] = self.nodes[Path(target)], self.nodes[Path(source)]

    def rename(self, source, target):
        if Path(target) in self.nodes:
            raise FileExistsError(str(target))
        self.calls.append(("rename", Path(source), Path(target)))
        self.nodes[Path(target)] = self.nodes.pop(Path(source))

    def unlink(self, path):
        self.calls.append(("unlink", Path(path)))
        if self.nodes[Path(path)][0] != "link":
            raise AssertionError("Attempted to unlink data")
        del self.nodes[Path(path)]


class PosixMoveTests(unittest.TestCase):
    def setUp(self):
        self.projects = Path(os.path.abspath("modeled-projects"))
        self.source = self.projects / "_status"
        self.target = self.projects / "status"
        self.layout = SimpleNamespace(root=self.projects.parent / "kit", state=self.projects.parent / "kit/state")
        self.fs = Namespace()
        for ancestor in reversed((self.projects,) + tuple(self.projects.parents)):
            self.fs.add(ancestor)
        self.fs.add(self.source)
        self.source_identity = self.fs.nodes[self.source]
        self.record = {"version": 2, "staged": {}, "retired": [], "explicit_sources": []}
        self.saved = []
        patches = {
            "_lstat": self.fs.lstat, "_readlink": self.fs.readlink,
            "_symlink": self.fs.symlink, "_hardlink": self.fs.hardlink,
            "_exchange": self.fs.exchange, "_rename_exclusive": self.fs.rename,
            "_unlink": self.fs.unlink, "_sync_directory": lambda path: None,
            "_probe": lambda path: None,
        }
        for name, replacement in patches.items():
            patcher = mock.patch.object(migration, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, record):
        self.saved.append(copy.deepcopy(record))

    def run_move(self, **kwargs):
        return migration.run(self.record, self.projects, self.layout, [self.source], save=self.save, **kwargs)

    def finish(self, **kwargs):
        return self.run_move(finalize=True, plan_id=self.record["plan_id"], **kwargs)

    def test_prepare_and_finalize_preserve_original_directory_identity(self):
        result = self.run_move()
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(self.fs.nodes[self.source], self.source_identity)
        self.assertEqual(self.fs.readlink(self.target), str(self.source))
        result = self.finish()
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.fs.nodes[self.target], self.source_identity)
        self.assertNotIn(self.source, self.fs.nodes)
        artifact = Path(result["posix_move"]["artifact"])
        self.assertEqual(self.fs.nodes[artifact][0], "link")
        self.assertFalse(result["posix_move"]["artifact_contains_backup_data"])
        self.assertFalse(any(call[0] == "unlink" and call[1] == self.source for call in self.fs.calls))

    def test_save_precedes_user_namespace_mutations(self):
        original = self.fs.symlink
        def checked(target, path):
            self.assertEqual(self.saved[-1]["posix_move"]["phase"], "seed-intent")
            original(target, path)
        with mock.patch.object(migration, "_symlink", checked):
            self.run_move()
        self.assertTrue(self.saved[-1]["posix_move"]["link_identity"])

    def test_finalize_rejects_wrong_plan_and_installed_old_reference(self):
        self.run_move()
        with self.assertRaises(PendingMigration):
            self.run_move(finalize=True, plan_id="wrong")
        with mock.patch("agent_kit_sync.status_directory._old_references", side_effect=PendingMigration("old reference")):
            with self.assertRaises(PendingMigration):
                self.finish(active_paths=[Path("AGENTS.md")])
        self.assertEqual(self.fs.nodes[self.source], self.source_identity)

    def test_crash_after_link_creation_recovers_from_saved_inode(self):
        original = self.fs.hardlink
        def interrupted(source, target):
            original(source, target)
            raise SystemExit("crash")
        with mock.patch.object(migration, "_hardlink", interrupted), self.assertRaises(SystemExit):
            self.run_move()
        self.record = copy.deepcopy(self.saved[-1])
        self.assertEqual(self.run_move()["state"], "prepared")
        self.assertEqual(self.finish()["state"], "complete")

    def test_crash_after_exchange_recovers_actual_namespace(self):
        self.run_move()
        original = self.fs.exchange
        def interrupted(source, target):
            original(source, target)
            raise SystemExit("crash")
        with mock.patch.object(migration, "_exchange", interrupted), self.assertRaises(SystemExit):
            self.finish()
        self.record = copy.deepcopy(self.saved[-1])
        self.assertEqual(self.finish()["state"], "complete")
        self.assertEqual(self.fs.nodes[self.target], self.source_identity)

    def test_crash_after_retirement_recovers_artifact(self):
        self.run_move()
        original = self.fs.rename
        def interrupted(source, target):
            original(source, target)
            raise SystemExit("crash")
        with mock.patch.object(migration, "_rename_exclusive", interrupted), self.assertRaises(SystemExit):
            self.finish()
        self.record = copy.deepcopy(self.saved[-1])
        self.assertEqual(self.finish()["state"], "complete")

    def test_existing_target_multiple_sources_and_explicit_sources_preserved(self):
        cases = ("target", "multiple", "explicit", "outside")
        for case in cases:
            with self.subTest(case=case):
                record = copy.deepcopy(self.record)
                sources = [self.source]
                if case == "target":
                    self.fs.add(self.target)
                elif case == "multiple":
                    sources.append(self.projects / "Projects_status")
                elif case == "explicit":
                    record["explicit_sources"] = [str(self.source)]
                else:
                    sources = [self.projects.parent / "Projects_status"]
                before = copy.deepcopy(self.fs.nodes)
                with self.assertRaises(PendingMigration):
                    migration.run(record, self.projects, self.layout, sources, save=self.save)
                self.assertEqual(self.fs.nodes, before)
                self.fs.nodes.pop(self.target, None)

    def test_unsupported_probe_fails_before_user_data_mutations(self):
        before = copy.deepcopy(self.fs.nodes)
        with mock.patch.object(migration, "_probe", side_effect=OSError("unsupported")), self.assertRaises(OSError):
            self.run_move()
        self.assertEqual(self.fs.nodes, before)
        self.assertNotIn("posix_move", self.record)

    def test_unexpected_entry_or_changed_identity_is_preserved(self):
        self.run_move()
        self.fs.add(self.target, "link", str(self.source))
        before = copy.deepcopy(self.fs.nodes)
        with self.assertRaises(PendingMigration):
            self.finish()
        self.assertEqual(self.fs.nodes, before)

    def test_retirement_collision_never_overwrites_unexpected_entry(self):
        self.run_move()
        artifact = Path(self.record["posix_move"]["artifact"])
        self.fs.add(artifact)
        before = copy.deepcopy(self.fs.nodes)
        with self.assertRaises(PendingMigration):
            self.finish()
        self.assertEqual(self.fs.nodes, before)

    def test_corrupt_receipt_paths_and_plan_are_rejected(self):
        self.run_move()
        for key, value in (("artifact", str(self.projects.parent / "outside")), ("source", str(self.projects / "other")), ("source_identity", [9, 9999])):
            with self.subTest(key=key):
                record = copy.deepcopy(self.record)
                record["posix_move"][key] = value
                before = copy.deepcopy(self.fs.nodes)
                with self.assertRaises(PendingMigration):
                    migration.run(record, self.projects, self.layout, [self.source], save=self.save)
                self.assertEqual(self.fs.nodes, before)

    def test_original_unlinked_state_recovers_after_intent_saved(self):
        with mock.patch.object(migration, "_symlink", side_effect=OSError("creation failed")), self.assertRaises(OSError):
            self.run_move()
        self.record = copy.deepcopy(self.saved[-1])
        self.assertEqual(self.run_move()["state"], "prepared")

    def test_seed_created_before_identity_save_is_preserved_as_pending(self):
        original = self.fs.symlink
        def interrupted(target, path):
            original(target, path)
            raise SystemExit("crash before ownership is durable")
        with mock.patch.object(migration, "_symlink", interrupted), self.assertRaises(SystemExit):
            self.run_move()
        self.record = copy.deepcopy(self.saved[-1])
        before = copy.deepcopy(self.fs.nodes)
        with self.assertRaises(PendingMigration):
            self.run_move()
        self.assertEqual(self.fs.nodes, before)
        self.assertEqual(self.fs.nodes[self.source], self.source_identity)

    def test_changed_parent_identity_is_preserved(self):
        self.run_move()
        self.fs.add(self.projects)
        before = copy.deepcopy(self.fs.nodes)
        with self.assertRaises(PendingMigration):
            self.finish()
        self.assertEqual(self.fs.nodes, before)

    def test_cross_filesystem_source_is_pending_before_probe_or_user_mutation(self):
        original = self.fs.lstat
        def another_filesystem(path):
            info = original(path)
            if path == self.source:
                info.st_dev = 10
            return info
        before = copy.deepcopy(self.fs.nodes)
        with mock.patch.object(migration, "_lstat", another_filesystem), mock.patch.object(migration, "_probe") as probe:
            with self.assertRaises(PendingMigration):
                self.run_move()
        probe.assert_not_called()
        self.assertEqual(self.fs.nodes, before)
        self.assertNotIn("posix_move", self.record)

    def test_sync_installation_overlap_is_rejected_before_probe(self):
        before = copy.deepcopy(self.fs.nodes)
        layout = SimpleNamespace(root=self.projects.parent, state=self.projects.parent / "state")
        with mock.patch.object(migration, "_probe") as probe, self.assertRaises(PendingMigration):
            migration.run(self.record, self.projects, layout, [self.source], save=self.save)
        probe.assert_not_called()
        self.assertEqual(self.fs.nodes, before)

    def test_finalization_save_failure_prevents_exchange(self):
        self.run_move()
        def failed(record):
            if record["posix_move"]["phase"] == "exchange-intent":
                raise OSError("receipt unavailable")
        with self.assertRaises(OSError):
            migration.run(self.record, self.projects, self.layout, [self.source], finalize=True,
                          plan_id=self.record["plan_id"], save=failed)
        self.assertEqual(self.fs.nodes[self.source], self.source_identity)

    def test_success_can_be_repeated_without_recreating_legacy_source(self):
        self.run_move()
        self.finish()
        before = copy.deepcopy(self.fs.nodes)
        result = migration.run(self.record, self.projects, self.layout, [], save=self.save)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.fs.nodes, before)

    def test_repository_marker_is_pending_before_probe(self):
        self.fs.add(self.source / ".git")
        before = copy.deepcopy(self.fs.nodes)
        with mock.patch.object(migration, "_probe") as probe, self.assertRaises(PendingMigration):
            self.run_move()
        probe.assert_not_called()
        self.assertEqual(self.fs.nodes, before)
        self.assertNotIn("posix_move", self.record)

    def test_repository_marker_added_after_preparation_blocks_exchange(self):
        self.run_move()
        self.fs.add(self.source / ".git")
        before = copy.deepcopy(self.fs.nodes)
        with self.assertRaises(PendingMigration):
            self.finish()
        self.assertEqual(self.fs.nodes, before)

    def test_completed_unexpected_artifact_is_preserved(self):
        self.run_move()
        self.finish()
        artifact = Path(self.record["posix_move"]["artifact"])
        self.fs.add(artifact)
        before = copy.deepcopy(self.fs.nodes)
        with self.assertRaises(PendingMigration):
            migration.run(self.record, self.projects, self.layout, [], save=self.save)
        self.assertEqual(self.fs.nodes, before)

    def test_completed_active_tree_can_be_restored_and_artifact_removed(self):
        self.run_move()
        self.finish()
        artifact = Path(self.record["posix_move"]["artifact"])
        del self.fs.nodes[artifact]
        self.fs.add(self.target)
        self.fs.add(self.projects)
        before = copy.deepcopy(self.fs.nodes)
        saved = len(self.saved)
        result = migration.run(self.record, self.projects, self.layout, [], save=self.save)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.fs.nodes, before)
        self.assertEqual(len(self.saved), saved)

    def test_completed_receipt_blocks_reappeared_legacy_and_invalid_active_tree(self):
        self.run_move()
        self.finish()
        completed = copy.deepcopy(self.fs.nodes)
        alternatives = (self.source, self.projects / (self.projects.name + "_status"),
                        self.projects.parent / (self.projects.name + "_status"))
        for legacy in alternatives:
            with self.subTest(legacy=legacy):
                self.fs.nodes = copy.deepcopy(completed)
                self.fs.add(legacy)
                before = copy.deepcopy(self.fs.nodes)
                with self.assertRaises(PendingMigration):
                    migration.run(self.record, self.projects, self.layout, [], save=self.save)
                self.assertEqual(self.fs.nodes, before)
        for condition in ("missing", "link", "linked-parent"):
            with self.subTest(condition=condition):
                self.fs.nodes = copy.deepcopy(completed)
                if condition == "missing":
                    del self.fs.nodes[self.target]
                elif condition == "link":
                    self.fs.add(self.target, "link", str(self.source))
                else:
                    self.fs.add(self.projects, "link", "elsewhere")
                before = copy.deepcopy(self.fs.nodes)
                with self.assertRaises(PendingMigration):
                    migration.run(self.record, self.projects, self.layout, [], save=self.save)
                self.assertEqual(self.fs.nodes, before)


class NativeRenameContractTests(unittest.TestCase):
    def test_platform_flags_and_signatures_use_documented_atomic_operations(self):
        first, second = Path("first"), Path("second")
        for platform, symbol, exchange_args, retire_args in (
            ("linux", "renameat2", (-100, b"first", -100, b"second", 2), (-100, b"first", -100, b"second", 1)),
            ("darwin", "renamex_np", (b"first", b"second", 2), (b"first", b"second", 4)),
        ):
            with self.subTest(platform=platform):
                function = mock.Mock(return_value=0)
                library = SimpleNamespace(**{symbol: function})
                with mock.patch.object(migration.sys, "platform", platform), mock.patch.object(migration.ctypes, "CDLL", return_value=library) as loader:
                    migration._exchange(first, second)
                    self.assertEqual(function.call_args.args, exchange_args)
                    migration._rename_exclusive(first, second)
                    self.assertEqual(function.call_args.args, retire_args)
                    loader.assert_called_with(None, use_errno=True)
                self.assertIs(function.restype, migration.ctypes.c_int)

    def test_native_errors_and_missing_symbols_fail_closed(self):
        with mock.patch.object(migration.sys, "platform", "linux"):
            with mock.patch.object(migration.ctypes, "CDLL", return_value=SimpleNamespace()):
                with self.assertRaises(PendingMigration):
                    migration._exchange(Path("one"), Path("two"))
            library = SimpleNamespace(renameat2=mock.Mock(return_value=-1))
            with mock.patch.object(migration.ctypes, "CDLL", return_value=library), mock.patch.object(migration.ctypes, "get_errno", return_value=errno.EEXIST):
                with self.assertRaises(FileExistsError):
                    migration._rename_exclusive(Path("one"), Path("two"))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(os.path.abspath("modeled-projects"))
        self.root = self.parent / ".owned-probe"
        self.fs = Namespace()
        self.fs.add(self.parent)
        def make_root(**kwargs):
            self.fs.add(self.root)
            return str(self.root)
        def rmdir(path):
            if any(item.parent == path for item in self.fs.nodes if item != path):
                raise OSError("directory is not empty")
            if self.fs.nodes[path][0] != "directory":
                raise AssertionError("Attempted to rmdir a link")
            del self.fs.nodes[path]
        patches = (
            mock.patch.object(migration.tempfile, "mkdtemp", side_effect=make_root),
            mock.patch.object(migration, "_lstat", self.fs.lstat),
            mock.patch.object(migration, "_readlink", self.fs.readlink),
            mock.patch.object(migration, "_symlink", self.fs.symlink),
            mock.patch.object(migration, "_hardlink", self.fs.hardlink),
            mock.patch.object(migration, "_exchange", self.fs.exchange),
            mock.patch.object(migration, "_rename_exclusive", self.fs.rename),
            mock.patch.object(migration, "_unlink", self.fs.unlink),
            mock.patch.object(migration, "_sync_directory", lambda path: None),
            mock.patch.object(Path, "mkdir", lambda path, **kwargs: self.fs.add(path)),
            mock.patch.object(Path, "rmdir", rmdir),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_probe_only_changes_its_owned_empty_nodes(self):
        with mock.patch.object(migration, "_rename_exclusive", wraps=self.fs.rename) as rename:
            migration._probe(self.parent)
        self.assertEqual(rename.call_args_list[0], mock.call(self.root / "directory", self.root / "link"))
        self.assertEqual(set(self.fs.nodes), {self.parent})

    def test_interrupted_probe_inspects_actual_post_exchange_state_for_cleanup(self):
        original = self.fs.exchange
        def interrupted(source, target):
            original(source, target)
            raise OSError("probe interrupted")
        with mock.patch.object(migration, "_exchange", interrupted), self.assertRaises(OSError):
            migration._probe(self.parent)
        self.assertEqual(set(self.fs.nodes), {self.parent})

    def test_probe_preserves_an_unknown_entry_and_its_parent(self):
        unknown = self.root / "unknown"
        def interrupted(source, target):
            self.fs.add(unknown)
            raise OSError("probe interrupted")
        with mock.patch.object(migration, "_exchange", interrupted), self.assertRaises(OSError):
            migration._probe(self.parent)
        self.assertEqual(set(self.fs.nodes), {self.parent, self.root, unknown})


if __name__ == "__main__":
    unittest.main()
