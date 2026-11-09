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


class Stage:
    """The files one machine installs into its folder for one module, written to a temporary folder."""

    def __init__(self, skipped: Optional[str] = None, error: Optional[str] = None, disabled=()):
        self.skipped = skipped      # why this machine does not install the module, or None
        self.error = error          # why the module could not be planned, or None
        self.disabled = set(disabled)  # names of the module's items disabled on this machine


def stage_module(layout, machine, module, modules, stage: Path, tree_for) -> Stage:
    """Write into `stage` the files this machine installs into its folder for `module`, as it installs them.

    The planner decides which items install and renders their files, so a hook test sees what the hook
    sees in an agent app: an item disabled, held, or left out is absent, a templated or overridden file
    has this machine's bytes, and a file in the module folder that no item lists is never there.
    `modules` is every module of the sync, so placeholders naming another module's hook resolve.
    `tree_for(module, item)` gives the tree to read an item's files from.
    """
    from .fsutil import atomic_write
    from .plan import Planner, hook_commands
    planner = Planner(layout, machine, [module], tree_for)
    planner.hook_commands = hook_commands(layout, machine, modules)
    plan = planner.build()
    disabled = [item.name for item in module.items if planner.disabled(module.name, item.name)]
    if module.name in plan.failed:
        reason = plan.skipped.get(module.name, "")
        return Stage(error=reason[len("error: "):] if reason.startswith("error: ") else reason or "it could not be planned")
    if module.name in plan.skipped:
        return Stage(skipped=plan.skipped[module.name])
    base = layout.installed_module(module.name)
    stage.mkdir(parents=True, exist_ok=True)
    for copy in plan.copies:
        if copy.app != "kit" or copy.module != module.name:
            continue
        try:
            rel = copy.path.relative_to(base)
        except ValueError:
            continue
        atomic_write(stage / rel, copy.data, executable=copy.executable)
    return Stage(disabled=disabled)


def _stage_own_tests(stage: Path, module, item, tree_for) -> None:
    """Write a hook's own test files when the planner left them out, as it does for a missing runtime,
    so the test reports the real reason it cannot run."""
    from .fsutil import atomic_write
    from pathlib import PurePosixPath
    tree = tree_for(module, item)
    for rel in item.data.get("tests", []):
        target = stage / Path(*PurePosixPath(rel).parts)
        data = tree.read(rel)
        if data is not None and not target.exists():
            atomic_write(target, data)


SKIPPED = "skipped"
UNPLANNED = "unplanned"


def run_staged_tests(layout, machine, module, modules, item, tree_for, prefix="aks-staged-") -> Optional[str]:
    """Why `item`'s tests fail against the files this machine installs, None when they pass, SKIPPED, or UNPLANNED.

    SKIPPED means there is nothing to test here, because the module is not installed on this machine or
    the hook is disabled on it, so the hook never runs here. UNPLANNED means the planner cannot plan the
    module at all, so nothing in it installs and the install reports the planner's error itself.
    """
    missing = missing_tests(module, item)
    if missing:
        return missing
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        stage = Path(tmp) / module.name
        staged = stage_module(layout, machine, module, modules, stage, tree_for)
        if staged.error is not None:
            return UNPLANNED
        if staged.skipped is not None or item.name in staged.disabled:
            return SKIPPED
        _stage_own_tests(stage, module, item, tree_for)
        for rel in item.data.get("tests", []):
            result, detail = run_test(stage, rel)
            if result == "failed":
                return f"{rel}: {detail.strip().splitlines()[-1] if detail.strip() else 'failed'}"
    return None


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
        with tempfile.TemporaryDirectory(prefix="aks-test-stage-") as tmp:
            stage = Path(tmp) / module.name
            tree_for = lambda m, i: Tree(m.root)  # noqa: E731
            staged = stage_module(layout, engine.machine, module, modules, stage, tree_for)
            if staged.error is not None:
                failures += 1
                sys.stdout.write(f"failed: agent-kit-sync could not plan module {module.name} on this machine: {staged.error}\n")
                continue
            if staged.skipped is not None:
                sys.stdout.write(f"skipped: module {module.name} is not installed on this machine: {staged.skipped}\n")
                continue
            for item in module.of("hook"):
                if item.name in staged.disabled:
                    sys.stdout.write(f"skipped: {item.key} is disabled on this machine\n")
                    continue
                _stage_own_tests(stage, module, item, tree_for)
                for rel in item.data.get("tests", []):
                    result, detail = run_test(stage, rel)
                    sys.stdout.write(f"{result}: {module.name}/{rel}\n")
                    if result == "failed":
                        failures += 1
                        sys.stdout.write("  " + detail.strip().replace("\n", "\n  ") + "\n")
    label = checks.SYSTEM_LABELS.get(os_name(), os_name())
    sys.stdout.write(f"Tests ran on {label} only. Every other machine runs a hook's tests again before it installs a new or changed version, and keeps the hook off with an alert if they fail there.\n")
    return 1 if failures else 0
