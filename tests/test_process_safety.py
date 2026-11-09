"""Bounded checks for hidden children, worker ownership, and test isolation."""

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

from support import ROOT
from agent_kit_sync import background, cli, processes, testing
from agent_kit_sync.paths import Layout
from agent_kit_sync.state import request_rerun


class LaunchTests(unittest.TestCase):
    def test_pass_cap_preserves_pending_work(self):
        with tempfile.TemporaryDirectory() as root:
            layout = Layout(Path(root))
            args = SimpleNamespace(background=False, quiet_wait=False)
            def one_run(*unused):
                request_rerun(layout)
                return 0
            with mock.patch.object(cli, "_one_run", side_effect=one_run) as run:
                self.assertEqual(cli.cmd_sync(args, layout), 0)
                self.assertEqual(run.call_count, 3)
            self.assertTrue((layout.state / "rerun").is_file())

    def test_busy_direct_background_command_does_not_wait_for_quiet_edits(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(background, "retain_worker_lease", return_value=False), mock.patch.object(cli, "_recent_edit") as inspect:
            self.assertEqual(cli.cmd_sync(SimpleNamespace(background=True, quiet_wait=True), Layout(Path(root))), 0)
            inspect.assert_not_called()

    def test_windows_options_hide_console_and_preserve_other_flags(self):
        with mock.patch.object(processes.sys, "platform", "win32"):
            self.assertEqual(processes.options({"creationflags": 0x200})["creationflags"], 0x08000200)
            for visible in (0x8, 0x10):
                with self.assertRaises(ValueError):
                    processes.options({"creationflags": visible})

    def test_posix_options_are_unchanged(self):
        with mock.patch.object(processes.sys, "platform", "linux"):
            self.assertEqual(processes.options({"close_fds": True}), {"close_fds": True})

    def test_all_library_and_test_launches_use_shared_helper(self):
        violations = []
        for folder in (ROOT / "agent_kit_sync", ROOT / "tests"):
            for path in folder.glob("*.py"):
                if path.name == "processes.py":
                    continue
                for node in ast.walk(ast.parse(path.read_bytes())):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" and node.func.attr in ("run", "Popen", "call", "check_output", "check_call"):
                        violations.append(f"{path.name}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_isolated_tests_explicitly_prevent_background_sync(self):
        env = testing.isolated_env(Path("unused"))
        self.assertEqual(env["AGENT_KIT_SYNC_TEST"], "1")
        self.assertEqual(env["AGENT_KIT_SYNC_NO_BACKGROUND"], "1")
        with mock.patch.dict(os.environ, env), mock.patch.object(processes, "popen") as spawn:
            self.assertFalse(background.start(["unused"], Path("unused")))
            spawn.assert_not_called()

    def test_busy_worker_never_starts_another_process(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"AGENT_KIT_SYNC_TEST": "0", "AGENT_KIT_SYNC_NO_BACKGROUND": ""}), mock.patch.object(background, "reserve", return_value=None), mock.patch.object(processes, "popen") as spawn:
            self.assertFalse(background.start(["unused"], root))
            self.assertTrue((Path(root) / "state/rerun").is_file())
            spawn.assert_not_called()

    def test_failed_start_releases_reservation(self):
        lease = mock.Mock()
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"AGENT_KIT_SYNC_TEST": "0", "AGENT_KIT_SYNC_NO_BACKGROUND": ""}), mock.patch.object(background, "reserve", return_value=lease), mock.patch.object(processes, "popen", side_effect=OSError("launch refused")):
            with self.assertRaises(OSError):
                background.start(["unused"], root)
            lease.close.assert_called_once()

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows console ownership")
    def test_one_native_child_has_no_console(self):
        code = "import ctypes,json; print(json.dumps({'console':bool(ctypes.windll.kernel32.GetConsoleWindow())}))"
        result = processes.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"console": False})

    def test_native_worker_lease_is_exclusive_and_released_after_exit(self):
        # One short-lived child, with a file handshake instead of a process stress loop.
        import time
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"AGENT_KIT_SYNC_TEST": "0", "AGENT_KIT_SYNC_NO_BACKGROUND": ""}):
            path = Path(root)
            code = "import sys,time; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from agent_kit_sync.background import retain_worker_lease; assert retain_worker_lease(sys.argv[2]); p=Path(sys.argv[2]); (p/'ready').write_text('ready'); deadline=time.monotonic()+5;\nwhile not (p/'finish').exists() and time.monotonic()<deadline: time.sleep(.02)\n"
            self.assertTrue(background.start([sys.executable, "-I", "-S", "-c", code, str(ROOT), root], root))
            try:
                deadline = time.monotonic() + 3
                while not (path / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((path / "ready").exists(), (path / "logs/sync-background.log").read_bytes())
                self.assertFalse(background.start(["must-never-launch"], root))
            finally:
                (path / "finish").write_text("finish", encoding="utf-8")
                deadline = time.monotonic() + 3
                released = None
                while released is None and time.monotonic() < deadline:
                    released = background.reserve(root)
                    if released is None:
                        time.sleep(.02)
                self.assertIsNotNone(released)
                if released:
                    released.close()


if __name__ == "__main__":
    unittest.main()
