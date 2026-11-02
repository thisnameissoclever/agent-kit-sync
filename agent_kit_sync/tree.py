"""Reading module files from the checkout's working tree or from one of its commits."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple

from . import gitops
from .fsutil import is_executable, read_or_none
from .model import DEFAULT_IGNORE


def ignored(rel: str, patterns) -> bool:
    parts = PurePosixPath(rel).parts
    for pattern in list(DEFAULT_IGNORE) + list(patterns):
        if pattern.endswith("/"):
            if pattern.rstrip("/") in parts[:-1] or rel.startswith(pattern):
                return True
            continue
        if any(fnmatch.fnmatchcase(part, pattern) for part in parts):
            return True
        if fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


class Tree:
    """Files under one root, read from disk."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.commit: Optional[str] = None

    def read(self, rel: str) -> Optional[bytes]:
        return read_or_none(self.root / rel)

    def executable(self, rel: str) -> bool:
        return is_executable(self.root / rel)

    def files(self, rel_dir: str, ignore=()) -> List[Tuple[str, bool]]:
        """Return (path relative to rel_dir, executable) for each file, in sorted order."""
        base = self.root / rel_dir
        found = []
        if not base.is_dir():
            return found
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames.sort()
            here = Path(dirpath)
            for name in sorted(filenames):
                path = here / name
                rel = path.relative_to(base).as_posix()
                if ignored(rel, ignore):
                    continue
                if path.is_symlink():
                    target = Path(os.path.realpath(path))
                    if base.resolve() not in target.parents:
                        continue  # A link out of the skill folder is not installed.
                found.append((rel, is_executable(path)))
            dirnames[:] = [d for d in dirnames if not ignored((here / d).relative_to(base).as_posix() + "/x", ignore)]
        return found


class CommitTree(Tree):
    """Files under one root as they were at a commit of the checkout."""

    def __init__(self, repo: Path, commit: str, prefix: str):
        super().__init__(repo / prefix)
        self.repo = Path(repo)
        self.commit = commit
        self.prefix = prefix.rstrip("/")

    def _path(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def read(self, rel: str) -> Optional[bytes]:
        return gitops.show(self.repo, self.commit, self._path(rel))

    def executable(self, rel: str) -> bool:
        parent, _, name = rel.rpartition("/")
        for mode, path in gitops.ls_tree(self.repo, self.commit, self._path(parent) if parent else self.prefix):
            if path == name:
                return mode == "100755"
        return False

    def files(self, rel_dir: str, ignore=()) -> List[Tuple[str, bool]]:
        entries = gitops.ls_tree(self.repo, self.commit, self._path(rel_dir))
        return sorted((rel, mode == "100755") for mode, rel in entries if mode != "120000" and not ignored(rel, ignore))
