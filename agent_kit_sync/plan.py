"""Working out every installed copy, section block, and setting this machine should have."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from . import render
from .model import Inventory, Item, Machine, Module, module_applies
from .paths import Layout, os_name
from .tree import Tree

INSTRUCTION_FILES = {"claude": "CLAUDE.md", "codex": "AGENTS.md"}
SETTINGS_FILES = {"claude": "settings.json", "codex": "config.toml"}
CODE_SUFFIXES = (".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl")
CODE_SETTING_ROOTS = ("hooks", "permissions", "env", "apiKeyHelper", "mcp_servers", "mcpServers", "sandbox_mode", "approval_policy", "shell_environment_policy")
KNOWN_TEXT_SETTINGS = ("model", "model_reasoning_effort", "theme", "outputStyle", "effortLevel", "autoUpdatesChannel", "tui")


@dataclass
class Copy:
    """One file the tool writes, rendered from one source file of an item."""

    key: str                 # stable identifier: app|module/item|relative path
    app: str                 # app it is for, or "kit" for files under installed/
    module: str
    item: str
    kind: str                # skill, agent, hook, file
    path: Path               # destination on this machine
    source_rel: str          # source file, relative to the module folder
    data: bytes              # rendered bytes
    executable: bool
    render_kind: str         # copy, text, agent-md, agent-codex
    code: bool
    local: bool              # from a local module, which never syncs


@dataclass
class SectionBlock:
    app: str
    key: str                 # module/section
    module: str
    item: str
    source_rel: str
    content: str
    local: bool


@dataclass
class SettingWrite:
    app: str
    key: str                 # dotted key
    item_key: str            # module/item
    value: object
    mode: str
    code: bool
    local: bool


@dataclass
class HookSpec:
    module: str
    name: str
    path: Path               # installed hook file
    events: List[str]
    tools: List[str]
    protocol: str
    args: List[str]
    runtime: str
    timeout: int
    on_failure: str
    apps: List[str]
    order: int


@dataclass
class Plan:
    copies: List[Copy] = field(default_factory=list)
    sections: List[SectionBlock] = field(default_factory=list)
    settings: List[SettingWrite] = field(default_factory=list)
    hooks: List[HookSpec] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)   # modules that could not be rendered
    cursor_blocks: List[Tuple[str, str, str]] = field(default_factory=list)   # (module, item, text) of each section for Cursor

    def instruction_files(self, machine: Machine) -> Dict[str, Path]:
        return {app: machine.app_homes[app] / name for app, name in INSTRUCTION_FILES.items() if app in machine.apps}

    def drop_module(self, name: str) -> None:
        """Treat a module as one that could not be planned, so this machine keeps what it installed for it."""
        self.copies = [c for c in self.copies if c.module != name]
        self.sections = [s for s in self.sections if s.key.split("/", 1)[0] != name]
        self.settings = [s for s in self.settings if s.item_key.split("/", 1)[0] != name]
        self.hooks = [h for h in self.hooks if h.module != name]
        self.cursor_blocks = [b for b in self.cursor_blocks if b[0] != name]
        if name not in self.failed:
            self.failed.append(name)


def runtimes_available() -> Dict[str, bool]:
    return {"python": True, "node": shutil.which("node") is not None, "bash": shutil.which("bash") is not None, "powershell": shutil.which("pwsh") is not None or shutil.which("powershell") is not None}


TEXT_SUFFIXES = (".md", ".markdown")
AGENT_CODE_KEYS = ("hooks", "mcpServers", "mcp_servers", "permissionMode", "permission_mode", "sandbox_mode", "tools", "disallowedTools")


def is_code_path(rel: str, kind: str, data: Optional[bytes] = None, executable: bool = False) -> bool:
    """Code is anything that can run or widen what an agent may do: everything except Markdown text."""
    if kind == "hook" or executable:
        return True
    if data is not None and data.startswith(b"#!"):
        return True
    suffix = PurePosixPath(rel).suffix.lower()
    if kind in ("skill", "file"):
        return suffix not in TEXT_SUFFIXES
    return suffix in CODE_SUFFIXES


def agent_is_code(source: bytes) -> bool:
    """A subagent definition is code when its header can run commands or change permissions."""
    import re as _re
    text = source.decode("utf-8", "replace")
    if "{{kit.hook " in text:
        return True
    header = text.split("\n---", 1)[0] if text.startswith("---") else ""
    for key in AGENT_CODE_KEYS:
        if _re.search(r"^(?:[a-z]+\.)?" + _re.escape(key) + r"\s*:", header, _re.M):
            return True
    return False


def is_code_setting(app: str, key: str) -> bool:
    root = key.split(".")[0]
    if root in CODE_SETTING_ROOTS:
        return True
    return root not in KNOWN_TEXT_SETTINGS


def cli_command(layout: Layout, machine: Machine) -> str:
    """How an agent runs agent-kit-sync: the wrapper in bin/, with forward slashes, which every shell accepts."""
    if os_name() == "windows":
        path = (layout.bin / "agent-kit-sync.cmd").as_posix()
        return f'& "{path}"' if " " in path else path
    path = str(layout.bin / "agent-kit-sync")
    return shlex.quote(path)


def hook_cli(layout: Layout, machine: Machine) -> str:
    python = machine.python or sys.executable
    parts = [python, "-I", "-S", str(layout.launcher)]
    if os_name() == "windows":
        return windows_cli(parts)
    return " ".join(shlex.quote(part) for part in parts)


PLAIN_WORD = re.compile(r"[\w./:-]+")


def windows_cli(parts: List[str]) -> str:
    """A command for Windows that Git Bash and PowerShell both run as written.

    Paths use forward slashes, which Windows programs accept: Git Bash removes a backslash from an
    unquoted word, and a backslash in a JSON or YAML double-quoted string starts an escape. A part
    with a space or another character a shell reads is quoted for Bash and PowerShell alike. When
    the program itself is quoted, PowerShell needs its call operator in front, which Bash refuses,
    so it is added only where Claude Code runs hooks in PowerShell, on a machine without Git Bash.
    A part no quoting suits for both shells, a path holding both ' and $, gets Command Prompt
    quoting, which Git Bash runs and PowerShell does not. A command inside a double-quoted string is
    escaped where it is placed; see render.substitute.
    """
    from .register import git_bash, windows_hook_quote
    parts = [part.replace("\\", "/") for part in parts]
    quoted = [part if PLAIN_WORD.fullmatch(part) else windows_hook_quote(part) for part in parts]
    if None in quoted:
        return subprocess.list2cmdline(parts)  # No quoting suits both shells; Command Prompt quoting is the best left.
    command = " ".join(quoted)
    if quoted[0] != parts[0] and not git_bash():
        command = "& " + command
    return command


def hook_command(layout: Layout, machine: Machine, module: str, name: str) -> str:
    return hook_cli(layout, machine) + f" run-hook {module}/{name}"


def hook_commands(layout: Layout, machine: Machine, modules: List[Module]) -> Dict[str, str]:
    """The command for every hook in the inventory, which {{kit.hook MODULE/HOOK}} becomes."""
    return {item.key: hook_command(layout, machine, module.name, item.name) for module in modules for item in module.of("hook")}


def values_for(layout: Layout, machine: Machine, module: str, app: str, hooks: Dict[str, str]) -> render.Values:
    """This machine's placeholder values. A folder the machine file does not set is a default, so
    edits carried back never turn a path into that placeholder; the user did not choose it."""
    from .status_directory import migration_enabled
    return render.Values(
        home=str(Path.home()),
        projects=str(machine.projects) if machine.projects else "",
        worktrees=str(machine.worktrees) if machine.worktrees else "",
        module=str(layout.installed_module(module)),
        app=app,
        os=machine.os,
        cli=cli_command(layout, machine),
        hooks=hooks,
        reverse_skip=tuple(machine.defaulted),
        canonical_status=migration_enabled(layout),
    )


def item_templates(item) -> List[str]:
    """Module paths of the files whose placeholders are replaced, for a skill or a files item.

    A files item lists its templates relative to its folder, or `templates = true` when its
    path is a single file.
    """
    listed = item.data.get("templates", [])
    if item.kind == "skill":
        base = item.data["dir"].rstrip("/")
    elif item.kind == "file":
        base = item.data["path"].rstrip("/")
        if listed is True:
            return [base]
    else:
        return []
    return [f"{base}/{rel}" for rel in listed] if isinstance(listed, list) else []


def skill_dest(machine: Machine, app: str, name: str) -> Optional[Path]:
    homes = machine.app_homes
    if app == "claude":
        return homes["claude"] / "skills" / name
    if app == "codex":
        return Path.home() / ".agents" / "skills" / name
    if app == "cursor":
        if "claude" in machine.apps or "codex" in machine.apps:
            return None  # Cursor reads Claude Code's and Codex's skill folders.
        return homes["cursor"] / "skills" / name
    return None


def agent_dest(machine: Machine, app: str, name: str) -> Optional[Path]:
    homes = machine.app_homes
    if app == "claude":
        return homes["claude"] / "agents" / f"{name}.md"
    if app == "codex":
        return homes["codex"] / "agents" / f"{name}.toml"
    if app == "cursor":
        return homes["cursor"] / "agents" / f"{name}.md"
    return None


class Planner:
    def __init__(self, layout: Layout, machine: Machine, modules: List[Module], tree_for):
        """`tree_for(module, item)` returns the Tree to read one item's files from."""
        self.layout = layout
        self.machine = machine
        self.modules = modules
        self.tree_for = tree_for
        self.runtimes = runtimes_available()
        self.overrides = machine.overrides
        self.hook_commands = hook_commands(layout, machine, modules)

    def override(self, module: str, item: Optional[str] = None) -> Optional[dict]:
        if item and f"{module}/{item}" in self.overrides:
            return self.overrides[f"{module}/{item}"]
        return self.overrides.get(module)

    def disabled(self, module: str, item: Optional[str] = None) -> bool:
        found = self.overrides.get(module)
        if found and found.get("kind") == "disable":
            return True
        if item:
            found = self.overrides.get(f"{module}/{item}")
            return bool(found and found.get("kind") == "disable")
        return False

    def apps_for(self, module: Module) -> List[str]:
        from .paths import SUPPORTED_APPS
        return [app for app in module.apps if app in self.machine.apps and app in SUPPORTED_APPS]

    def build(self) -> Plan:
        plan = Plan()
        for order, module in enumerate(self.modules):
            reason = module_applies(module, self.machine, self.runtimes)
            if reason is None and self.disabled(module.name):
                reason = "disabled on this machine"
            if reason is not None:
                plan.skipped[module.name] = reason
                continue
            before = (len(plan.copies), len(plan.sections), len(plan.settings), len(plan.hooks), len(plan.cursor_blocks))
            try:
                self._module(plan, module, order)
            except (render.RenderError, OSError, ValueError, UnicodeDecodeError) as error:
                # Drop whatever this module planned, so the engine keeps its previous copies.
                del plan.copies[before[0]:], plan.sections[before[1]:], plan.settings[before[2]:], plan.hooks[before[3]:], plan.cursor_blocks[before[4]:]
                plan.errors.append(f"module {module.name}: {error}")
                plan.failed.append(module.name)
                plan.skipped[module.name] = f"error: {error}"
        return plan

    def _local_text(self, module: str, item: str) -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
        found = self.override(module, item)
        if not found:
            return None, None, None
        path = found.get("file")
        data = None
        if path:
            target = Path(path)
            if not target.is_absolute():
                target = self.layout.root / target
            data = target.read_bytes() if target.is_file() else None
        kind = found.get("kind")
        text = data.decode("utf-8") if data is not None else None
        if kind == "prepend":
            return text, None, None
        if kind == "append":
            return None, text, None
        if kind == "replace":
            return None, None, data
        return None, None, None

    def _overridden(self, module: Module, item: Item, data: bytes, main: bool) -> Tuple[bytes, Optional[str]]:
        """Apply a replace, prepend, or append override to an item's main file. Returns the bytes and the override kind."""
        if not main:
            return data, None
        prepend, append, replace = self._local_text(module.name, item.name)
        if replace is not None:
            return replace, "replace"
        if prepend is None and append is None:
            return data, None
        from .sections import wrap_local
        return wrap_local(data.decode("utf-8"), prepend, append).encode("utf-8"), "local"

    def _module(self, plan: Plan, module: Module, order: int) -> None:
        apps = self.apps_for(module)
        for item in module.items:
            if self.disabled(module.name, item.name):
                continue
            tree: Tree = self.tree_for(module, item)
            if item.kind == "section":
                self._section(plan, module, item, apps, tree)
            elif item.kind == "skill":
                self._skill(plan, module, item, apps, tree)
            elif item.kind == "agent":
                self._agent(plan, module, item, apps, tree)
            elif item.kind == "hook":
                self._hook(plan, module, item, apps, tree, order)
            elif item.kind == "file":
                self._file(plan, module, item, tree)
            elif item.kind == "setting":
                self._setting(plan, module, item)

    def _values(self, module: Module, app: str) -> render.Values:
        return values_for(self.layout, self.machine, module.name, app, self.hook_commands)

    def _section(self, plan: Plan, module: Module, item: Item, apps: List[str], tree: Tree) -> None:
        prepend, append, replace = self._local_text(module.name, item.name)
        for app in apps:
            if app != "cursor" and app not in INSTRUCTION_FILES:
                continue
            rel = dict(item.data.get("variants", {})).get(app, item.data["file"])
            raw = replace if replace is not None else tree.read(rel)
            if raw is None:
                raise render.RenderError(f"section {item.name} file {rel} is missing")
            text = render.render_text(raw.decode("utf-8"), self._values(module, app)).text
            from .sections import with_local
            if prepend is not None or append is not None:
                text = with_local(text, prepend, append)
            if app == "cursor":
                # Cursor reads rule files, written as output only; see cursor_rules.
                plan.cursor_blocks.append((module.name, item.name, text))
            else:
                plan.sections.append(SectionBlock(app, item.key, module.name, item.name, rel, text, module.local))

    def _skill(self, plan: Plan, module: Module, item: Item, apps: List[str], tree: Tree) -> None:
        rel_dir = item.data["dir"].rstrip("/")
        templates = set(item.data.get("templates", []))
        files = tree.files(rel_dir, module.ignore)
        for app in apps:
            dest = skill_dest(self.machine, app, item.name)
            if dest is None:
                continue
            for rel, executable in files:
                raw = tree.read(f"{rel_dir}/{rel}")
                if raw is None:
                    continue
                if rel in templates:
                    data = render.render_text(raw.decode("utf-8"), self._values(module, app), header=rel.endswith(".md")).text.encode("utf-8")
                    kind = "text"
                else:
                    data, kind = raw, "copy"
                data, local = self._overridden(module, item, data, rel == "SKILL.md")
                if local:
                    kind = "override-" + local + "-" + kind
                plan.copies.append(Copy(
                    key=f"{app}|{item.key}|{rel}", app=app, module=module.name, item=item.name, kind="skill",
                    path=dest / Path(*PurePosixPath(rel).parts), source_rel=f"{rel_dir}/{rel}", data=data,
                    executable=executable, render_kind=kind, code=is_code_path(rel, "skill", raw, executable), local=module.local,
                ))

    def _agent(self, plan: Plan, module: Module, item: Item, apps: List[str], tree: Tree) -> None:
        rel = item.data["file"]
        raw = tree.read(rel)
        if raw is None:
            raise render.RenderError(f"subagent {item.name} file {rel} is missing")
        source = raw.decode("utf-8")
        has_hooks = agent_is_code(raw)
        for app in apps:
            dest = agent_dest(self.machine, app, item.name)
            if dest is None:
                continue
            values = self._values(module, app)
            if app == "codex":
                data, kind = render.render_agent_codex(source, values), "agent-codex"
            else:
                data, kind = render.render_agent_md(source, app, values).text, "agent-md"
            if app != "codex":
                encoded, local = self._overridden(module, item, data.encode("utf-8"), True)
                if local:
                    data, kind = encoded.decode("utf-8"), "override-" + local + "-" + kind
            plan.copies.append(Copy(
                key=f"{app}|{item.key}|{rel}", app=app, module=module.name, item=item.name, kind="agent", path=dest,
                source_rel=rel, data=data.encode("utf-8"), executable=False, render_kind=kind, code=has_hooks, local=module.local,
            ))

    def _hook_args(self, module: Module, item: Item) -> List[str]:
        """The hook's args with this machine's values; {{kit.app}} is filled in when the hook runs."""
        from .dispatch import APP_ARG
        values = self._values(module, APP_ARG)
        try:
            return [render.substitute(str(arg), values) for arg in item.data.get("args", [])]
        except render.RenderError as error:
            raise render.RenderError(f"hook {item.name} args: {error}") from None

    def _hook(self, plan: Plan, module: Module, item: Item, apps: List[str], tree: Tree, order: int) -> None:
        rel = item.data["file"]
        raw = tree.read(rel)
        if raw is None:
            raise render.RenderError(f"hook {item.name} file {rel} is missing")
        dest = self.layout.installed_module(module.name) / Path(*PurePosixPath(rel).parts)
        plan.copies.append(Copy(
            key=f"kit|{item.key}|{rel}", app="kit", module=module.name, item=item.name, kind="hook", path=dest,
            source_rel=rel, data=raw, executable=True, render_kind="copy", code=True, local=module.local,
        ))
        runtime = item.data.get("runtime", "python")
        if not self.runtimes.get(runtime, False):
            plan.errors.append(f"hook {item.key} needs {runtime}, which this machine does not have")
            return
        plan.hooks.append(HookSpec(
            module=module.name, name=item.name, path=dest, events=list(item.data.get("events", [])),
            tools=list(item.data.get("tools", [])), protocol=item.data.get("protocol", "native"),
            args=self._hook_args(module, item), runtime=runtime,
            timeout=int(item.data.get("timeout", 10)), on_failure=item.data.get("on_failure", "open"),
            apps=list(item.data.get("apps", apps)), order=order,
        ))
        for test in item.data.get("tests", []):
            test_raw = tree.read(test)
            if test_raw is not None:
                plan.copies.append(Copy(
                    key=f"kit|{item.key}|{test}", app="kit", module=module.name, item=item.name, kind="hook",
                    path=self.layout.installed_module(module.name) / Path(*PurePosixPath(test).parts),
                    source_rel=test, data=test_raw, executable=False, render_kind="copy", code=True, local=module.local,
                ))

    def _file(self, plan: Plan, module: Module, item: Item, tree: Tree) -> None:
        rel = item.data["path"].rstrip("/")
        entries = tree.files(rel, module.ignore)
        targets = [(f"{rel}/{sub}", sub, executable) for sub, executable in entries] if entries else [(rel, PurePosixPath(rel).name, tree.executable(rel))]
        base = self.layout.installed_module(module.name)
        templates = set(item_templates(item))
        for source_rel, _sub, executable in targets:
            raw = tree.read(source_rel)
            if raw is None:
                continue
            kind = "copy"
            if source_rel in templates:
                # A support file holds this machine's values; it has no app of its own.
                data = render.render_text(raw.decode("utf-8"), self._values(module, ""), header=source_rel.endswith(".md")).text.encode("utf-8")
                kind = "text"
            else:
                data = raw
            data, local = self._overridden(module, item, data, len(targets) == 1)
            plan.copies.append(Copy(
                key=f"kit|{item.key}|{source_rel}", app="kit", module=module.name, item=item.name, kind="file",
                path=base / Path(*PurePosixPath(source_rel).parts), source_rel=source_rel, data=data,
                executable=executable, render_kind=("override-" + local + "-" + kind) if local else kind, code=is_code_path(source_rel, "file", raw, executable), local=module.local,
            ))

    def _setting(self, plan: Plan, module: Module, item: Item) -> None:
        app = item.data["app"]
        if app not in self.machine.apps or app not in SETTINGS_FILES:
            return
        plan.settings.append(SettingWrite(
            app=app, key=item.data["key"], item_key=item.key, value=item.data["value"],
            mode=item.data.get("mode", "set"), code=is_code_setting(app, item.data["key"]), local=module.local,
        ))
