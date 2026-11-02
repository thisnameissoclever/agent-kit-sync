"""Fake machines for tests: each has its own home folder and kit folder, sharing one bare repo."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_kit_sync import gitops, toml  # noqa: E402
from agent_kit_sync.engine import Engine  # noqa: E402
from agent_kit_sync.paths import Layout  # noqa: E402

HOME_VARS = ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "AGENT_KIT_SYNC_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
IDENTITY = {"name": "Test Person", "email": "test@example.com"}


def git(repo, *args):
    return gitops.run(Path(repo) if repo else None, *args, env=gitops.identity_env(IDENTITY)).stdout.decode()


@contextmanager
def as_home(home: Path):
    saved = {name: os.environ.get(name) for name in HOME_VARS}
    try:
        for name in HOME_VARS:
            os.environ.pop(name, None)
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class Machine:
    def __init__(self, base: Path, name: str, remote: Path, apps=("claude", "codex"), policy=None, tags=()):
        self.name = name
        self.home = base / name
        self.home.mkdir(parents=True)
        self.layout = Layout(self.home / ".agent-kit-sync")
        self.layout.root.mkdir()
        for app in apps:
            folder = {"claude": ".claude", "codex": ".codex", "cursor": ".cursor"}[app]
            (self.home / folder).mkdir()
        self.apps = list(apps)
        with self.env():
            git(None, "clone", "-q", str(remote), str(self.layout.config))
        machine = {
            "name": name,
            "apps": list(apps),
            "tags": list(tags),
            "python": sys.executable,
            "projects": str(self.home / "Projects"),
            "worktrees": str(self.home / "Projects" / "_worktrees"),
            "git_identity": IDENTITY,
            "policy": dict(policy or {"new_items": "apply", "code_changes": "apply", "other_changes": "apply", "outgoing": "push"}),
        }
        self.write_machine(machine)

    def write_machine(self, data: dict) -> None:
        self.layout.machine_file.write_text(toml.dumps(data), encoding="utf-8")

    def machine_data(self) -> dict:
        return toml.load_file(self.layout.machine_file)

    @contextmanager
    def env(self):
        with as_home(self.home):
            yield

    def sync(self, network=True):
        with self.env():
            engine = Engine(self.layout)
            return engine, engine.run(network=network)

    def path(self, *parts) -> Path:
        return self.home.joinpath(*parts)

    def read(self, *parts) -> str:
        return self.path(*parts).read_text(encoding="utf-8")

    def write(self, rel_path: Path, text: str) -> None:
        rel_path.parent.mkdir(parents=True, exist_ok=True)
        rel_path.write_text(text, encoding="utf-8")
        st = rel_path.stat()
        os.utime(rel_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


class TwoMachines(unittest.TestCase):
    """A bare repo and helpers to create machines and seed the config repo."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aks-test-"))
        self.remote = self.tmp / "remote.git"
        git(None, "init", "-q", "--bare", "-b", "main", str(self.remote))
        seed = self.tmp / "seed"
        git(None, "clone", "-q", str(self.remote), str(seed))
        self.seed = seed

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed_files(self, files: dict, message="seed"):
        for rel, text in files.items():
            path = self.seed / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", message)
        git(self.seed, "push", "-q", "origin", "HEAD:main")

    def machine(self, name, **kwargs) -> Machine:
        return Machine(self.tmp, name, self.remote, **kwargs)

    def standard_config(self):
        self.seed_files({
            "kit.toml": """
                format = 1

                [[modules]]
                name = "writing"
            """,
            ".gitattributes": "* text=auto eol=lf\n",
            "modules/writing/module.toml": """
                description = "Writing rules"

                [[sections]]
                name = "plain"
                file = "sections/plain.md"

                [[skills]]
                name = "review"
                dir = "skills/review"

                [[agents]]
                name = "helper"
                file = "agents/helper.md"

                [[settings]]
                app = "claude"
                key = "env.MAX_TOKENS"
                value = "10000"
            """,
            "modules/writing/sections/plain.md": """
                Write plainly.
                Keep projects in {{kit.projects}}.
            """,
            "modules/writing/skills/review/SKILL.md": """
                ---
                name: review
                description: Review prose
                ---
                Read the draft and report problems.
            """,
            "modules/writing/agents/helper.md": """
                ---
                name: helper
                description: Helps
                claude.tools: Read, Grep
                codex.sandbox_mode: read-only
                ---
                Help with the task.
            """,
        })
