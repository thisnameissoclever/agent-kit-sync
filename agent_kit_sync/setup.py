"""First-time setup steps. The setup skill runs these in order and asks the user between them.

check     prints prerequisites and detected apps as JSON
init      creates the kit folder, installs the tool, and clones the config repo
scan      lists what is already on the machine as JSON
report    compares the scan with the config repo and writes state/setup-report.md
apply     writes the machine file and installs, using the user's answers
register  registers the dispatcher in each app
"""

from __future__ import annotations
from . import processes

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from . import VERSION, gitops, toml
from .fsutil import atomic_write, read_or_none
from .paths import APPS, Layout, detect_apps, os_name

re_network = re.compile(r"^(?:https?://|ssh://|git://|[\w.-]+@[\w.-]+:)")
SKIP_FOLDERS = {"node_modules", ".git", "Library", "AppData", ".Trash", ".cache", "Applications", "Pictures", "Music", "Movies"}


def out(data) -> None:
    sys.stdout.write(json.dumps(data, indent=1, default=str) + "\n")


def run(args, layout: Layout) -> int:
    return {"check": check, "init": init, "scan": scan, "report": report, "apply": apply, "register": register_step}[args.step](args, layout)


# ---------------------------------------------------------------- check


def check(args, layout: Layout) -> int:
    pythons = []
    for name in ("python3", "python", "py"):
        found = shutil.which(name)
        if found and found not in pythons:
            pythons.append(found)
    git = shutil.which("git")
    git_version = None
    if git:
        git_version = processes.run([git, "--version"], stdout=subprocess.PIPE, timeout=10).stdout.decode().strip()
    install = {
        "macos": {"git": "xcode-select --install", "python": "brew install python (or xcode-select --install)"},
        "windows": {"git": "winget install --id Git.Git -e", "python": "winget install --id Python.Python.3.13 -e"},
        "linux": {"git": "sudo apt install git (or your distribution's package manager)", "python": "sudo apt install python3"},
    }[os_name()]
    out({
        "os": os_name(),
        "python": {"running": sys.executable, "version": sys.version.split()[0], "found": pythons, "ok": sys.version_info >= (3, 9)},
        "git": {"path": git, "version": git_version, "ok": git is not None},
        "gh": shutil.which("gh"),
        "apps": {app: str(home) for app, home in detect_apps().items()},
        "kit_folder": str(layout.root),
        "kit_folder_exists": layout.root.exists(),
        "install_commands": install,
    })
    return 0


# ---------------------------------------------------------------- init


def init(args, layout: Layout) -> int:
    from .commands import ensure_tool, write_launcher
    from .fsutil import private_dir
    layout.root.mkdir(parents=True, exist_ok=True)
    private_dir(layout.root)
    for folder in (layout.state, layout.logs, layout.local_modules):
        folder.mkdir(parents=True, exist_ok=True)
    version = ensure_tool(layout, args.tool_url)
    write_launcher(layout)
    result = {"kit_folder": str(layout.root), "tool_version": version}
    if args.config_url and not layout.config.exists():
        visibility = public_check(args.config_url)
        result["config_public"] = visibility
        try:
            gitops.run(None, "clone", "-q", args.config_url, str(layout.config), timeout=gitops.NETWORK_TIMEOUT)
            result["config_cloned"] = True
        except gitops.GitError as error:
            result["config_cloned"] = False
            result["config_error"] = str(error)
    elif not layout.config.exists() and args.create:
        layout.config.mkdir(parents=True)
        gitops.run(layout.config, "init", "-q", "-b", "main")
        atomic_write(layout.config / "kit.toml", b"format = 1\n")
        atomic_write(layout.config / ".gitattributes", b"* text=auto eol=lf\n")
        result["config_created"] = True
    out(result)
    return 0


def public_check(url: str) -> bool:
    """True when a hosted repository can be read with every saved credential turned off.

    A folder on this machine is never public, so only network addresses are checked.
    """
    if not re_network.match(url):
        return False
    https = url
    match = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?$", url)
    if match:
        https = f"https://{match.group(1)}/{match.group(2)}.git"
    if not https.startswith(("http://", "https://")):
        return False
    env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_SSH_COMMAND": "false"}
    result = gitops.run(None, "-c", "credential.helper=", "ls-remote", "--heads", https, check=False, timeout=30, env=env)
    return result.returncode == 0


# ---------------------------------------------------------------- scan


def scan(args, layout: Layout) -> int:
    out(scan_machine())
    return 0


