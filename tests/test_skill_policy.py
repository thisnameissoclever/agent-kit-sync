"""Repository skill policy applies without changing unrelated approval boundaries."""

import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

from support import ROOT, TwoMachines, as_home
from agent_kit_sync import gitops, toml
from agent_kit_sync.engine import Engine
from agent_kit_sync.model import load_module
from agent_kit_sync.model import ConfigError
from agent_kit_sync.paths import Layout
from agent_kit_sync.state import Index


class SkillPolicyTests(unittest.TestCase):
    def make_engine(self, home, repository_policy=None, local_policy=None):
        layout = Layout(home / "kit")
        module = layout.config / "modules/work"
        (module / "skills/review").mkdir(parents=True)
        (layout.config / ".git").mkdir()
        config = {"format": 2, "modules": [{"name": "work"}]}
        if repository_policy is not None:
            config["defaults"] = {"skills": repository_policy}
        (layout.config / "kit.toml").write_text(toml.dumps(config), encoding="utf-8")
        layout.machine_file.write_text(toml.dumps({"name": "fixture", "apps": [], "python": sys.executable,
                                                  "policy": local_policy or {}}), encoding="utf-8")
        (module / "module.toml").write_text('[[skills]]\nname = "review"\ndir = "skills/review"\n[[hooks]]\nname = "guard"\nfile = "guard.py"\nevents = ["pre_tool_use"]\n', encoding="utf-8")
        (module / "skills/review/SKILL.md").write_text('---\nname: review\ndescription: Review code\n---\nReview the accepted scope.\n', encoding="utf-8")
        (module / "guard.py").write_text('pass\n', encoding="utf-8")
        (module / "skills/review/helper.py").write_text('pass\n', encoding="utf-8")
        return Engine(layout), load_module(module, "work", local=False)

    def effective(self, engine, module):
        with mock.patch("agent_kit_sync.engine.gitops.head", return_value="current"):
            result, _trees = engine._effective_modules([module], incoming=True)
        return [item.key for item in result[0].items]

    def test_repo_policy_applies_skills_and_retires_obsolete_approval_hold(self):
        with tempfile.TemporaryDirectory() as folder, as_home(Path(folder)):
            engine, module = self.make_engine(Path(folder), "apply")
            held = engine.held.add("incoming:work/review", "new-item", "Old request", ["apply", "decline"], item_key="work/review")
            engine.report.held.append(held["id"])
            engine.alerts.raise_("incoming:work/review", "Old request", "agent-kit-sync held")
            saved = engine.held.add("held:work/review", "edit", "Saved local edit", ["retry"], {"edit": b"keep this"}, item_key="work/review")
            self.assertIn("work/review", self.effective(engine, module))
            self.assertIsNone(engine.held.find(held["id"]))
            self.assertNotIn(held["id"], engine.report.held)
            self.assertEqual(engine.held.blob(saved, "edit"), b"keep this")
            self.assertNotIn("incoming:work/review", [a["key"] for a in engine.alerts.open()])
            self.assertTrue(any(h["item_key"] == "work/guard" for h in engine.held.all()))

    def test_repo_policy_applies_skill_updates_even_when_other_changes_ask(self):
        with tempfile.TemporaryDirectory() as folder, as_home(Path(folder)):
            engine, module = self.make_engine(Path(folder), "apply", {"other_changes": "ask", "code_changes": "ask"})
            engine.index.items["work/review"] = {"fp": "previous", "commit": "previous"}
            with mock.patch.object(engine, "_item_at", return_value=None):
                self.assertIn("work/review", self.effective(engine, module))
            self.assertFalse(any(h["item_key"] == "work/review" for h in engine.held.all()))

    def test_explicit_local_ask_overrides_repo_automatic_skills(self):
        with tempfile.TemporaryDirectory() as folder, as_home(Path(folder)):
            engine, module = self.make_engine(Path(folder), "apply", {"skills": "ask"})
            self.assertNotIn("work/review", self.effective(engine, module))

    def test_without_skill_policy_existing_new_item_policy_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder, as_home(Path(folder)):
            engine, module = self.make_engine(Path(folder))
            self.assertNotIn("work/review", self.effective(engine, module))

    def test_invalid_repository_skill_policy_is_reported(self):
        with tempfile.TemporaryDirectory() as folder, as_home(Path(folder)):
            engine, _module = self.make_engine(Path(folder), "wrong")
            with self.assertRaisesRegex(ConfigError, "defaults.skills"):
                engine.load_modules()

    def test_invalid_local_skill_policy_still_requires_local_correction(self):
        with tempfile.TemporaryDirectory() as folder, as_home(Path(folder)):
            with self.assertRaisesRegex(ConfigError, "policy skills"):
                self.make_engine(Path(folder), None, {"skills": "wrong"})

    def test_failed_recovery_saves_fetch_timestamp_for_throttling(self):
        with tempfile.TemporaryDirectory() as folder, as_home(Path(folder)):
            engine, _module = self.make_engine(Path(folder), "wrong")
            def fetched():
                engine.index.last_fetch = 123.0
            with mock.patch("agent_kit_sync.engine.gitops.has_remote", return_value=True), mock.patch.object(engine, "merge_remote", side_effect=fetched):
                report = engine.run(network=True)
            self.assertTrue(report.errors)
            self.assertEqual(Index(engine.layout).last_fetch, 123.0)

    def test_push_retry_uses_policy_from_the_merge_before_installing(self):
        for initial, incoming in (("apply", "ask"), ("ask", "apply")):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as folder, as_home(Path(folder)):
                engine, module = self.make_engine(Path(folder), initial)
                observed = []
                def merged(*args, **kwargs):
                    kit = engine.layout.config / "kit.toml"
                    data = toml.load_file(kit)
                    data["defaults"]["skills"] = incoming
                    kit.write_text(toml.dumps(data), encoding="utf-8")
                    return type("Result", (), {"returncode": 0, "stdout": b""})()
                def installed(modules, incoming=True):
                    observed.append(self.effective(engine, module))
                with mock.patch("agent_kit_sync.engine.gitops.head", return_value="local"), mock.patch("agent_kit_sync.engine.gitops.rev", side_effect=["old-remote", "new-remote"]), mock.patch("agent_kit_sync.engine.gitops.upstream", return_value="origin/main"), mock.patch("agent_kit_sync.engine.gitops.is_ancestor", side_effect=lambda repo, first, second: first == "local" and second == "new-remote"), mock.patch("agent_kit_sync.engine.gitops.push", side_effect=[False, True]), mock.patch("agent_kit_sync.engine.gitops.branch", return_value="main"), mock.patch("agent_kit_sync.engine.gitops.fetch"), mock.patch("agent_kit_sync.engine.gitops.run", side_effect=merged), mock.patch.object(engine, "_set_aside_uncommitted"), mock.patch.object(engine, "install", side_effect=installed):
                    engine.push()
                self.assertEqual(engine.machine.policy.get("skills"), incoming)
                self.assertEqual("work/review" in observed[0], incoming == "apply")


