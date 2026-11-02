"""Git commands with time limits and without prompts nobody could answer."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence

NETWORK_TIMEOUT = 60
LOCAL_TIMEOUT = 30


class GitError(Exception):
    def __init__(self, args: Sequence[str], code: int, output: str):
        self.code = code
        self.output = output
        super().__init__(f"git {' '.join(args)} failed ({code}): {output.strip()[:500]}")


def git_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "LC_ALL": "C",
    })
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    if extra:
        env.update(extra)
    return env


def run(repo: Optional[Path], *args: str, timeout: int = LOCAL_TIMEOUT, check: bool = True, input_bytes: Optional[bytes] = None, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if repo is not None:
        cmd += ["-C", str(repo)]
    cmd += ["-c", "core.autocrlf=false", "-c", "core.longpaths=true", "-c", "core.quotepath=false", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args]
    if args and args[0] in ("fetch", "push", "clone", "ls-remote", "pull") and "GIT_SSH_COMMAND" not in os.environ:
        configured = None
        if repo is not None:
            found = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.sshCommand"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            configured = found.stdout.decode().strip() or None
        env = dict(env or {})
        env["GIT_SSH_COMMAND"] = (configured or "ssh") + " -o BatchMode=yes"
    try:
        result = subprocess.run(cmd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if input_bytes is None else subprocess.PIPE, timeout=timeout, env=git_env(env), stdin=None if input_bytes is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise GitError(args, -1, f"timed out after {timeout} seconds") from None
    except FileNotFoundError:
        raise GitError(args, -2, "git is not installed") from None
    if check and result.returncode != 0:
        raise GitError(args, result.returncode, (result.stdout or b"").decode("utf-8", "replace") + (result.stderr or b"").decode("utf-8", "replace"))
    return result


def out(repo: Optional[Path], *args: str, timeout: int = LOCAL_TIMEOUT) -> str:
    return run(repo, *args, timeout=timeout).stdout.decode("utf-8", "replace").strip()


def head(repo: Path) -> Optional[str]:
    result = run(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def rev(repo: Path, ref: str) -> Optional[str]:
    result = run(repo, "rev-parse", "--verify", "-q", ref + "^{commit}", check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def show(repo: Path, commit: str, path: str) -> Optional[bytes]:
    result = run(repo, "show", f"{commit}:{path}", check=False)
    return result.stdout if result.returncode == 0 else None


def ls_tree(repo: Path, commit: str, path: str) -> List[tuple]:
    """Return (mode, relative path) for every file under `path` at `commit`."""
    result = run(repo, "ls-tree", "-r", "-z", "--full-tree", commit, "--", path.rstrip("/") + "/", check=False)
    if result.returncode != 0:
        return []
    entries = []
    prefix = path.rstrip("/") + "/"
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        mode = meta.split(b" ")[0].decode()
        rel = name.decode("utf-8")
        if rel.startswith(prefix):
            entries.append((mode, rel[len(prefix):]))
    return entries


def status_porcelain(repo: Path) -> List[str]:
    text = run(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.decode("utf-8", "replace")
    return [entry for entry in text.split("\0") if entry]


def commit_all(repo: Path, message: str, identity: Dict[str, str], paths: Optional[List[str]] = None) -> Optional[str]:
    """Stage `paths` (or everything) and commit. Returns the commit, or None when nothing changed."""
    if paths is None:
        run(repo, "add", "-A")
    else:
        if not paths:
            return None
        run(repo, "add", "-A", "--", *paths)
    staged = run(repo, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return None
    name = identity.get("name") or "agent-kit-sync"
    email = identity.get("email") or "agent-kit-sync@localhost"
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    run(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message, env=env)
    return head(repo)


def identity_env(identity: Dict[str, str]) -> Dict[str, str]:
    name = identity.get("name") or "agent-kit-sync"
    email = identity.get("email") or "agent-kit-sync@localhost"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def has_remote(repo: Path, name: str = "origin") -> bool:
    return run(repo, "remote", "get-url", name, check=False).returncode == 0


def fetch(repo: Path, remote: str = "origin") -> None:
    run(repo, "fetch", "--quiet", "--prune", remote, timeout=NETWORK_TIMEOUT)


def branch(repo: Path) -> str:
    """The checkout's current branch, or main when it has none yet."""
    result = run(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    name = result.stdout.decode().strip()
    return name or "main"


def upstream(repo: Path) -> str:
    """The remote-tracking ref for the current branch, such as origin/main."""
    name = branch(repo)
    result = run(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    found = result.stdout.decode().strip()
    return found if result.returncode == 0 and found else f"origin/{name}"


def push(repo: Path, branch: str = "main", remote: str = "origin") -> bool:
    result = run(repo, "push", "--quiet", remote, f"HEAD:refs/heads/{branch}", timeout=NETWORK_TIMEOUT, check=False)
    if result.returncode == 0:
        return True
    text = (result.stdout or b"").decode("utf-8", "replace")
    if "rejected" in text or "non-fast-forward" in text or "fetch first" in text:
        return False
    raise GitError(["push"], result.returncode, text)


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return run(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def unmerged(repo: Path) -> Dict[str, str]:
    """Paths left conflicted by a merge, with Git's two-letter status such as UU, AA, DU, or UD."""
    entries = {}
    for entry in status_porcelain(repo):
        code, path = entry[:2], entry[3:]
        if code in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
            entries[path] = code
    return entries


def merge_file(current: bytes, base: bytes, other: bytes) -> Optional[bytes]:
    """Three-way merge of one file's bytes. Returns None on conflict."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="aks-merge-") as tmp:
        paths = []
        for name, data in (("current", current), ("base", base), ("other", other)):
            path = Path(tmp) / name
            path.write_bytes(data)
            paths.append(str(path))
        result = run(None, "merge-file", "-p", "-q", *paths, check=False)
        if result.returncode != 0:
            return None
        return result.stdout
