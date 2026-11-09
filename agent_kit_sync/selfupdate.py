"""Installing tool versions side by side and switching between them."""

from __future__ import annotations
from . import processes

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

from . import gitops
from .fsutil import atomic_write
from .paths import Layout

KEEP = 3
SELF_TEST_SECONDS = 20


def current_name(layout: Layout) -> Optional[str]:
    try:
        return layout.current_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def version_dir(layout: Layout, commit: str) -> Path:
    return layout.tool / commit[:12]


def install_version(layout: Layout, commit: str) -> Path:
    """Copy the files of one commit of the tool repo into its own folder."""
    target = version_dir(layout, commit)
    if (target / "agent_kit_sync" / "__init__.py").is_file():
        return target
    staging = layout.tool / (".staging-" + commit[:12])
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    archive = gitops.run(layout.tool_repo, "archive", "--format=tar", commit).stdout
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.issym() or member.islnk() or member.name.startswith(("/", "..")) or "/../" in member.name:
                continue
            tar.extract(member, staging)
    if target.exists():
        shutil.rmtree(target)
    os.replace(staging, target)
    return target


def self_test(layout: Layout, version: Path) -> Tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="aks-selftest-") as home:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "AGENT_KIT_SYNC"))}
        env.update({"HOME": home, "USERPROFILE": home, "AGENT_KIT_SYNC_HOME": str(Path(home) / ".agent-kit-sync"), "AGENT_KIT_SYNC_TEST": "1", "AGENT_KIT_SYNC_NO_BACKGROUND": "1"})
        try:
            code = "import sys; sys.path.insert(0, sys.argv[1]); from agent_kit_sync.selftest import main; sys.exit(main())"
            result = processes.run([sys.executable, "-I", "-S", "-c", code, str(version)], cwd=home, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=SELF_TEST_SECONDS)
        except subprocess.TimeoutExpired:
            return False, "the self-test took longer than 20 seconds"
        return result.returncode == 0, result.stdout.decode("utf-8", "replace")[-800:]


def switch(layout: Layout, version: Path) -> None:
    atomic_write(layout.current_file, (version.name + "\n").encode("utf-8"))
    os.utime(version, None)


def prune(layout: Layout) -> None:
    current = current_name(layout)
    versions = sorted((p for p in layout.tool.iterdir() if p.is_dir() and (p / "agent_kit_sync").is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in versions[KEEP:]:
        if old.name != current and time.time() - old.stat().st_mtime > 3600:
            shutil.rmtree(old, ignore_errors=True)


def origin(layout: Layout) -> Optional[str]:
    result = gitops.run(layout.tool_repo, "remote", "get-url", "origin", check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def update(layout: Layout, policy: str, network: bool = True) -> Tuple[str, str]:
    """Return (outcome, detail). Outcomes: current, updated, held, failed, pinned, offline."""
    if policy == "pin":
        return "pinned", ""
    if not layout.tool_repo.exists():
        return "offline", "no tool repo clone"
    if network:
        try:
            gitops.run(layout.tool_repo, "fetch", "--quiet", "origin", "+refs/heads/main:refs/heads/main", timeout=gitops.NETWORK_TIMEOUT)
        except gitops.GitError as error:
            return "offline", str(error)
    commit = gitops.rev(layout.tool_repo, "main")
    if commit is None:
        return "failed", "the tool repo has no main branch"
    if current_name(layout) == commit[:12]:
        return "current", commit[:12]
    if policy == "ask":
        return "held", commit[:12]
    version = install_version(layout, commit)
    ok, detail = self_test(layout, version)
    if not ok:
        return "failed", detail
    switch(layout, version)
    prune(layout)
    return "updated", commit[:12]
