"""Repository skill policy applies without changing unrelated approval boundaries."""

import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

from support import ROOT, TwoMachines, as_home
from agent_kit_sync import toml
from agent_kit_sync.engine import Engine
from agent_kit_sync.model import load_module
from agent_kit_sync.model import ConfigError
from agent_kit_sync.paths import Layout


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
            engine.alerts.raise_("incoming:work/review", "Old request", "agent-kit-sync held")
            saved = engine.held.add("held:work/review", "edit", "Saved local edit", ["retry"], {"edit": b"keep this"}, item_key="work/review")
            self.assertIn("work/review", self.effective(engine, module))
            self.assertIsNone(engine.held.find(held["id"]))
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
            with self.assertRaisesRegex(ConfigError, "defaults.skills"):
                self.make_engine(Path(folder), "wrong")


class AutomaticSkillSyncTests(TwoMachines):
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
