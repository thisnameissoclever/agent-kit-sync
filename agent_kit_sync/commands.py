"""Commands other than sync: setup, install, add, shared memory, doctor, repair, undo, uninstall, and update."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

from . import VERSION, gitops, sections, toml, tomledit
from .fsutil import atomic_write, read_or_none, real, sha256
from .paths import Layout, detect_apps, os_name

SHARED_MEMORY_MODULE = "shared-memory"
SHARED_MEMORY_SECTION = "entries"


def say(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def dispatch(args, layout: Layout, launcher: Path) -> int:
    handler = {
        "add": cmd_add, "remember": cmd_remember, "forget": cmd_forget, "doctor": cmd_doctor, "repair": cmd_repair,
        "undo": cmd_undo, "uninstall": cmd_uninstall, "update": cmd_update, "setup": cmd_setup, "install": cmd_install,
    }[args.command]
    return handler(args, layout)


# ---------------------------------------------------------------- kit folder


def write_launcher(layout: Layout) -> None:
    template = Path(__file__).with_name("launcher_template.py").read_bytes()
    atomic_write(layout.launcher, template)
    python = sys.executable
    if os_name() == "windows":
        script = (f'@echo off\r\nif exist "{python}" (\r\n  "{python}" -I -S "{layout.launcher}" %*\r\n) else (\r\n'
                  f'  py -3 -I -S "{layout.launcher}" %*\r\n)\r\n')
        atomic_write(layout.bin / "agent-kit-sync.cmd", script.encode("utf-8"))
    else:
        script = (f'#!/bin/sh\nPY="{python}"\n[ -x "$PY" ] || PY=$(command -v python3 || command -v python)\n'
                  f'exec "$PY" -I -S "{layout.launcher}" "$@"\n')
        atomic_write(layout.bin / "agent-kit-sync", script.encode("utf-8"), executable=True)


def ensure_tool(layout: Layout, tool_url: Optional[str]) -> str:
    """Make sure a tool version is installed and current. Returns its folder name."""
    from .selfupdate import current_name, install_version, switch
    layout.tool.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parents[1]
    source = tool_url or str(here)
    if not layout.tool_repo.exists():
        gitops.run(None, "clone", "-q", "--bare", source, str(layout.tool_repo), timeout=gitops.NETWORK_TIMEOUT)
        # Follow the public repository the clone came from, not the clone itself, which may be deleted.
        upstream = tool_url
        if upstream is None and (here / ".git").exists():
            found = gitops.run(here, "remote", "get-url", "origin", check=False)
            upstream = found.stdout.decode().strip() if found.returncode == 0 else None
        if upstream:
            gitops.run(layout.tool_repo, "remote", "set-url", "origin", upstream)
    commit = gitops.rev(layout.tool_repo, "main") or gitops.rev(layout.tool_repo, "HEAD")
    if commit is None:
        raise RuntimeError(f"the tool repo at {source} has no commits")
    if current_name(layout) != commit[:12]:
        version = install_version(layout, commit)
        switch(layout, version)
    return commit[:12]


# ---------------------------------------------------------------- setup


def cmd_setup(args, layout: Layout) -> int:
    from . import setup as setup_steps
    return setup_steps.run(args, layout)


def cmd_install(args, layout: Layout) -> int:
    """Install without questions, for cloud sessions and machines whose machine file already exists."""
    from .cli import cmd_sync
    from .fsutil import private_dir
    layout.root.mkdir(parents=True, exist_ok=True)
    private_dir(layout.root)
    ensure_tool(layout, None)
    write_launcher(layout)
    if not layout.config.exists():
        if not args.config_url:
            say("Give the config repo with --config-url.")
            return 2
        gitops.run(None, "clone", "-q", args.config_url, str(layout.config), timeout=gitops.NETWORK_TIMEOUT)
    if not layout.machine_file.exists():
        apps = detect_apps()
        data = {
            "name": "cloud" if args.cloud else (os.environ.get("HOSTNAME") or "machine"),
            "apps": sorted(apps), "tags": ["cloud"] if args.cloud else [], "python": sys.executable, "cloud": bool(args.cloud),
            "policy": {"new_items": "apply", "code_changes": "apply", "other_changes": "apply", "outgoing": "push", "tool_updates": "apply"},
        }
        atomic_write(layout.machine_file, toml.dumps(data).encode("utf-8"))
    from types import SimpleNamespace
    return cmd_sync(SimpleNamespace(background=False, quiet_wait=False, now=True, offline=False), layout)


# ---------------------------------------------------------------- add


def _module_root(layout: Layout, module: str, local: bool) -> Path:
    return (layout.local_modules if local else layout.config / "modules") / module


def _ensure_module(layout: Layout, module: str, local: bool) -> Path:
    from .model import check_name
    check_name(module, "module name")
    root = _module_root(layout, module, local)
    manifest = root / "module.toml"
    if not manifest.exists():
        atomic_write(manifest, f'description = ""\n'.encode("utf-8"))
        if not local:
            kit = layout.config / "kit.toml"
            text = kit.read_text(encoding="utf-8") if kit.exists() else "format = 1\n"
            if not re.search(r'^name\s*=\s*"' + re.escape(module) + '"', text, re.M):
                text = tomledit.append_entry(text, "modules", {"name": module})
                atomic_write(kit, text.encode("utf-8"))
    return root


def guess_kind(path: Path) -> str:
    if path.is_dir() and (path / "SKILL.md").is_file():
        return "skill"
    if path.suffix == ".toml" and path.parent.name == "agents":
        return "agent"
    if path.suffix == ".md" and path.parent.name == "agents":
        return "agent"
    if path.name in ("CLAUDE.md", "AGENTS.md"):
        return "section"
    if path.suffix in (".py", ".sh", ".mjs", ".js", ".ps1"):
        return "hook"
    return "file"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "item"


def cmd_add(args, layout: Layout) -> int:
    source = Path(args.path).expanduser().resolve()
    if not source.exists():
        say(f"{source} does not exist.")
        return 2
    kind = args.kind or guess_kind(source)
    root = _ensure_module(layout, args.module, args.local)
    manifest = root / "module.toml"
    text = manifest.read_text(encoding="utf-8")
    if kind == "skill":
        name = args.name or slug(source.name)
        dest = root / "skills" / name
        if dest.exists():
            say(f"{dest} already exists.")
            return 2
        from .tree import Tree
        for rel, executable in Tree(source).files(""):
            atomic_write(dest / Path(*PurePosixPath(rel).parts), (source / rel).read_bytes(), executable=executable)
        text = tomledit.append_entry(text, "skills", {"name": name, "dir": f"skills/{name}"})
    elif kind == "agent":
        name = args.name or slug(source.stem)
        data = source.read_bytes()
        if source.suffix == ".toml":
            data = _agent_from_codex(source).encode("utf-8")
        atomic_write(root / "agents" / f"{name}.md", data)
        text = tomledit.append_entry(text, "agents", {"name": name, "file": f"agents/{name}.md"})
    elif kind == "hook":
        name = args.name or slug(source.stem)
        rel = f"hooks/{source.name}"
        tests = [Path(t).expanduser().resolve() for t in (args.tests or "").split(",") if t.strip()]
        if not tests and not args.local:
            say("Every synced hook needs tests, because each machine runs them before it installs the hook. "
                "Write them as the portable-scripts skill describes, then add the hook again with --tests TEST[,TEST], or add --local to keep it on this machine only.")
            return 2
        for test in tests:
            if not test.is_file():
                say(f"{test} does not exist.")
                return 2
        atomic_write(root / rel, source.read_bytes(), executable=True)
        test_rels = []
        for test in tests:
            # Keep each test where it sits relative to the hook, so a path such as ../hook.py still works.
            try:
                inner = PurePosixPath(test.relative_to(source.parent).as_posix())
            except ValueError:
                inner = PurePosixPath("tests") / test.name
            test_rel = f"hooks/{inner}"
            atomic_write(root / Path(*PurePosixPath(test_rel).parts), test.read_bytes())
            test_rels.append(test_rel)
        events = [e.strip() for e in (args.events or "pre_tool_use").split(",") if e.strip()]
        runtime = "node" if source.suffix in (".mjs", ".js", ".cjs") else ("bash" if source.suffix == ".sh" else "python")
        entry = {"name": name, "file": rel, "events": events, "protocol": "native", "runtime": runtime, "timeout": 10, "on_failure": "open"}
        if test_rels:
            entry["tests"] = test_rels
        text = tomledit.append_entry(text, "hooks", entry)
    elif kind == "section":
        if not args.heading:
            say("Give the heading of the section to add with --heading.")
            return 2
        body, start, stop = _section_text(source, args.heading)
        if body is None:
            say(f"No heading {args.heading!r} in {source}.")
            return 2
        name = args.name or slug(args.heading)
        atomic_write(root / "sections" / f"{name}.md", body.encode("utf-8"))
        text = tomledit.append_entry(text, "sections", {"name": name, "file": f"sections/{name}.md"})
        # Mark the original text in place, so the tool manages it from now on.
        original = source.read_bytes().decode("utf-8")
        backup = layout.state / "backups" / ("adopted-" + time.strftime("%Y%m%d-%H%M%S") + "-" + source.name)
        atomic_write(backup, original.encode("utf-8"))
        atomic_write(source, sections.adopt(original, start, stop, f"{args.module}/{name}").encode("utf-8"))
        from .state import Index, run_lock
        with run_lock(layout, wait=60):
            index = Index(layout)
            adopted = index.items.setdefault(f"{args.module}/{name}", {})
            adopted.setdefault("adopted", []).append(str(real(source)))
            index.save()
    else:
        name = args.name or slug(source.name)
        rel = f"files/{source.name}"
        atomic_write(root / rel, source.read_bytes())
        text = tomledit.append_entry(text, "files", {"name": name, "path": rel})
    toml.loads(text)
    atomic_write(manifest, text.encode("utf-8"))
    from .model import load_module, ConfigError
    try:
        load_module(root, args.module, args.local)
    except ConfigError as error:
        say(f"The module no longer loads: {error}")
        return 1
    say(f"Added {kind} {name} to {'local ' if args.local else ''}module {args.module}. Run agent-kit-sync sync to install it everywhere.")
    if kind == "hook" and not args.local:
        say("Run agent-kit-sync test MODULE before the next sync; it runs the hook's tests and the check for code that fails on macOS, Windows, or Linux.")
    return 0


def _section_text(path: Path, heading: str):
    lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
    target = heading.strip().lstrip("#").strip()
    fence = None
    for number, line in enumerate(lines):
        marker = re.match(r"^\s*(```+|~~~+)", line)
        if marker:
            fence = None if fence and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) else (fence or marker.group(1))
            continue
        if fence:
            continue
        match = re.match(r"^(#+)\s+(.*?)\s*$", line)
        if match and match.group(2) == target:
            level = len(match.group(1))
            stop = len(lines)
            inner = None
            for later in range(number + 1, len(lines)):
                marker = re.match(r"^\s*(```+|~~~+)", lines[later])
                if marker:
                    inner = None if inner and marker.group(1)[0] == inner[0] and len(marker.group(1)) >= len(inner) else (inner or marker.group(1))
                    continue
                if inner:
                    continue
                other = re.match(r"^(#+)\s", lines[later])
                if other and len(other.group(1)) <= level:
                    stop = later
                    break
            while stop > number + 1 and lines[stop - 1].strip() == "":
                stop -= 1
            return "".join(lines[number:stop]), number, stop
    return None, 0, 0


def _agent_from_codex(path: Path) -> str:
    data = toml.load_file(path)
    from .render import yaml_quote
    header = ["---\n"]
    for key, value in data.items():
        if key == "developer_instructions":
            continue
        name = key if key in ("name", "description") else "codex." + key
        if isinstance(value, (str, int, float, bool)):
            header.append(f"{name}: {yaml_quote(value)}\n")
        else:
            raise SystemExit(f"{path} has the field {key}, which holds a list or table; agent-kit-sync can sync only single values in subagent fields. Move it out of the file before adding it.")
    header.append("---\n")
    body = str(data.get("developer_instructions", ""))
    return "".join(header) + (body if body.endswith("\n") else body + "\n")


# ---------------------------------------------------------------- shared memory


def _memory_root(layout: Layout) -> Path:
    return layout.config / "modules" / SHARED_MEMORY_MODULE


def _ensure_memory_module(layout: Layout) -> Path:
    root = _ensure_module(layout, SHARED_MEMORY_MODULE, False)
    manifest = root / "module.toml"
    text = manifest.read_text(encoding="utf-8")
    if "[[sections]]" not in text:
        text = 'description = "Rules agents learned that apply on every project and machine"\n'
        text = tomledit.append_entry(text, "sections", {"name": SHARED_MEMORY_SECTION, "file": "sections/entries.md"})
        atomic_write(manifest, text.encode("utf-8"))
    return root


def rebuild_memory(layout: Layout) -> None:
    root = _ensure_memory_module(layout)
    entries = sorted((root / "entries").glob("*.md")) if (root / "entries").is_dir() else []
    parts = ["## Shared memory\n\nRules learned on any machine that apply everywhere. Add one with `{{kit.cli}} remember \"TEXT\"`.\n"]
    for path in entries:
        text = path.read_text(encoding="utf-8").strip()
        parts.append(f"\n- {text} ({path.stem})\n")
    atomic_write(root / "sections" / "entries.md", "".join(parts).encode("utf-8"))


def cmd_remember(args, layout: Layout) -> int:
    from .checks import Checker, read_terms
    text = " ".join(args.text.split())
    if not text:
        say("Give the rule to remember.")
        return 2
    problems = Checker(read_terms(layout.blocklist)).file("shared memory entry", text.encode("utf-8"), own=True)
    if problems:
        say("Not saved: " + "; ".join(str(p) for p in problems))
        return 1
    root = _ensure_memory_module(layout)
    entry_id = time.strftime("%Y%m%d") + "-" + secrets.token_hex(3)
    atomic_write(root / "entries" / f"{entry_id}.md", (text + "\n").encode("utf-8"))
    rebuild_memory(layout)
    say(f"Saved shared memory entry {entry_id}. It reaches every app and machine at the next sync.")
    return 0


def cmd_forget(args, layout: Layout) -> int:
    path = _memory_root(layout) / "entries" / f"{args.id}.md"
    if not path.exists():
        say(f"No shared memory entry {args.id}.")
        return 2
    path.unlink()
    rebuild_memory(layout)
    say(f"Removed shared memory entry {args.id}.")
    return 0


# ---------------------------------------------------------------- doctor and repair


def cmd_doctor(args, layout: Layout) -> int:
    from .model import load_machine
    from .register import claude_version
    problems = []
    notes = []
    machine = load_machine(layout)
    if shutil.which("git") is None:
        problems.append("Git is not on the command path.")
    for folder in machine.defaulted:
        path = getattr(machine, folder)
        where = "" if path.is_dir() else ", which does not exist on this machine"
        notes.append(f"The machine file sets no {folder} folder, so {path} is used{where}. Set {folder} in {layout.machine_file} to change it.")
    from .model import folder_name, kit_defaults
    for key, value in kit_defaults(layout).items():
        if key in ("projects_folder_name", "worktrees_folder_name") and folder_name(value) is None:
            problems.append(f"kit.toml sets defaults.{key} to {value!r}, which is not a plain folder name on this system, so it is ignored.")
    if sys.version_info < (3, 9):
        problems.append(f"Python {sys.version.split()[0]} is older than 3.9.")
    if machine.python and not Path(machine.python).exists():
        problems.append(f"The Python registered for hooks, {machine.python}, is gone. Run agent-kit-sync repair from a terminal.")
    if not layout.launcher.exists():
        problems.append(f"{layout.launcher} is missing. Run agent-kit-sync repair.")
    for app in machine.apps:
        home = machine.app_homes[app]
        if not home.is_dir():
            problems.append(f"{app}'s folder {home} is missing.")
    if "claude" in machine.apps:
        path = machine.app_homes["claude"] / "settings.json"
        data = _json(path)
        mine = _registered(data, layout)
        notes.append(f"Claude Code: {len(mine)} agent-kit-sync hook entries in {path}.")
        broken = _interpreter_only(data, machine.python or sys.executable)
        if broken:
            problems.append(f"{path} has {len(broken)} hook entries that run only {machine.python or sys.executable}, with no script ({', '.join(broken)}). "
                            "Each fails on every event. Another program probably rewrote the file and dropped the arguments. Delete those entries, then run agent-kit-sync repair.")
        if not mine:
            problems.append("No agent-kit-sync hook entries in Claude Code's settings. Run agent-kit-sync repair.")
        unshelled = _powershell_without_shell(data, layout)
        if unshelled:
            problems.append(f"{path} has {len(unshelled)} agent-kit-sync hook entries written for PowerShell that no longer set \"shell\" to \"powershell\" ({', '.join(unshelled)}). "
                            "Claude Code runs them in Git Bash, where they fail. Another program probably rewrote the file. Run agent-kit-sync repair.")
        version = claude_version()
        if version and version < (2, 1, 295):
            notes.append("This Claude Code is older than 2.1.295, so hooks set to block on failure fail open if the dispatcher cannot start.")
    if "codex" in machine.apps:
        path = machine.app_homes["codex"] / "hooks.json"
        mine = _registered(_json(path), layout)
        notes.append(f"Codex: {len(mine)} agent-kit-sync hook entries in {path}.")
        override = machine.app_homes["codex"] / "AGENTS.override.md"
        if override.is_file() and override.read_text(encoding="utf-8").strip():
            problems.append(f"{override} exists, so Codex ignores AGENTS.md and none of the synced sections.")
        unapproved = _codex_unapproved(machine.app_homes["codex"], path, layout)
        if unapproved:
            problems.append(f"Codex has not approved {len(unapproved)} agent-kit-sync hook entries ({', '.join(unapproved)}). Open /hooks in Codex and approve them.")
    if (Path.home() / ".cursor").is_dir():
        notes.append("Cursor is installed. It runs the hooks registered for Claude Code; if the dispatcher cannot start there, Cursor lets every action continue.")
        if "claude" in machine.apps and _exec_form(_json(machine.app_homes["claude"] / "settings.json"), layout):
            from .register import CLAUDE_MIN_POWERSHELL
            problems.append("Cursor cannot run this machine's agent-kit-sync entries in Claude Code's settings, because it ignores their \"args\" and starts Python with no script. "
                            f"Update Claude Code to {'.'.join(map(str, CLAUDE_MIN_POWERSHELL))} or later, then run agent-kit-sync repair.")
    for event in ("session_start", "pre_tool_use", "stop"):
        code, out = _dry_dispatch(layout, "claude" if "claude" in machine.apps else "codex", event)
        if code not in (0,):
            problems.append(f"The dispatcher exited with {code} on a sample {event} event: {out[:200]}")
    for line in notes:
        say(line)
    for line in problems:
        say("Problem: " + line)
    if not problems:
        say("No problems found.")
    return 1 if problems else 0


def _snake(event: str) -> str:
    import re as _re
    return _re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def _codex_unapproved(codex_home: Path, hooks_path: Path, layout: Layout) -> List[str]:
    """Codex keys each approval by hooks file, event, group position, and handler position."""
    from .register import _entry_command, ours
    data = _json(hooks_path)
    try:
        config = toml.load_file(codex_home / "config.toml")
    except (OSError, toml.TOMLDecodeError):
        config = {}
    state = ((config.get("hooks") or {}).get("state") or {})
    missing = []
    for event, groups in (data.get("hooks") or {}).items():
        for g, group in enumerate(groups if isinstance(groups, list) else []):
            for h, handler in enumerate(group.get("hooks", []) if isinstance(group, dict) else []):
                if isinstance(handler, dict) and ours(_entry_command(handler), layout.launcher):
                    key = f"{hooks_path}:{_snake(event)}:{g}:{h}"
                    if not (state.get(key) or {}).get("trusted_hash"):
                        missing.append(event)
    return missing


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _interpreter_only(data: dict, python: str) -> List[str]:
    from .register import interpreter_only
    found = []
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for handler in group.get("hooks", []) if isinstance(group, dict) else []:
                if interpreter_only(handler, python):
                    found.append(event)
    return found


def _exec_form(data: dict, layout: Layout) -> List[str]:
    """Events whose agent-kit-sync entry keeps its arguments in "args", which Cursor drops."""
    from .register import _entry_command, ours
    found = []
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for handler in group.get("hooks", []) if isinstance(group, dict) else []:
                if isinstance(handler, dict) and handler.get("args") and ours(_entry_command(handler), layout.launcher):
                    found.append(event)
    return found


def _powershell_without_shell(data: dict, layout: Layout) -> List[str]:
    """Events whose agent-kit-sync entry is a PowerShell call but has lost its "shell" field."""
    from .register import ours
    found = []
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for handler in group.get("hooks", []) if isinstance(group, dict) else []:
                if (isinstance(handler, dict) and str(handler.get("command", "")).startswith("& ") and handler.get("shell") != "powershell"
                        and ours(str(handler.get("command", "")), layout.launcher)):
                    found.append(event)
    return found


def _registered(data: dict, layout: Layout) -> List[str]:
    from .register import _entry_command, ours
    found = []
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for handler in group.get("hooks", []) if isinstance(group, dict) else []:
                if isinstance(handler, dict) and ours(_entry_command(handler), layout.launcher):
                    found.append(event)
    return found


def _dry_dispatch(layout: Layout, app: str, event: str):
    payload = {"session_id": "agent-kit-sync-doctor", "hook_event_name": event, "cwd": str(Path.home())}
    if event == "pre_tool_use":
        payload.update({"tool_name": "Read", "tool_input": {"file_path": str(Path.home() / "x")}})
    env = dict(os.environ, AGENT_KIT_SYNC_NO_BACKGROUND="1")
    try:
        result = subprocess.run([sys.executable, "-I", "-S", str(layout.launcher), "hook", app, event], input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return result.returncode, result.stdout.decode("utf-8", "replace")


def cmd_repair(args, layout: Layout) -> int:
    from .model import load_machine
    from .register import register
    from .state import Index
    data = toml.load_file(layout.machine_file)
    data["python"] = sys.executable
    atomic_write(layout.machine_file, toml.dumps(data).encode("utf-8"))
    write_launcher(layout)
    machine = load_machine(layout)
    register(layout, machine, Index(layout).hooks)
    say(f"Registered the dispatcher again with {sys.executable}.")
    return 0


# ---------------------------------------------------------------- undo


def cmd_undo(args, layout: Layout) -> int:
    from .model import load_machine
    from .state import Index, RunRecord, run_lock
    record = RunRecord.latest(layout)
    if record is None:
        say("No run to undo.")
        return 0
    if not args.yes:
        say(f"Run {record['run']} changed {len(record['files'])} files, made {len(record['commits'])} commits{', pushed' if record.get('pushed') else ''}, and installed {len(record.get('incoming', {}))} changes from other machines.")
        say("Run agent-kit-sync undo --yes to reverse it.")
        return 0
    machine = load_machine(layout)
    with run_lock(layout, wait=30):
        own = [c for c in record["commits"] if c]
        if own and (layout.config / ".git").exists():
            env = gitops.identity_env(machine.git_identity)
            for commit in reversed(own):
                result = gitops.run(layout.config, "revert", "--no-edit", commit, env=env, check=False)
                if result.returncode != 0:
                    gitops.run(layout.config, "revert", "--abort", check=False)
                    say(f"Could not reverse commit {commit[:12]}, because later changes touch the same lines. Nothing was undone; edit the files back by hand, then sync.")
                    return 1
        index = Index(layout)
        touched = {str(Path(entry["path"])) for entry in record["files"]}
        for entry in reversed(record["files"]):
            path = Path(entry["path"])
            if entry.get("kind") in ("instructions", "settings"):
                continue  # Shared files: restored key by key and block by block below.
            if entry.get("saved"):
                atomic_write(path, Path(entry["saved"]).read_bytes())
            elif path.exists():
                path.unlink()
        for change in reversed(record.get("settings", [])):
            from . import settings
            path = Path(change["path"])
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            new = settings.change(text, settings.file_format(path), change["key"], settings.MISSING if change["missing"] else change["old"])
            atomic_write(path, new.encode("utf-8"))
        for entry in record["files"]:
            if entry.get("kind") == "instructions" and entry.get("saved"):
                _restore_blocks(Path(entry["path"]), Path(entry["saved"]).read_text(encoding="utf-8"))
        # Items this run carried back: put their edited copies back to the reversed version at the next sync.
        for item_key in record.get("edits", []):
            index.items.setdefault(item_key, {})["force"] = True
        for key in [k for k, e in index.copies.items() if str(real(Path(e["path"]))) in touched or str(Path(e["path"])) in touched]:
            index.drop_copy(key)
        index.save()
        data = toml.load_file(layout.machine_file)
        for item_key, commit in record.get("incoming", {}).items():
            data.setdefault("overrides", {})[item_key] = {"kind": "pin", "commit": commit}
        atomic_write(layout.machine_file, toml.dumps(data).encode("utf-8"))
        marker = Path(record["_dir"]) / "record.json"
        saved = json.loads(marker.read_text(encoding="utf-8"))
        saved["undone"] = time.time()
        atomic_write(marker, json.dumps(saved).encode("utf-8"))
    say("Reversed the run. Changes it brought from other machines are pinned on this machine; run agent-kit-sync sync to push the reversal of this machine's own changes.")
    return 0


def _restore_blocks(path: Path, old_text: str) -> None:
    """Put the tool's blocks back as they were in `old_text`, keeping every other change."""
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        old_blocks = sections.extract(old_text)
        now = sections.extract(current)
    except sections.MarkerError:
        return
    managed = list(set(old_blocks) | set(now))
    atomic_write(path, sections.apply(current, old_blocks, managed).encode("utf-8"))


