"""The agent-kit-sync command."""

from __future__ import annotations
from . import processes

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import VERSION, gitops, toml
from .paths import Layout, kit_dir


def layout_from_env() -> Layout:
    return Layout(kit_dir())


def say(text: str = "") -> None:
    sys.stdout.write(text + "\n")


# ---------------------------------------------------------------- sync


def cmd_sync(args, layout: Layout) -> int:
    from .background import retain_worker_lease
    from .engine import Engine, QUIET_SECONDS
    from .model import ConfigError
    from .notify import notify
    from .state import LockBusy, request_rerun, run_lock, take_rerun
    if args.background and not retain_worker_lease(layout.root):
        request_rerun(layout)
        return 0
    if args.quiet_wait:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and _recent_edit(layout, QUIET_SECONDS):
            time.sleep(2)
    try:
        with run_lock(layout, wait=0 if args.background else 30):
            for attempt in range(3):
                take_rerun(layout)
                code = _one_run(args, layout)
                if not take_rerun(layout):
                    return code
                if attempt == 2:
                    request_rerun(layout)  # Preserve work deferred beyond the bounded loop.
            return code
    except LockBusy:
        request_rerun(layout)
        if not args.background:
            say("Another agent-kit-sync run is in progress; it will run again when it finishes.")
        return 0
    except Exception as error:  # Every failure must reach the user, not only the log.
        import traceback
        from .state import Alerts
        detail = f"{type(error).__name__}: {error}"
        try:
            Alerts(layout).raise_("run", f"An agent-kit-sync sync failed: {detail[:300]}. The full error is in {layout.logs / 'sync-background.log'}.", "agent-kit-sync sync")
            from .notify import notify
            notify("agent-kit-sync", f"A sync failed: {detail[:150]}")
        except Exception:
            pass
        sys.stderr.write(traceback.format_exc())
        if not args.background:
            say(f"agent-kit-sync: {detail}")
        return 2


