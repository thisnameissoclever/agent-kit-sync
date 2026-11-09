"""Launch automated child processes without opening Windows console windows."""

from __future__ import annotations

import subprocess
import sys


def options(kwargs):
    result = dict(kwargs)
    if sys.platform.startswith("win"):
        flags = result.get("creationflags", 0)
        if flags & (0x00000008 | 0x00000010):
            raise ValueError("Automated processes cannot use DETACHED_PROCESS or CREATE_NEW_CONSOLE")
        result["creationflags"] = flags | 0x08000000  # CREATE_NO_WINDOW
    return result


def run(*args, **kwargs):
    return subprocess.run(*args, **options(kwargs))


def popen(*args, **kwargs):
    return subprocess.Popen(*args, **options(kwargs))