# ---------------------------------------------------------------- uninstall


def cmd_uninstall(args, layout: Layout) -> int:
    from .engine import Engine
    from .model import load_machine
    from .register import unregister
    from .state import Held, run_lock
    if not args.yes:
        say("This removes agent-kit-sync's blocks, settings, hook entries, and installed copies from this machine and restores what they replaced. Run agent-kit-sync uninstall --yes to continue.")
        return 0
    from types import SimpleNamespace
    from .cli import cmd_sync
    cmd_sync(SimpleNamespace(background=False, quiet_wait=False, now=True, offline=False), layout)
    if Held(layout).all():
        say("Resolve the held changes first (agent-kit-sync held), so no edit is lost.")
        return 1
    machine = load_machine(layout)
    with run_lock(layout, wait=30):
        engine = Engine(layout)
        engine.restoring = True
        empty = []
        from .plan import Plan
        plan = Plan()
        engine.index.hooks = []
        engine._write_copies(plan)
        engine._write_sections(plan)
        engine._write_settings(plan)
        engine.index.save()
        unregister(layout, machine)
        from .setup import restore_registrations
        restore_registrations(layout)
    say(f"Removed agent-kit-sync from this machine. The kit folder {layout.root} is left in place; delete it yourself when you no longer need its backups and logs. Codex keeps its approval records for the removed entries until you clear them in /hooks.")
    return 0


# ---------------------------------------------------------------- update


def cmd_update(args, layout: Layout) -> int:
    from .model import load_machine
    from .selfupdate import update
    policy = "apply" if args.apply else load_machine(layout).policy.get("tool_updates", "apply")
    outcome, detail = update(layout, policy)
    say(f"Tool update: {outcome} {detail}".strip())
    return 0 if outcome in ("current", "updated", "pinned", "held") else 1