def _recent_edit(layout: Layout, seconds: float) -> bool:
    try:
        index = json.loads((layout.state / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    now = time.time()
    for entry in index.get("copies", {}).values():
        try:
            if now - os.stat(entry["path"]).st_mtime < seconds:
                return True
        except OSError:
            continue
    return False


def _one_run(args, layout: Layout) -> int:
    from .engine import Engine
    from .model import load_machine
    from .notify import notify
    from .register import register
    from .selfupdate import update
    from .state import Alerts
    machine = load_machine(layout)
    alerts = Alerts(layout)
    network = not args.offline
    if not machine.cloud and not getattr(args, "quiet_wait", False):
        outcome, detail = update(layout, machine.policy.get("tool_updates", "apply"), network)
        if outcome == "failed":
            alerts.raise_("tool-update", f"An agent-kit-sync update failed its self-test, so this machine stays on the version it has. {detail[-200:]}", "agent-kit-sync status")
        elif outcome == "held":
            alerts.raise_("tool-update", f"agent-kit-sync version {detail} is available. This machine's policy asks before updating.", "agent-kit-sync update --apply")
        elif outcome in ("updated", "current"):
            alerts.close("tool-update")
            _offline_count(layout, reset=True)
        if outcome == "offline" and network and _offline_count(layout) >= 20:
            alerts.raise_("tool-update", f"agent-kit-sync has not reached its own repository in 20 runs, so it is not updating. {detail[-200:]}", "agent-kit-sync doctor")
    before = {alert["id"] for alert in alerts.open()}
    engine = Engine(layout)
    engine.throttle_fetch = bool(getattr(args, "quiet_wait", False))
    report = engine.run(network=network)
    alerts.close("run")
    try:
        changed = register(layout, engine.machine, engine.index.hooks)
        if changed.get("codex"):
            alerts.raise_("codex-approval", "agent-kit-sync changed its hook entries in Codex. Open /hooks in Codex and approve the agent-kit-sync entries, or Codex skips them.", "agent-kit-sync doctor")
    except ValueError as error:
        alerts.raise_("register", f"agent-kit-sync could not register its hooks: {error}", "agent-kit-sync doctor")
    fresh = [alert for alert in alerts.open() if alert["id"] not in before]
    if fresh and not machine.cloud:
        notify("agent-kit-sync", fresh[0]["message"] if len(fresh) == 1 else f"{len(fresh)} problems need your attention. Run agent-kit-sync status.")
    if not args.background:
        _print_report(report)
        for alert in alerts.open():
            say(f"Alert: {alert['message']}")
    return 1 if report.errors else 0


def _offline_count(layout: Layout, reset: bool = False) -> int:
    path = layout.state / "tool-offline"
    try:
        count = 0 if reset else int(path.read_text() or "0") + 1
    except (OSError, ValueError):
        count = 0 if reset else 1
    try:
        layout.state.mkdir(parents=True, exist_ok=True)
        path.write_text(str(count))
    except OSError:
        pass
    return count


def _print_report(report) -> None:
    lines = []
    if report.edits:
        lines.append("Carried back: " + ", ".join(sorted(set(report.edits))))
    if report.deletions:
        lines.append("Deleted everywhere: " + ", ".join(report.deletions))
    if report.installed:
        lines.append(f"Wrote {len(report.installed)} installed copies or settings")
    if report.removed:
        lines.append(f"Removed {len(report.removed)} installed copies")
    if report.merged:
        lines.append("Merged changes from the config repo")
    if report.pushed:
        lines.append("Pushed to the config repo")
    if report.held:
        lines.append(f"Held {len(set(report.held))} changes; run agent-kit-sync held")
    for error in report.errors:
        lines.append("Error: " + error)
    say("\n".join(lines) if lines else "Everything is in sync.")


# ---------------------------------------------------------------- hook


def cmd_hook(args, layout: Layout, launcher: Path) -> int:
    from .dispatch import main as dispatch_main
    return dispatch_main(layout.root, launcher, args.app, args.event)


def cmd_run_hook(args, layout: Layout) -> int:
    """Run one synced hook directly, as a subagent definition's own hook does."""
    import subprocess
    try:
        index = json.loads((layout.state / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        index = {}
    module, _, name = args.hook.partition("/")
    hook = next((h for h in index.get("hooks", []) if h["module"] == module and h["name"] == name), None)
    if hook is None:
        sys.stderr.write(f"agent-kit-sync has no hook {args.hook} on this machine.\n")
        return 2
    runtime = {"python": [sys.executable], "node": ["node"], "bash": ["bash"]}.get(hook.get("runtime", "python"), [])
    data = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    try:
        from .dispatch import caller, hook_args
        try:
            payload = json.loads(data.decode("utf-8")) if data else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        # Claude Code runs the hook a subagent definition names, and Cursor runs Claude Code's.
        calling = os.environ.get("AGENT_KIT_SYNC_APP") or caller("claude", payload if isinstance(payload, dict) else {})
        result = processes.run(runtime + [hook["path"]] + hook_args(hook, calling) + list(args.rest), input=data, timeout=hook.get("timeout", 10))
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"agent-kit-sync: the {args.hook} hook timed out.\n")
        return 2 if hook.get("on_failure") == "closed" else 1
    if result.returncode not in (0, 2) and hook.get("on_failure") == "closed":
        return 2
    return result.returncode


# ---------------------------------------------------------------- status and held


def cmd_status(args, layout: Layout) -> int:
    from .engine import Engine
    from .state import Alerts, Held
    engine = Engine(layout)
    inventory, modules = engine.load_modules()
    from .plan import Planner, runtimes_available
    from .tree import Tree
    plan = Planner(layout, engine.machine, modules, lambda m, i: Tree(m.root)).build()
    say(f"agent-kit-sync {VERSION} on {engine.machine.name}; apps: {', '.join(engine.machine.apps) or 'none'}")
    head = gitops.head(layout.config) if (layout.config / ".git").exists() else None
    say(f"Config repo commit: {head[:12] if head else 'none'}")
    say(f"Installed copies: {len(engine.index.copies)}; sections: {len(engine.index.blocks)}; settings: {len(engine.index.settings)}; hooks: {len(engine.index.hooks)}")
    for name, reason in sorted(plan.skipped.items()):
        say(f"Skipped module {name}: {reason}")
    from .plan import INSTRUCTION_FILES
    for app, file_name in INSTRUCTION_FILES.items():
        if app in engine.machine.apps:
            path = engine.machine.app_homes[app] / file_name
            if path.exists():
                say(f"{path}: {path.stat().st_size:,} bytes")
    held = Held(layout).all()
    alerts = Alerts(layout).open()
    if held:
        say(f"Held changes: {len(held)}; run agent-kit-sync held")
    for alert in alerts:
        say(f"Alert: {alert['message']}")
    unsynced = _unsynced(engine)
    if unsynced:
        say("Not in the inventory:")
        for path in unsynced[:30]:
            say(f"  {path}")
        if len(unsynced) > 30:
            say(f"  ... and {len(unsynced) - 30} more")
    return 0


def _unsynced(engine) -> List[str]:
    from .fsutil import real
    known = {real(Path(entry["path"])) for entry in engine.index.copies.values()}
    found = []
    folders = []
    machine = engine.machine
    if "claude" in machine.apps:
        folders += [machine.app_homes["claude"] / "skills", machine.app_homes["claude"] / "agents"]
    if "codex" in machine.apps:
        folders += [Path.home() / ".agents" / "skills", machine.app_homes["codex"] / "skills", machine.app_homes["codex"] / "agents"]
    for folder in folders:
        if not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            if child.name.startswith("."):
                continue
            files = [child] if child.is_file() else [p for p in child.rglob("*") if p.is_file()]
            if not any(real(p) in known for p in files):
                found.append(str(child))
    return found


def cmd_held(args, layout: Layout) -> int:
    from .state import Held
    held = Held(layout)
    if args.action == "resolve":
        from .resolve import resolve
        return resolve(layout, args.id, args.choice)
    entries = held.all()
    if args.json:
        say(json.dumps(entries, indent=1))
        return 0
    if not entries:
        say("No held changes.")
        return 0
    for entry in entries:
        say(f"{entry['id']}  {entry['kind']}")
        say(f"  {entry['summary']}")
        say(f"  Choices: {', '.join(entry['choices'])}  (agent-kit-sync held resolve {entry['id']} CHOICE)")
    return 0


# ---------------------------------------------------------------- machine changes


def _machine_doc(layout: Layout) -> dict:
    return toml.load_file(layout.machine_file)


def _save_machine(layout: Layout, data: dict) -> None:
    from .fsutil import atomic_write
    text = toml.dumps(data)
    toml.loads(text)
    atomic_write(layout.machine_file, text.encode("utf-8"))


def cmd_disable(args, layout: Layout, enable: bool = False) -> int:
    data = _machine_doc(layout)
    overrides = data.setdefault("overrides", {})
    if enable:
        if overrides.get(args.target, {}).get("kind") == "disable":
            del overrides[args.target]
    else:
        overrides[args.target] = {"kind": "disable"}
    _save_machine(layout, data)
    say(("Enabled " if enable else "Disabled ") + args.target + " on this machine. The next sync applies it.")
    return 0


def cmd_override(args, layout: Layout) -> int:
    data = _machine_doc(layout)
    overrides = data.setdefault("overrides", {})
    if args.kind == "remove":
        overrides.pop(args.target, None)
    else:
        entry = {"kind": args.kind}
        if args.kind in ("replace", "prepend", "append"):
            if not args.file:
                say("Give the local file with --file.")
                return 2
            entry["file"] = str(Path(args.file).resolve())
        if args.kind == "pin":
            entry["commit"] = args.commit or gitops.head(layout.config)
        overrides[args.target] = entry
    _save_machine(layout, data)
    say(f"Override for {args.target} saved. The next sync applies it.")
    return 0


def cmd_which(args, layout: Layout) -> int:
    from .fsutil import real
    from .state import Index
    index = Index(layout)
    target = real(Path(args.path))
    for entry in index.copies.values():
        if real(Path(entry["path"])) == target:
            say(f"{entry['item_key']} ({entry['kind']}), from {entry['source_rel']}, for {entry['app']}")
            return 0
    say("agent-kit-sync did not install that file.")
    return 1


# ---------------------------------------------------------------- local test run


def cmd_test(args, layout: Layout) -> int:
    from .testing import run_module_tests
    return run_module_tests(layout, args.module)


# ---------------------------------------------------------------- entry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-kit-sync", description="Keep coding-agent skills, hooks, instructions, and settings the same on every machine.")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("sync", help="Run a sync now")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--quiet-wait", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--now", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--offline", action="store_true", help="Do not fetch or push")
    p = sub.add_parser("hook", help="Run registered hooks for an agent-app event (internal)")
    p.add_argument("app")
    p.add_argument("event")
    p = sub.add_parser("run-hook", help="Run one synced hook (internal)")
    p.add_argument("hook")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("status", help="Show the last run, alerts, held changes, and unsynced items")
    p = sub.add_parser("held", help="List held changes, or resolve one")
    p.add_argument("action", nargs="?", choices=["list", "resolve"], default="list")
    p.add_argument("id", nargs="?")
    p.add_argument("choice", nargs="?")
    p.add_argument("--json", action="store_true")
    for name in ("disable", "enable"):
        p = sub.add_parser(name, help=f"{name.capitalize()} a module or MODULE/ITEM on this machine")
        p.add_argument("target")
    p = sub.add_parser("override", help="Set or remove an override on this machine")
    p.add_argument("target")
    p.add_argument("kind", choices=["disable", "replace", "prepend", "append", "pin", "remove"])
    p.add_argument("--file")
    p.add_argument("--commit")
    p = sub.add_parser("which", help="Show which item an installed file came from")
    p.add_argument("path")
    p = sub.add_parser("test", help="Run the checks and module tests without pushing")
    p.add_argument("module", nargs="?")
    p = sub.add_parser("add", help="Add a local skill, subagent definition, hook, or instruction section to a module")
    p.add_argument("path")
    p.add_argument("--module", required=True)
    p.add_argument("--kind", choices=["skill", "agent", "hook", "section", "file"])
    p.add_argument("--name")
    p.add_argument("--local", action="store_true", help="Add to a local module that never syncs")
    p.add_argument("--heading", help="For an instruction file: the heading of the section to add")
    p.add_argument("--events", help="For a hook: comma-separated events")
    p.add_argument("--tests", help="For a hook: comma-separated test files, copied beside the hook; required unless --local")
    p = sub.add_parser("remember", help="Add a shared memory entry")
    p.add_argument("text")
    p = sub.add_parser("forget", help="Remove a shared memory entry")
    p.add_argument("id")
    sub.add_parser("doctor", help="Check this machine's setup")
    sub.add_parser("repair", help="Register the dispatcher again with this machine's Python")
    p = sub.add_parser("undo", help="Reverse this machine's most recent run that changed anything")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("uninstall", help="Remove agent-kit-sync's changes from this machine")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("update", help="Show or apply a held tool update")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("setup", help="First-time setup steps, run by the setup skill")
    p.add_argument("step", choices=["check", "init", "scan", "report", "apply", "register"])
    p.add_argument("--answers", help="For apply: a TOML file of the user's answers")
    p.add_argument("--config-url")
    p.add_argument("--tool-url")
    p.add_argument("--create", action="store_true")
    p = sub.add_parser("install", help="Install from an existing checkout without questions")
    p.add_argument("--cloud", action="store_true")
    p.add_argument("--config-url")
    return parser


def main(argv: Optional[List[str]] = None, launcher: Optional[Path] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    layout = layout_from_env()
    launcher = launcher or layout.launcher
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "hook":
        return cmd_hook(args, layout, launcher)
    if args.command == "run-hook":
        return cmd_run_hook(args, layout)
    if args.command == "sync":
        return cmd_sync(args, layout)
    if args.command == "status":
        return cmd_status(args, layout)
    if args.command == "held":
        return cmd_held(args, layout)
    if args.command in ("disable", "enable"):
        return cmd_disable(args, layout, enable=args.command == "enable")
    if args.command == "override":
        return cmd_override(args, layout)
    if args.command == "which":
        return cmd_which(args, layout)
    if args.command == "test":
        return cmd_test(args, layout)
    from . import commands
    return commands.dispatch(args, layout, launcher)