class AutomaticSkillSyncTests(TwoMachines):
    def test_invalid_repository_policy_does_not_prevent_fetching_its_correction(self):
        good_kit = 'format = 2\n[defaults]\nskills = "apply"\n[[modules]]\nname = "work"\n'
        self.seed_files({
            "kit.toml": good_kit,
            "modules/work/module.toml": '[[skills]]\nname = "review"\ndir = "skills/review"\n',
            "modules/work/skills/review/SKILL.md": '---\nname: review\ndescription: Review code\n---\nInitial procedure.\n',
        })
        machine = self.machine("a", policy={"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"})
        machine.sync()
        before = machine.read(".agents", "skills", "review", "SKILL.md")
        self.seed_files({"kit.toml": good_kit.replace('"apply"', '"wrong"')}, "Invalid shared policy")
        _engine, report = machine.sync()
        self.assertTrue(report.errors)
        self.assertEqual(machine.read(".agents", "skills", "review", "SKILL.md"), before)
        self.seed_files({"kit.toml": good_kit, "modules/work/skills/review/SKILL.md": '---\nname: review\ndescription: Review code\n---\nRecovered procedure.\n'}, "Correct shared policy")
        with mock.patch("agent_kit_sync.engine.gitops.fetch", wraps=gitops.fetch) as fetch:
            engine, report = machine.sync()
        self.assertEqual(report.errors, [])
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("Recovered procedure", machine.read(".agents", "skills", "review", "SKILL.md"))
        self.assertNotIn("config", [alert["key"] for alert in engine.alerts.open()])

    def test_fetched_repository_policy_applies_in_the_same_sync_pass(self):
        self.seed_files({
            "kit.toml": 'format = 2\n[[modules]]\nname = "work"\n',
            "modules/work/module.toml": '[[skills]]\nname = "review"\ndir = "skills/review"\n',
            "modules/work/skills/review/SKILL.md": '---\nname: review\ndescription: Review code\n---\nShared procedure.\n',
        })
        machine = self.machine("a", policy={"new_items": "ask", "other_changes": "ask", "code_changes": "ask", "outgoing": "push"})
        engine, _report = machine.sync()
        self.assertTrue(any(h.get("item_key") == "work/review" for h in engine.held.all()))
        self.seed_files({"kit.toml": 'format = 2\n[defaults]\nskills = "apply"\n[[modules]]\nname = "work"\n'}, "Enable automatic skill sync")
        engine, report = machine.sync()
        self.assertEqual(report.errors, [])
        self.assertIn("Shared procedure", machine.read(".agents", "skills", "review", "SKILL.md"))
        self.assertFalse(any(h.get("item_key") == "work/review" for h in engine.held.all()))
        self.assertEqual(report.held, [])

    def test_new_and_updated_skills_install_without_individual_requests(self):
        self.seed_files({
            "kit.toml": 'format = 2\n[defaults]\nskills = "apply"\n[[modules]]\nname = "work"\n',
            "modules/work/module.toml": '[[skills]]\nname = "review"\ndir = "skills/review"\n[[hooks]]\nname = "guard"\nfile = "guard.py"\nevents = ["pre_tool_use"]\n',
            "modules/work/skills/review/SKILL.md": '---\nname: review\ndescription: Review code\n---\nInitial review procedure.\n',
            "modules/work/guard.py": 'pass\n',
        })
        machine = self.machine("a", policy={"new_items": "ask", "code_changes": "ask", "other_changes": "ask", "outgoing": "push"})
        engine, report = machine.sync()
        self.assertEqual(report.errors, [])
        self.assertIn("Initial review procedure", machine.read(".agents", "skills", "review", "SKILL.md"))
        self.assertFalse(any(h["item_key"] == "work/review" for h in engine.held.all()))
        self.assertTrue(any(h["item_key"] == "work/guard" for h in engine.held.all()))
        self.seed_files({"modules/work/skills/review/SKILL.md": '---\nname: review\ndescription: Review code\n---\nUpdated review procedure.\n'}, "Update shared skill")
        engine, report = machine.sync()
        self.assertEqual(report.errors, [])
        self.assertIn("Updated review procedure", machine.read(".claude", "skills", "review", "SKILL.md"))
        self.assertIn("Updated review procedure", machine.read(".agents", "skills", "review", "SKILL.md"))
        self.assertFalse(any(h["item_key"] == "work/review" for h in engine.held.all()))


if __name__ == "__main__":
    unittest.main()