def scan_machine() -> dict:
    apps = detect_apps()
    found = {"apps": {app: str(home) for app, home in apps.items()}, "skills": [], "agents": [], "hooks": [], "instructions": [], "projects_candidates": find_projects_folders()}
    for app, home in apps.items():
        for folder in skill_folders(app, home):
            if not folder.is_dir():
                continue
            for child in sorted(folder.iterdir()):
                if child.name.startswith(".") or child.name == "synced":
                    continue
                skill = child / "SKILL.md"
                if skill.is_file():
                    found["skills"].append({"app": app, "name": child.name, "path": str(child), "link": child.is_symlink(), "description": frontmatter_value(skill, "description")})
        agents = home / "agents"
        if agents.is_dir():
            for child in sorted(agents.iterdir()):
                if child.suffix in (".md", ".toml") and child.is_file():
                    found["agents"].append({"app": app, "name": child.stem, "path": str(child)})
        for settings_name in ("settings.json", "hooks.json"):
            path = home / settings_name
            data = read_json(path)
            for event, groups in (data.get("hooks") or {}).items():
                for g, group in enumerate(groups if isinstance(groups, list) else []):
                    for handler in group.get("hooks", []) if isinstance(group, dict) else []:
                        if isinstance(handler, dict):
                            found["hooks"].append({"app": app, "file": str(path), "event": event, "matcher": group.get("matcher"), "command": handler.get("command"), "args": handler.get("args"), "timeout": handler.get("timeout"), "status_message": handler.get("statusMessage"), "position": g})
        if app == "codex":
            config = home / "config.toml"
            try:
                inline = (toml.load_file(config).get("hooks") or {})
            except Exception:
                inline = {}
            for event, groups in inline.items():
                if event == "state" or not isinstance(groups, list):
                    continue
                for group in groups:
                    for handler in group.get("hooks", []) if isinstance(group, dict) else []:
                        found["hooks"].append({"app": app, "file": str(config), "event": event, "matcher": group.get("matcher"), "command": handler.get("command"), "inline": True})
        for name in ("CLAUDE.md", "AGENTS.md"):
            path = home / name
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                headings = [m.group(2) for m in re.finditer(r"^(#{1,3})\s+(.*?)\s*$", text, re.M)]
                found["instructions"].append({"app": app, "path": str(path), "bytes": len(text.encode("utf-8")), "headings": headings})
    return found


def skill_folders(app: str, home: Path) -> List[Path]:
    if app == "codex":
        return [Path.home() / ".agents" / "skills", home / "skills"]
    if app == "antigravity":
        return [home / "config" / "skills"]
    return [home / "skills"]


def frontmatter_value(path: Path, key: str) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^" + re.escape(key) + r":\s*(.+)$", text[:4000], re.M)
    return match.group(1).strip().strip('"') if match else ""


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def find_projects_folders(name: str = "") -> List[str]:
    """Folders that look like a projects folder: the given name, or ones holding several Git repositories."""
    roots = [Path.home()]
    if os_name() == "macos":
        roots += [p for p in Path("/Volumes").glob("*") if p.is_dir()]
    elif os_name() == "windows":
        roots += [Path(f"{letter}:\\") for letter in "DEFGHIJ" if Path(f"{letter}:\\").exists()]
    else:
        roots += [p for base in (Path("/mnt"), Path("/media")) if base.is_dir() for p in base.glob("*") if p.is_dir()]
    found = []
    for root in roots:
        try:
            children = [c for c in root.iterdir() if c.is_dir() and c.name not in SKIP_FOLDERS and not c.name.startswith(".")]
        except OSError:
            continue
        for child in children:
            if name and child.name == name:
                found.append(str(child))
                continue
            try:
                repos = sum(1 for g in child.iterdir() if (g / ".git").exists())
            except OSError:
                continue
            if repos >= 3:
                found.append(str(child))
    return sorted(set(found))


# ---------------------------------------------------------------- report


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a[:20000], b[:20000], autojunk=False).ratio()


