"""Approving agent-kit-sync's own hook entries in Codex.

Codex runs a hook only after it is approved, and it keeps each approval as the hash of the
entry in `config.toml`. When agent-kit-sync changes an entry, for example its timeout, the old
hash no longer matches and Codex skips the entry until someone approves it again. This module
asks Codex's app server, `codex app-server`, for the current entries and their hashes, and
approves exactly the entries agent-kit-sync wrote: an entry qualifies only when it is in this
machine's Codex `hooks.json`, alone in its group, and its handler and matcher equal, field for
field, what agent-kit-sync writes there. Every other hook, including one edited to add commands around the dispatcher,
is left for the user to approve in Codex's `/hooks` screen. It then lists the entries again and
reports any that are still not approved.

The app server speaks JSON-RPC, one JSON message per line on standard input and output. Every
read has a deadline, and the process is stopped when the exchange ends or fails.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import processes
from .model import Machine
from .paths import Layout
from .register import desired_entries

TRUSTED = ("trusted", "managed")
DEADLINE_SECONDS = 30


class TrustResult:
    def __init__(self, approved: Optional[List[str]] = None, remaining: Optional[List[str]] = None, error: str = ""):
        self.approved = approved or []
        self.remaining = remaining or []
        self.error = error

    @property
    def ok(self) -> bool:
        return not self.error and not self.remaining


def _snake(event: str) -> str:
    import re
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def expected_entries(layout: Layout, machine: Machine, hooks: List[dict]) -> Dict[str, dict]:
    """What agent-kit-sync writes in Codex's hooks.json, keyed by Codex's snake_case event name."""
    result = {}
    for native, wanted in desired_entries(layout, machine, "codex", hooks, False).items():
        result[_snake(native)] = {"native": native, "command": wanted["handler"]["command"], "matcher": wanted["matcher"],
                                  "timeout": wanted["handler"].get("timeout"), "handler": wanted["handler"]}
    return result


def _on_disk(data: dict, key: str, expected: Dict[str, dict]) -> bool:
    """True when the group and handler a Codex key names hold exactly what agent-kit-sync writes, and nothing else.

    Codex's listing shows only some of an entry's fields, so the file itself is the check that no
    other field, such as `commandWindows`, was added or changed.
    """
    parts = key.rsplit(":", 3)
    wanted = expected.get(parts[1]) if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit() else None
    if not wanted:
        return False
    try:
        group = (data.get("hooks") or {}).get(wanted["native"])[int(parts[2])]
        handlers = group.get("hooks")
        if int(parts[3]) != 0 or not isinstance(handlers, list) or len(handlers) != 1:
            return False
    except (AttributeError, IndexError, TypeError, ValueError):
        return False
    return handlers[0] == wanted["handler"] and group.get("matcher") == wanted["matcher"] and set(group) <= {"matcher", "hooks"}


def _key_event(key: str) -> str:
    """Codex keys an entry `<file>:<event>:<group>:<handler>`; the file path may itself hold colons."""
    parts = key.rsplit(":", 3)
    return parts[1] if len(parts) == 4 else ""


def _same_file(left: str, right: Path) -> bool:
    try:
        return Path(left).resolve() == right.resolve()
    except (OSError, ValueError):
        return False


def ours_exactly(entry: dict, hooks_path: Path, expected: Dict[str, dict], data: Optional[dict] = None) -> bool:
    if not isinstance(entry, dict) or entry.get("isManaged") or entry.get("source") == "system":
        return False
    if data is not None and not _on_disk(data, str(entry.get("key") or ""), expected):
        return False
    if not _same_file(str(entry.get("sourcePath") or ""), hooks_path):
        return False
    wanted = expected.get(_key_event(str(entry.get("key") or "")))
    if not wanted or entry.get("command") != wanted["command"]:
        return False
    if wanted["timeout"] is not None and entry.get("timeoutSec") not in (None, wanted["timeout"]):
        return False
    return wanted["matcher"] is None or entry.get("matcher") == wanted["matcher"]


class _Server:
    def __init__(self, command: Sequence[str], cwd: Path, deadline: float, environment: Optional[Dict[str, str]] = None):
        self.deadline = deadline
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self.process = processes.popen(list(command), cwd=str(cwd), stdin=processes.subprocess.PIPE, stdout=processes.subprocess.PIPE,
                                       stderr=processes.subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, env=environment)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.next_id = 0

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method: str, params: dict) -> dict:
        self.next_id += 1
        ident = self.next_id
        self.send({"id": ident, "method": method, "params": params})
        while True:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"Codex's app server did not answer {method} within {DEADLINE_SECONDS} seconds")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError(f"Codex's app server exited before answering {method}")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == ident and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"Codex's app server refused {method}: {str(message['error'])[:200]}")
                result = message.get("result")
                return result if isinstance(result, dict) else {}

    def close(self) -> None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except processes.subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        # Close standard output only after the reader has reached its end. A child process that
        # outlived the server can hold the pipe open, and closing it under a blocked reader blocks.
        self.reader.join(timeout=1)
        if not self.reader.is_alive():
            try:
                self.process.stdout.close()
            except OSError:
                pass


def _entries(result: dict) -> List[dict]:
    return [entry for group in result.get("data") or [] if isinstance(group, dict) for entry in group.get("hooks") or [] if isinstance(entry, dict)]


def trust_own_hooks(layout: Layout, machine: Machine, hooks: List[dict], command: Optional[Sequence[str]] = None, write: bool = True) -> TrustResult:
    """Approve agent-kit-sync's own Codex entries. Returns what it approved and what is still unapproved.

    With `write` False it only checks: every unapproved entry of its own is reported in `remaining`.
    """
    hooks_path = machine.app_homes["codex"] / "hooks.json"
    expected = expected_entries(layout, machine, hooks)
    if command is None:
        program = shutil.which("codex")
        if not program:
            return TrustResult(error="the codex program is not on PATH")
        command = [program, "app-server"]
    try:
        # The app server must read and write the Codex home agent-kit-sync registered in, which can differ from the inherited one.
        environment = dict(os.environ, CODEX_HOME=str(machine.app_homes["codex"]))
        server = _Server(command, Path.home(), time.monotonic() + DEADLINE_SECONDS, environment)
    except OSError as error:
        return TrustResult(error=f"Codex's app server could not start: {error}")
    try:
        server.call("initialize", {"clientInfo": {"name": "agent-kit-sync", "version": "1"}})
        server.send({"method": "initialized"})
        # Read the file before and after the listing: when both reads agree, the hashes Codex
        # listed are of the content checked below.
        before = hooks_path.read_bytes() if hooks_path.exists() else b""
        entries = _entries(server.call("hooks/list", {}))
        raw = hooks_path.read_bytes() if hooks_path.exists() else b""
        if raw != before:
            return TrustResult(error=f"{hooks_path} changed while agent-kit-sync was approving its entries")
        try:
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except ValueError:
            return TrustResult(error=f"{hooks_path} is not valid JSON")
        mine = [entry for entry in entries if ours_exactly(entry, hooks_path, expected, data if isinstance(data, dict) else {})]
        if expected and not mine:
            return TrustResult(error=f"Codex did not list agent-kit-sync's entries from {hooks_path} as written")
        pending = [entry for entry in mine if entry.get("trustStatus") not in TRUSTED]
        if not pending:
            return TrustResult()
        if not write:
            return TrustResult(remaining=[entry["key"] for entry in pending], error="Codex lists them as not approved")
        edits = [{"keyPath": f"hooks.state.{json.dumps(entry['key'])}.trusted_hash", "value": entry["currentHash"], "mergeStrategy": "replace"}
                 for entry in pending if entry.get("currentHash")]
        if (hooks_path.read_bytes() if hooks_path.exists() else b"") != raw:
            return TrustResult(error=f"{hooks_path} changed while agent-kit-sync was approving its entries")
        if edits:
            server.call("config/batchWrite", {"edits": edits})
        after = {entry.get("key"): entry for entry in _entries(server.call("hooks/list", {}))}
        approved, remaining = [], []
        for entry in pending:
            now = after.get(entry["key"]) or {}
            (approved if now.get("trustStatus") in TRUSTED else remaining).append(entry["key"])
        return TrustResult(approved, remaining, "" if not remaining else "Codex still lists them as not approved after agent-kit-sync approved them")
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        return TrustResult(error=str(error))
    finally:
        server.close()


def settle(layout: Layout, machine: Machine, hooks: List[dict], alerts, codex_changed: bool, command: Optional[Sequence[str]] = None, force: bool = False) -> Optional[TrustResult]:
    """Run `_settle`, and turn any unexpected error into the alert, so approval can never break a sync."""
    try:
        return _settle(layout, machine, hooks, alerts, codex_changed, command, force)
    except Exception as error:  # noqa: BLE001  (an approval problem must never stop a sync)
        result = TrustResult(error=f"{type(error).__name__}: {error}")
        try:
            alerts.raise_("codex-approval", f"agent-kit-sync could not approve its own hook entries in Codex ({result.error}), so Codex may skip them. Run agent-kit-sync repair to try again, or open /hooks in Codex and approve the agent-kit-sync entries.", "agent-kit-sync repair")
        except Exception:  # noqa: BLE001
            pass
        return result


def _settle(layout: Layout, machine: Machine, hooks: List[dict], alerts, codex_changed: bool, command: Optional[Sequence[str]], force: bool) -> Optional[TrustResult]:
    """After registering, approve agent-kit-sync's own Codex entries, and raise or close the alert.

    Runs only when Codex is one of this machine's apps and agent-kit-sync just changed its entries,
    one of them has no approval at all, the approval alert is still open, or the caller forces it,
    as `repair` and setup do. The open alert matters because a stale approval, recorded for an
    earlier form of an entry, looks like an approval in config.toml. A machine whose policy says
    approvals must be typed in a terminal keeps the alert instead, so the user approves the entries
    in Codex.
    """
    if "codex" not in machine.apps:
        return None
    from .commands import _codex_unapproved
    hooks_path = machine.app_homes["codex"] / "hooks.json"
    alert_open = any(alert.get("key") == "codex-approval" for alert in alerts.open())
    if not (force or codex_changed or alert_open or _codex_unapproved(machine.app_homes["codex"], hooks_path, layout)):
        return None
    if machine.policy.get("approval") == "terminal":
        # Only check: close the alert once the user has approved the entries in Codex.
        check = trust_own_hooks(layout, machine, hooks, command, write=False)
        if check.ok:
            alerts.close("codex-approval")
        else:
            alerts.raise_("codex-approval", "Codex has agent-kit-sync hook entries that are not approved. This machine's policy requires you to approve them: open /hooks in Codex and approve the agent-kit-sync entries, or Codex skips them.", "agent-kit-sync doctor")
        return check
    result = trust_own_hooks(layout, machine, hooks, command)
    if result.ok:
        alerts.close("codex-approval")
    else:
        alerts.raise_("codex-approval", f"agent-kit-sync could not approve its own hook entries in Codex ({result.error}), so Codex skips them. Run agent-kit-sync repair to try again, or open /hooks in Codex and approve the agent-kit-sync entries.", "agent-kit-sync repair")
    return result
