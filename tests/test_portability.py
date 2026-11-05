"""The check for script lines that work on one operating system and fail on another."""

import unittest

from support import ROOT  # noqa: F401  (puts the tool on the import path)
from agent_kit_sync import checks


def details(path, text, systems=checks.SYSTEMS, marks=None):
    return [finding.detail for finding in checks.portability_findings(path, text, systems, marks=marks)]


class PortabilityTests(unittest.TestCase):
    def test_known_windows_breakers_are_reported_with_the_system(self):
        cases = {
            "spawn npx": ("w.mjs", "const child = spawn('npx', ['jest'], { cwd });\n"),
            "command npx": ("w.mjs", "  return { command: 'npx', prefix: ['jest'] };\n"),
            "subprocess npx": ("w.py", "subprocess.run(['npx', 'jest'])\n"),
            "node_modules bin": ("w.mjs", "const local = path.join(cwd, 'node_modules', '.bin', 'jest');\n"),
            "shebang fake program": ("t.py", "FAKE = \"#!/bin/sh\\necho hi\\n\"\n"),
            "colon PATH": ("t.py", "env = {\"PATH\": f\"{bin}:/usr/bin:/bin\"}\n"),
            "shell true": ("h.py", "subprocess.run(command, shell=True)\n"),
            "unix import": ("h.py", "import fcntl\n"),
            "unix call": ("h.py", "os.killpg(group, 9)\n"),
            "fixed program path": ("h.py", "SHELL = \"/bin/sh\"\n"),
        }
        for name, (path, text) in cases.items():
            with self.subTest(name):
                found = details(path, text)
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].startswith("fails on Windows:"), found)

    def test_one_system_programs_name_the_systems_they_fail_on(self):
        self.assertTrue(details("h.py", "subprocess.run(['osascript', '-e', script])\n")[0].startswith("fails on Windows and Linux:"))
        self.assertTrue(details("h.py", "subprocess.run(['xdg-open', url])\n")[0].startswith("fails on macOS and Windows:"))
        self.assertTrue(details("h.py", "subprocess.run(['powershell', '-c', x])\n")[0].startswith("fails on macOS and Linux:"))

    def test_portable_code_passes(self):
        for text in (
            "import json, sys\npayload = json.load(sys.stdin)\n",
            "subprocess.run([sys.executable, str(HOOK)], input=data)\n",
            "env = {\"PATH\": os.pathsep.join([bin_dir, os.environ[\"PATH\"]])}\n",
            "command = data.get(\"command\", data.get(\"cmd\", \"\"))\n",
            "RUNNERS = new Set(['npx', 'pnpx', 'yarn']);\n",
            "check(result.stdout == \"#!/u\")\n",
            "#!/usr/bin/env python3\nprint('hi')\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(details("h.py" if "import" in text or "subprocess" in text else "h.mjs", text), [])

    def test_a_file_that_checks_its_system_is_not_reported_for_system_calls(self):
        text = "import sys\nif sys.platform == 'darwin':\n    subprocess.run(['osascript', '-e', s])\n"
        self.assertEqual(details("h.py", text), [])
        # Unguardable problems are still reported, because the check cannot see which branch they sit in.
        text = "import sys\nif sys.platform != 'win32':\n    subprocess.run(['npx', 'jest'])\n"
        self.assertEqual(len(details("h.py", text)), 1)

    def test_only_the_modules_systems_are_checked(self):
        self.assertEqual(details("h.py", "subprocess.run(['osascript'])\n", systems=("macos",)), [])
        self.assertEqual(details("h.py", "import fcntl\n", systems=("macos", "linux")), [])
        self.assertEqual(len(details("h.py", "import fcntl\n", systems=("windows",))), 1)

    def test_shell_and_windows_scripts_are_reported_whole(self):
        self.assertTrue(details("run.sh", "echo hi\n")[0].startswith("fails on Windows: a shell script"))
        self.assertTrue(details("run.ps1", "Write-Host hi\n")[0].startswith("fails on macOS and Linux: a Windows script"))
        self.assertEqual(details("run.sh", "echo hi\n", systems=("macos", "linux")), [])

    def test_a_marked_line_is_accepted_and_listed(self):
        marks = []
        text = "SHELL = \"/bin/sh\"  # agent-kit-sync: portable because Windows never reaches this line\n"
        self.assertEqual(details("h.py", text, marks=marks), [])
        self.assertEqual([m.detail for m in marks], ["marked portable: Windows never reaches this line"])
        marks = []
        self.assertEqual(details("run.sh", "#!/bin/sh\n# agent-kit-sync: portable because Git Bash runs it\necho hi\n", marks=marks), [])
        self.assertEqual(len(marks), 1)

    def test_only_added_lines_are_checked_for_an_edit(self):
        text = "import fcntl\nimport json\n"
        self.assertEqual(checks.portability_findings("h.py", text, lines=[2]), [])
        self.assertEqual(len(checks.portability_findings("h.py", text, lines=[1])), 1)

    def test_checker_runs_it_on_own_scripts_only(self):
        checker = checks.Checker(own_values=[])
        data = b"import fcntl\n"
        self.assertTrue([f for f in checker.file("m/h.py", data, own=True) if f.check == "portability"])
        self.assertFalse([f for f in checker.file("m/h.py", data, own=False) if f.check == "portability"])
        self.assertFalse([f for f in checker.file("m/h.py", data, own=True, systems=None) if f.check == "portability"])
        self.assertFalse([f for f in checker.file("m/notes.md", b"Run `npx jest` with spawn('npx').\n", own=True) if f.check == "portability"])


if __name__ == "__main__":
    unittest.main()