def report(args, layout: Layout) -> int:
    from .engine import Engine
    machine_scan = scan_machine()
    inventory_items = []
    try:
        from .model import load_inventory
        inventory = load_inventory(layout.config)
        for module in inventory.modules:
            for item in module.items:
                text = ""
                rel = item.data.get("file") or (item.data.get("dir", "").rstrip("/") + "/SKILL.md" if item.kind == "skill" else None)
                if rel and (module.root / rel).is_file():
                    text = (module.root / rel).read_text(encoding="utf-8", errors="replace")
                inventory_items.append({"module": module.name, "kind": item.kind, "name": item.name, "text": text, "events": item.data.get("events", []), "file": item.data.get("file", "")})
    except Exception as error:
        inventory_items = []
        machine_scan["inventory_error"] = str(error)
    pairs = []
    for local in machine_scan["skills"] + machine_scan["agents"]:
        path = Path(local["path"])
        local_text = read_or_none(path / "SKILL.md" if path.is_dir() else path)
        local_text = local_text.decode("utf-8", "replace") if local_text else ""
        for synced in inventory_items:
            if synced["kind"] not in ("skill", "agent", "section"):
                continue
            score = similarity(local_text, synced["text"]) if local_text and synced["text"] else 0.0
            same_name = local["name"].lower() == synced["name"].lower()
            if same_name or score >= 0.35:
                pairs.append({"local": local["path"], "app": local["app"], "synced": f"{synced['module']}/{synced['name']}", "same_name": same_name, "text_similarity": round(score, 2), "identical": local_text == synced["text"]})
    for hook in machine_scan["hooks"]:
        command = " ".join([str(hook.get("command") or "")] + [str(a) for a in (hook.get("args") or [])])
        for synced in inventory_items:
            if synced["kind"] != "hook":
                continue
            script = Path(synced["file"]).name
            if script and script in command:
                pairs.append({"local": command, "app": hook["app"], "synced": f"{synced['module']}/{synced['name']}", "same_script": True})
    lines = [f"# Setup report for this machine", "", f"agent-kit-sync {VERSION}, {os_name()}, apps: {', '.join(machine_scan['apps']) or 'none'}.", ""]
    lines += ["## Possible duplicates", "", "Each pair shares a name, a script, or much of its text. The setup agent decides which do the same job and recommends what to do.", ""]
    for pair in pairs:
        lines.append("- " + json.dumps(pair))
    if not pairs:
        lines.append("- None found by name, script, or text.")
    lines += ["", "## On this machine", ""]
    for kind in ("skills", "agents", "hooks", "instructions"):
        lines.append(f"### {kind.capitalize()}")
        lines.append("")
        for entry in machine_scan[kind]:
            lines.append("- " + json.dumps(entry, default=str))
        lines.append("")
    lines += ["## Projects folder candidates", ""] + [f"- {p}" for p in machine_scan["projects_candidates"]] + [""]
    path = layout.state / "setup-report.md"
    atomic_write(path, "\n".join(lines).encode("utf-8"))
    out({"report": str(path), "duplicates": pairs, "counts": {k: len(machine_scan[k]) for k in ("skills", "agents", "hooks", "instructions")}})
    return 0


# ---------------------------------------------------------------- apply


def apply(args, layout: Layout) -> int:
    """Write the machine file from the user's answers and install.

    The answers file is TOML with the machine file's keys plus:
      approve = ["module/item", ...]          items to install even though new_items asks
      replace_existing = ["app|module/item"]  unmanaged files the config repo version may replace
      remove_registrations = [{file, event, command}]  hook entries adopted into modules
    """
    from .commands import write_launcher
    from .engine import Engine
    from .register import register
    if not args.answers:
        out({"error": "give the answers file with --answers"})
        return 2
    answers = toml.load_file(Path(args.answers))
    machine = {key: answers[key] for key in ("name", "apps", "app_homes", "tags", "python", "projects", "worktrees", "git_identity", "policy", "overrides", "cloud") if key in answers}
    machine.setdefault("python", sys.executable)
    machine.setdefault("apps", sorted(detect_apps()))
    for folder in ("projects", "worktrees"):
        if machine.get(folder):
            Path(machine[folder]).mkdir(parents=True, exist_ok=True)
    atomic_write(layout.machine_file, toml.dumps(machine).encode("utf-8"))
    engine = Engine(layout)
    engine.approve_new = set(answers.get("approve", []))
    engine.replace_existing = set(answers.get("replace_existing", []))
    result = engine.run(network=gitops.has_remote(layout.config))
    write_launcher(layout)
    changed = register(layout, engine.machine, engine.index.hooks)
    # Only now that the dispatcher runs the adopted hooks, remove their old registrations.
    installed_scripts = {Path(h["path"]).name for h in engine.index.hooks}
    wanted = [e for e in answers.get("remove_registrations", []) if any(name in str(e.get("command", "")) for name in installed_scripts)]
    skipped = [e for e in answers.get("remove_registrations", []) if e not in wanted]
    removed = remove_registrations(layout, wanted)
    # Approve last: removing a registration can move the dispatcher's group, and Codex keys each approval by position.
    codex_trust = _settle_codex(layout, engine.machine, engine.index.hooks, changed)
    out({"installed": len(result.installed), "held": result.held, "errors": result.errors, "registered": changed, "removed_registrations": removed, "codex_approval": codex_trust,
         "kept_registrations": [e.get("command") for e in skipped], "pushed": result.pushed})
    return 1 if result.errors else 0


