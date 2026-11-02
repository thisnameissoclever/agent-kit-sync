"""Locations of the kit folder and of each agent app's files on this machine."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

KIT_DIR_NAME = ".agent-kit-sync"
APPS = ("claude", "codex", "cursor", "antigravity")
# Apps whose items this version installs. Antigravity support comes later.
SUPPORTED_APPS = ("claude", "codex", "cursor")
PROGRAMS = {
    "claude": ("claude",),
    "codex": ("codex",),
    "cursor": ("cursor", "cursor-agent"),
    "antigravity": ("antigravity", "agy"),
}


def os_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def kit_dir() -> Path:
    override = os.environ.get("AGENT_KIT_SYNC_HOME")
    return Path(override) if override else Path.home() / KIT_DIR_NAME


def default_app_home(app: str) -> Path:
    home = Path.home()
    if app == "claude":
        value = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(value) if value else home / ".claude"
    if app == "codex":
        value = os.environ.get("CODEX_HOME")
        return Path(value) if value else home / ".codex"
    if app == "cursor":
        return home / ".cursor"
    if app == "antigravity":
        return home / ".gemini"
    raise ValueError(f"unknown app {app!r}")


def detect_apps() -> dict:
    """Return the agent apps present on this machine and their home folders."""
    found = {}
    for app in APPS:
        home = default_app_home(app)
        marker = home / "antigravity" if app == "antigravity" else home
        if marker.is_dir() or any(shutil.which(program) for program in PROGRAMS[app]):
            found[app] = home
    return found


class Layout:
    """Paths inside one kit folder."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def launcher(self) -> Path:
        return self.root / "launcher.py"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def tool(self) -> Path:
        return self.root / "tool"

    @property
    def tool_repo(self) -> Path:
        return self.root / "tool" / "repo.git"

    @property
    def current_file(self) -> Path:
        return self.root / "tool" / "current"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def machine_file(self) -> Path:
        return self.root / "machine.toml"

    @property
    def local(self) -> Path:
        return self.root / "local"

    @property
    def local_modules(self) -> Path:
        return self.root / "local" / "modules"

    @property
    def installed(self) -> Path:
        return self.root / "installed"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def blocklist(self) -> Path:
        return self.root / "blocklist.txt"

    @property
    def contributor_blocklist(self) -> Path:
        return self.root / "contributor-blocklist.txt"

    def installed_module(self, module: str) -> Path:
        return self.installed / module
