"""The inventory, modules, and machine file, read from TOML."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import FORMAT, toml
from .paths import APPS, Layout, os_name

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ITEM_KINDS = ("sections", "skills", "agents", "hooks", "settings", "files")
DEFAULT_IGNORE = (
    ".DS_Store", "Thumbs.db", "desktop.ini", "__pycache__", "*.pyc", ".pytest_cache",
    "node_modules", ".git", "*~", "*.swp",
)
POLICY_DEFAULTS = {
    "new_items": "ask",
    "code_changes": "ask",
    "other_changes": "apply",
    "outgoing": "ask-code",
    "tool_updates": "apply",
    "approval": "agent",
}
POLICY_VALUES = {
    "new_items": ("apply", "ask"),
    "code_changes": ("apply", "ask"),
    "other_changes": ("apply", "ask"),
    "outgoing": ("push", "ask-code", "ask"),
    "tool_updates": ("apply", "ask", "pin"),
    "approval": ("agent", "terminal"),
}
OVERRIDE_KINDS = ("disable", "replace", "prepend", "append", "pin")


class ConfigError(Exception):
    """A config repo, module, or machine file that cannot be used as written."""


def check_name(value, what: str) -> str:
    if not isinstance(value, str) or not NAME.match(value):
        raise ConfigError(f"{what} {value!r} must be lowercase letters, digits, and single hyphens")
    return value


@dataclass
class Item:
    module: str
    kind: str          # section, skill, agent, hook, setting, file
    name: str
    data: dict

    @property
    def key(self) -> str:
        return f"{self.module}/{self.name}"


@dataclass
class Module:
    name: str
    root: Path
    local: bool
    description: str = ""
    apps: List[str] = field(default_factory=lambda: list(APPS))
    requires: dict = field(default_factory=dict)
    ignore: List[str] = field(default_factory=list)
    items: List[Item] = field(default_factory=list)
    source: Optional[str] = None

    def of(self, kind: str) -> List[Item]:
        return [item for item in self.items if item.kind == kind]


@dataclass
class Inventory:
    root: Path
    format: int
    defaults: dict
    modules: List[Module]
    sources: dict


def _item_name(kind: str, entry: dict) -> str:
    if kind == "settings":
        return check_name(entry.get("name") or re.sub(r"[^a-z0-9]+", "-", f"{entry.get('app', '')}-{entry.get('key', '')}".lower()).strip("-"), "setting name")
    if kind == "files":
        return check_name(entry.get("name") or re.sub(r"[^a-z0-9]+", "-", str(entry.get("path", "")).lower()).strip("-"), "file name")
    return check_name(entry.get("name"), kind[:-1] + " name")


def load_module(root: Path, name: str, local: bool, source: Optional[str] = None, tree=None) -> Module:
    """Read a module. `tree`, when given, reads its files from a commit instead of disk."""
    check_name(name, "module name")
    path = root / "module.toml"
    raw = tree.read("module.toml") if tree is not None else (path.read_bytes() if path.is_file() else None)
    if raw is None:
        raise ConfigError(f"module {name} has no module.toml at {path}")
    try:
        data = toml.loads(raw.decode("utf-8"))
    except (toml.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from None
    module = Module(
        name=name,
        root=root,
        local=local,
        description=str(data.get("description", "")),
        apps=list(data.get("apps", APPS)),
        requires=dict(data.get("requires", {})),
        ignore=list(data.get("ignore", [])),
        source=source,
    )
    for app in module.apps:
        if app not in APPS:
            raise ConfigError(f"module {name}: unknown app {app!r}")
    seen = set()
    for kind in ITEM_KINDS:
        entries = data.get(kind, [])
        if not isinstance(entries, list):
            raise ConfigError(f"module {name}: [[{kind}]] must be a list of tables")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigError(f"module {name}: each [[{kind}]] entry must be a table")
            item = Item(module=name, kind=kind[:-1], name=_item_name(kind, entry), data=entry)
            folded = (item.kind, item.name.lower())
            if folded in seen:
                raise ConfigError(f"module {name}: two {kind} named {item.name}")
            seen.add(folded)
            module.items.append(item)
    _check_files(module, tree)
    return module


def safe_rel(rel) -> bool:
    if not isinstance(rel, str) or not rel or "\0" in rel:
        return False
    parts = rel.replace("\\", "/").split("/")
    return not (rel.startswith(("/", "\\")) or ".." in parts or ":" in rel or rel.startswith("~"))


def _exists(module: Module, tree, rel: str) -> bool:
    if tree is None:
        return (module.root / rel).exists()
    return tree.read(rel) is not None or bool(tree.files(rel))


def _check_files(module: Module, tree=None) -> None:
    for item in module.items:
        rel = None
        if item.kind in ("section", "agent", "hook"):
            rel = item.data.get("file")
        elif item.kind == "skill":
            rel = item.data.get("dir")
        elif item.kind == "file":
            rel = item.data.get("path")
        if item.kind == "setting":
            for required in ("app", "key", "value"):
                if required not in item.data:
                    raise ConfigError(f"module {module.name}: setting {item.name} has no {required}")
            continue
        if not isinstance(rel, str) or not rel:
            raise ConfigError(f"module {module.name}: {item.kind} {item.name} has no file")
        templates = item.data.get("templates", [])
        if not (isinstance(templates, list) or (templates is True and item.kind == "file")):
            raise ConfigError(f"module {module.name}: {item.kind} {item.name} has templates that are neither a list of paths nor, for a single file, true")
        extra = list(dict(item.data.get("variants", {})).values()) + list(item.data.get("tests", [])) + (list(templates) if isinstance(templates, list) else [])
        if templates is True and (tree.files(rel) if tree is not None else (module.root / rel).is_dir()):
            raise ConfigError(f"module {module.name}: file {item.name} sets templates = true, but {rel} is a folder; list the files to fill in instead")
        if isinstance(templates, list) and item.kind in ("skill", "file"):
            for template in templates:
                if isinstance(template, str) and safe_rel(template) and not _exists(module, tree, f"{rel.rstrip('/')}/{template}"):
                    raise ConfigError(f"module {module.name}: {item.kind} {item.name} lists template {template}, which is not in {rel}")
        if not safe_rel(rel) or not all(safe_rel(path) for path in extra):
            raise ConfigError(f"module {module.name}: {item.kind} {item.name} lists a path outside the module")
        if not _exists(module, tree, rel):
            raise ConfigError(f"module {module.name}: {item.kind} {item.name} lists {rel}, which does not exist")
        for app, variant in dict(item.data.get("variants", {})).items():
            if not _exists(module, tree, variant):
                raise ConfigError(f"module {module.name}: section {item.name} variant for {app} lists {variant}, which does not exist")


def load_inventory(root: Path) -> Inventory:
    path = root / "kit.toml"
    if not path.is_file():
        raise ConfigError(f"no kit.toml in {root}")
    try:
        data = toml.load_file(path)
    except toml.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None
    fmt = int(data.get("format", 1))
    if fmt > FORMAT:
        raise ConfigError(f"kit.toml uses format {fmt}; this agent-kit-sync reads format {FORMAT} and must update first")
    modules = []
    names = set()
    for entry in data.get("modules", []):
        name = check_name(entry.get("name"), "module name")
        if name in names:
            raise ConfigError(f"kit.toml lists module {name} twice")
        names.add(name)
        if entry.get("source"):
            continue  # Modules from sources are installed by a later version.
        modules.append(load_module(root / "modules" / name, name, local=False))
    return Inventory(root=root, format=fmt, defaults=dict(data.get("defaults", {})), modules=modules, sources=dict(data.get("sources", {})))


def load_local_modules(layout: Layout) -> List[Module]:
    if not layout.local_modules.is_dir():
        return []
    return [load_module(path, path.name, local=True) for path in sorted(layout.local_modules.iterdir()) if path.is_dir()]


@dataclass
class Machine:
    name: str
    apps: List[str]
    app_homes: Dict[str, Path]
    tags: List[str]
    python: str
    projects: Optional[Path]
    worktrees: Optional[Path]
    git_identity: dict
    policy: dict
    overrides: dict
    cloud: bool = False
    # Folders the machine file leaves out and that therefore come from the defaults, such as in a cloud session.
    defaulted: List[str] = field(default_factory=list)

    @property
    def os(self) -> str:
        return os_name()


def default_folders(layout: Layout) -> Dict[str, Path]:
    """The projects and worktrees folders for a machine whose machine file sets none.

    The names come from `defaults` in the config repo's kit.toml, as setup recommends them:
    `projects_folder_name` in the home folder, and `worktrees_folder_name` inside it. The
    folders need not exist; hooks that use them must handle their absence.
    """
    defaults: dict = {}
    try:
        defaults = dict(toml.load_file(layout.config / "kit.toml").get("defaults", {}))
    except (OSError, toml.TOMLDecodeError, ValueError):
        pass
    def name(value, fallback: str) -> str:
        if isinstance(value, dict):
            value = value.get(os_name())
        text = str(value).strip() if isinstance(value, str) else ""
        return text if text and "/" not in text and "\\" not in text and text not in (".", "..") else fallback
    projects = Path.home() / name(defaults.get("projects_folder_name"), "Projects")
    worktrees = projects / name(defaults.get("worktrees_folder_name"), ".worktrees" if os_name() == "windows" else "_worktrees")
    return {"projects": projects, "worktrees": worktrees}


def load_machine(layout: Layout) -> Machine:
    path = layout.machine_file
    if not path.is_file():
        raise ConfigError(f"no machine file at {path}; run setup first")
    try:
        data = toml.load_file(path)
    except toml.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None
    policy = dict(POLICY_DEFAULTS)
    policy.update(data.get("policy", {}))
    for key, value in policy.items():
        if key in POLICY_VALUES and value not in POLICY_VALUES[key]:
            raise ConfigError(f"{path}: policy {key} must be one of {', '.join(POLICY_VALUES[key])}")
    overrides = dict(data.get("overrides", {}))
    for target, override in overrides.items():
        if override.get("kind") not in OVERRIDE_KINDS:
            raise ConfigError(f"{path}: override for {target} must have kind {', '.join(OVERRIDE_KINDS)}")
        if override.get("kind") in ("replace", "prepend", "append"):
            local_file = Path(str(override.get("file", "")))
            if not local_file.is_absolute():
                local_file = layout.root / local_file
            if not local_file.is_file():
                raise ConfigError(f"{path}: the {override['kind']} override for {target} lists {local_file}, which does not exist")
    apps = list(data.get("apps", []))
    homes = {app: Path(value) for app, value in dict(data.get("app_homes", {})).items()}
    for app in apps:
        if app not in APPS:
            raise ConfigError(f"{path}: unknown app {app!r}")
        if app not in homes:
            from .paths import default_app_home
            homes[app] = default_app_home(app)
    defaulted = [folder for folder in ("projects", "worktrees") if not data.get(folder)]
    fallback = default_folders(layout) if defaulted else {}
    return Machine(
        name=str(data.get("name", "machine")),
        apps=apps,
        app_homes=homes,
        tags=list(data.get("tags", [])),
        python=str(data.get("python", "")),
        projects=Path(data["projects"]) if data.get("projects") else fallback.get("projects"),
        worktrees=Path(data["worktrees"]) if data.get("worktrees") else fallback.get("worktrees"),
        git_identity=dict(data.get("git_identity", {})),
        policy=policy,
        overrides=overrides,
        cloud=bool(data.get("cloud", False)),
        defaulted=defaulted,
    )


def module_applies(module: Module, machine: Machine, runtimes: Dict[str, bool]) -> Optional[str]:
    """Return None when the module applies here, else the reason it is skipped."""
    req = module.requires
    systems = req.get("os")
    if systems and machine.os not in systems:
        return f"needs {', '.join(systems)}"
    missing_tags = [tag for tag in req.get("tags", []) if tag not in machine.tags]
    if missing_tags:
        return f"needs tag {', '.join(missing_tags)}"
    missing_runtimes = [name for name in req.get("runtimes", []) if not runtimes.get(name)]
    if missing_runtimes:
        return f"needs {', '.join(missing_runtimes)}"
    missing_apps = [app for app in req.get("apps", []) if app not in machine.apps]
    if missing_apps:
        return f"needs {', '.join(missing_apps)}"
    if not any(app in machine.apps for app in module.apps):
        return "targets no app on this machine"
    return None