def _settle_codex(layout: Layout, machine, hooks: List[dict], changed: Dict[str, bool]) -> Dict[str, object]:
    """Approve agent-kit-sync's own Codex entries and say what happened, for the setup skill to report."""
    from .codex_trust import settle
    from .state import Alerts
    result = settle(layout, machine, hooks, Alerts(layout), bool(changed.get("codex")), force=True)
    if result is None:
        return {"needed": False}
    return {"needed": True, "approved": result.approved, "remaining": result.remaining, "error": result.error}


def remove_registrations(layout: Layout, entries: List[dict]) -> List[str]:
    """Remove hook entries the user adopted into modules, after backing up each file."""
    removed = []
    by_file: Dict[str, List[dict]] = {}
    for entry in entries:
        by_file.setdefault(entry["file"], []).append(entry)
    for file_name, wanted in by_file.items():
        path = Path(file_name)
        raw = read_or_none(path)
        if raw is None:
            continue
        backup = layout.state / "backups" / "registrations" / (path.parent.name + "-" + path.name)
        if not backup.exists():
            atomic_write(backup, raw)
            _private(backup)
        record = layout.state / "backups" / "registrations" / "removed.json"
        previous = json.loads(record.read_text(encoding="utf-8")) if record.exists() else []
        previous += [{"file": str(path), "event": e["event"], "command": e["command"]} for e in wanted]
        data = json.loads(raw.decode("utf-8"))
        hooks = data.get("hooks") or {}
        for entry in wanted:
            groups = hooks.get(entry["event"], [])
            for group in groups:
                kept = [h for h in group.get("hooks", []) if str(h.get("command")) != entry["command"]]
                if len(kept) != len(group.get("hooks", [])):
                    removed.append(f"{entry['event']}: {entry['command']}")
                group["hooks"] = kept
            hooks[entry["event"]] = [g for g in groups if g.get("hooks")]
            if not hooks[entry["event"]]:
                del hooks[entry["event"]]
        from .settings import json_indent
        original = raw.decode("utf-8")
        atomic_write(path, (json.dumps(data, indent=json_indent(original), ensure_ascii=False) + ("\n" if original.endswith("\n") else "")).encode("utf-8"))
        atomic_write(record, json.dumps(previous, indent=1).encode("utf-8"))
    return removed


def _private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def restore_registrations(layout: Layout) -> List[str]:
    """Put back the hook entries setup removed, from the saved backup of each file."""
    record = layout.state / "backups" / "registrations" / "removed.json"
    if not record.exists():
        return []
    restored = []
    for entry in json.loads(record.read_text(encoding="utf-8")):
        path = Path(entry["file"])
        backup = layout.state / "backups" / "registrations" / (path.parent.name + "-" + path.name)
        original = read_json(backup)
        group = next((g for g in (original.get("hooks") or {}).get(entry["event"], []) for h in g.get("hooks", []) if str(h.get("command")) == entry["command"]), None)
        if group is None:
            continue
        raw = read_or_none(path)
        data = json.loads(raw.decode("utf-8")) if raw else {}
        groups = data.setdefault("hooks", {}).setdefault(entry["event"], [])
        if any(str(h.get("command")) == entry["command"] for g in groups for h in g.get("hooks", [])):
            continue
        handler = next(h for h in group["hooks"] if str(h.get("command")) == entry["command"])
        groups.append(dict(group, hooks=[handler]))
        from .settings import json_indent
        text = raw.decode("utf-8") if raw else ""
        atomic_write(path, (json.dumps(data, indent=json_indent(text), ensure_ascii=False) + ("\n" if text.endswith("\n") or not text else "")).encode("utf-8"))
        restored.append(entry["command"])
    record.unlink()
    return restored


def register_step(args, layout: Layout) -> int:
    from .model import load_machine
    from .register import register
    from .state import Index
    machine = load_machine(layout)
    hooks = Index(layout).hooks
    changed = register(layout, machine, hooks)
    out({"registered": changed, "codex_approval": _settle_codex(layout, machine, hooks, changed)})
    return 0
