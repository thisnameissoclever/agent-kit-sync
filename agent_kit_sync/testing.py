"""Running module tests, each as its own process, with the user's folders pointed at a temporary one."""

from __future__ import annotations
from . import processes

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

TEST_SECONDS = 60
HOME_VARS = ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


def isolated_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in HOME_VARS and not k.startswith("AGENT_KIT_SYNC")}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "AGENT_KIT_SYNC_TEST": "1", "AGENT_KIT_SYNC_NO_BACKGROUND": "1"})
    return env


def runner(path: Path) -> Optional[List[str]]:
    suffix = path.suffix
    if suffix == ".py":
        return [sys.executable, str(path)]
    if suffix in (".mjs", ".js", ".cjs"):
        return ["node", str(path)] if shutil.which("node") else None
    if suffix == ".sh":
        return ["bash", str(path)] if shutil.which("bash") else None
    return None


def run_test(module_root: Path, rel: str) -> Tuple[str, str]:
    """Return (result, detail), where result is passed or failed.

    A test that cannot run here, because this machine lacks its runtime, fails: an unchecked
    hook must not count as a checked one.
    """
    path = module_root / rel
    if not path.is_file():
        return "failed", f"did not run: {rel} does not exist"
    cmd = runner(path)
    if cmd is None:
        if path.suffix in (".mjs", ".js", ".cjs"):
            return "failed", "did not run: this machine has no node"
        if path.suffix == ".sh":
            return "failed", "did not run: this machine has no bash"
        return "failed", f"did not run: agent-kit-sync runs tests written in Python (.py), JavaScript (.mjs, .js, .cjs), or bash (.sh), not {path.suffix or 'a file with no suffix'}"
    with tempfile.TemporaryDirectory(prefix="aks-test-home-") as home:
        try:
            result = processes.run(cmd, cwd=str(path.parent), env=isolated_env(Path(home)), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=TEST_SECONDS)
        except subprocess.TimeoutExpired:
            return "failed", f"took longer than {TEST_SECONDS} seconds"
    out = result.stdout.decode("utf-8", "replace")
    return ("passed" if result.returncode == 0 else "failed"), out[-1500:]


def missing_tests(module, item) -> Optional[str]:
    """Why a synced hook cannot be installed for want of tests, or None."""
    if item.kind != "hook" or module.local or item.data.get("tests"):
        return None
    return (f"{item.key} has no tests. Every synced hook needs tests, because each machine runs them before it installs the hook; "
            f"write them as the portable-scripts skill describes and list them under tests in the hook's module.toml entry")


def run_module_tests(layout, only: Optional[str] = None) -> int:
    from . import checks
    from .engine import Engine, item_source_files
    from .paths import os_name
    from .state import Held
    from .tree import Tree
    engine = Engine(layout)
    _inv, modules = engine.load_modules()
    held_items = {e.get("item_key") for e in Held(layout).all() if e["kind"] in ("new-item", "incoming")}
    failures = 0
    for module in modules:
        if only and module.name != only:
            continue
        if module.source == "builtin" and not only:
            continue
        if any(item.key in held_items for item in module.items):
            sys.stdout.write(f"held: {module.name} is waiting for approval, so its tests did not run; see agent-kit-sync held\n")
            continue
        systems = engine.module_systems(module)
        tree = Tree(module.root)
        for item in module.items:
            missing = missing_tests(module, item)
            if missing:
                failures += 1
                sys.stdout.write(f"failed: {missing}\n")
            if systems and item.kind != "setting":
                for rel in item_source_files(module, item, tree):
                    data = tree.read(rel)
                    if data is None or not checks.is_code(rel, data):
                        continue
                    marks: list = []
                    for finding in checks.portability_findings(f"{module.name}/{rel}", checks.decode_any(data), systems, marks=marks):
                        failures += 1
                        sys.stdout.write(f"failed: {finding}\n")
                    for mark in marks:
                        sys.stdout.write(f"accepted: {mark}\n")
        for item in module.of("hook"):
            for rel in item.data.get("tests", []):
                result, detail = run_test(module.root, rel)
                sys.stdout.write(f"{result}: {module.name}/{rel}\n")
                if result == "failed":
                    failures += 1
                    sys.stdout.write("  " + detail.strip().replace("\n", "\n  ") + "\n")
    label = checks.SYSTEM_LABELS.get(os_name(), os_name())
    sys.stdout.write(f"Tests ran on {label} only. Every other machine runs a hook's tests again before it installs a new or changed version, and keeps the hook off with an alert if they fail there.\n")
    return 1 if failures else 0
