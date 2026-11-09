"""Reserve a background worker before launch, using an inherited operating-system lease."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import subprocess

from . import processes

LEASE_ENV = "AGENT_KIT_SYNC_WORKER_LEASE"
_retained = []


class Lease:
    def __init__(self, handle, windows):
        self.handle = handle
        self.windows = windows

    def close(self):
        if self.windows:
            import ctypes
            from ctypes import wintypes
            close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            if not close(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.close(self.handle)

    def inherit(self, kwargs):
        import subprocess
        env = dict(kwargs.get("env", os.environ))
        env[LEASE_ENV] = ("win:" if self.windows else "fd:") + str(self.handle)
        kwargs["env"] = env
        if self.windows:
            os.set_handle_inheritable(self.handle, True)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [self.handle]}
            kwargs["startupinfo"] = startup
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP, without a console.
        else:
            os.set_inheritable(self.handle, True)
            kwargs["pass_fds"] = (self.handle,)
            kwargs["start_new_session"] = True


def reserve(root):
    path = Path(root) / "state" / "background-worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes
        create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        handle = create(str(path), 0xC0000000, 0, None, 4, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            if error in (32, 33):
                return None
            raise ctypes.WinError(error)
        return Lease(handle, True)
    import fcntl  # agent-kit-sync: portable because this branch runs only on POSIX.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return Lease(descriptor, False)


def retain_worker_lease(root):
    """Keep ownership until process exit; never pass the lease to grandchildren."""
    inherited = os.environ.pop(LEASE_ENV, None)
    if inherited:
        kind, value = inherited.split(":", 1)
        lease = Lease(int(value), kind == "win")
        if lease.windows:
            os.set_handle_inheritable(lease.handle, False)
        else:
            os.set_inheritable(lease.handle, False)
    else:
        lease = reserve(root)
    if lease is None:
        return False
    _retained.append(lease)
    return True


def start(argv, root):
    from .paths import Layout
    from .state import request_rerun
    layout = Layout(Path(root))
    if os.environ.get("AGENT_KIT_SYNC_TEST") == "1" or os.environ.get("AGENT_KIT_SYNC_NO_BACKGROUND"):
        return False
    request_rerun(layout)
    lease = reserve(root)
    if lease is None:
        return False  # The active worker consumes pending work in its bounded sync loop.
    try:
        logs = Path(root) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "sync-background.log").open("ab") as log:
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True, "cwd": str(root)}
            lease.inherit(kwargs)
            processes.popen(argv, **kwargs)
        return True
    finally:
        lease.close()
