"""Running module tests, each as its own process, with the user's folders pointed at a temporary one."""

from __future__ import annotations

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
    env.update({"HOME": str(home), "USERPROFILE": str(home), "AGENT_KIT_SYNC_TEST": "1"})
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
    """Return (result, detail), where result is passed, failed, or skipped."""
    path = module_root / rel
    cmd = runner(path)
    if cmd is None:
        return "skipped", f"no runtime for {path.suffix} on this machine"
    with tempfile.TemporaryDirectory(prefix="aks-test-home-") as home:
        try:
            result = subprocess.run(cmd, cwd=str(path.parent), env=isolated_env(Path(home)), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=TEST_SECONDS)
        except subprocess.TimeoutExpired:
            return "failed", f"took longer than {TEST_SECONDS} seconds"
    out = result.stdout.decode("utf-8", "replace")
    return ("passed" if result.returncode == 0 else "failed"), out[-1500:]


def run_module_tests(layout, only: Optional[str] = None) -> int:
    from .engine import Engine
    engine = Engine(layout)
    _inv, modules = engine.load_modules()
    from .state import Held
    held_items = {e.get("item_key") for e in Held(layout).all() if e["kind"] in ("new-item", "incoming")}
    failures = 0
    for module in modules:
        if only and module.name != only:
            continue
        if any(item.key in held_items for item in module.items):
            sys.stdout.write(f"skipped: {module.name} (held for approval)\n")
            continue
        for item in module.of("hook"):
            for rel in item.data.get("tests", []):
                result, detail = run_test(module.root, rel)
                sys.stdout.write(f"{result}: {module.name}/{rel}\n")
                if result == "failed":
                    failures += 1
                    sys.stdout.write("  " + detail.strip().replace("\n", "\n  ") + "\n")
    return 1 if failures else 0
