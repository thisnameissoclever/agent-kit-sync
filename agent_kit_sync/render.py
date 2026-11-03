"""Placeholders, rendering an item for one app, and carrying an edit back to its source.

Rendering never adds or removes a line break, so line i of a rendered copy comes from
a known line of the source. Carrying an edit back relies on that: unchanged lines keep
the source's placeholders, and only changed lines are mapped back.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Callable, Collection, Dict, List, Optional, Sequence, Set, Tuple

from . import toml
from .paths import APPS

PLACEHOLDER = re.compile(r"\{\{kit\.([a-z]+)(?: ([^{}\n]+?))?\}\}")
PATH_NAMES = ("projects", "worktrees", "module", "home")
APP_PREFIXES = ("claude.", "codex.", "cursor.", "antigravity.")
FRONT_KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(.*)$")
SHARED_AGENT_KEYS = ("name", "description")
ONLY_START = re.compile(r"^\s*\{\{kit\.only(?: ([^{}\n]*))?\}\}\s*$")
ONLY_END = re.compile(r"^\s*\{\{kit\.end\}\}\s*$")


class RenderError(Exception):
    """A source that cannot be rendered, such as one with an unknown placeholder."""


@dataclass
class Values:
    """This machine's value for each placeholder."""

    home: str
    projects: str
    worktrees: str
    module: str
    app: str
    os: str
    cli: str
    hooks: Dict[str, str]

    def lookup(self, name: str, arg: Optional[str]) -> str:
        if name == "literal":
            if not arg or not re.fullmatch(r"[a-z]+(?: [^{}\n]+)?", arg):
                raise RenderError("{{kit.literal}} needs a placeholder name")
            return "{{kit." + arg + "}}"
        if name == "hook":
            if not arg or arg not in self.hooks:
                raise RenderError(f"{{{{kit.hook {arg}}}}} names no hook in the inventory")
            return self.hooks[arg]
        if name in ("only", "end"):
            raise RenderError("{{kit.only APP}} and {{kit.end}} must each be alone on a line")
        if arg is not None:
            raise RenderError(f"{{{{kit.{name}}}}} takes no argument")
        if name in ("home", "projects", "worktrees", "module", "app", "os", "cli"):
            value = getattr(self, name)
            if value == "":
                raise RenderError(f"{{{{kit.{name}}}}} has no value on this machine")
            return value
        raise RenderError(f"unknown placeholder {{{{kit.{name}}}}}")

    def path_pairs(self) -> List[Tuple[str, str]]:
        pairs = [(getattr(self, name), "{{kit." + name + "}}") for name in PATH_NAMES if getattr(self, name)]
        return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def substitute(text: str, values: Values) -> str:
    return PLACEHOLDER.sub(lambda match: values.lookup(match.group(1), match.group(2)), text)


def placeholders_in(text: str) -> List[str]:
    return [match.group(0) for match in PLACEHOLDER.finditer(text)]


def only_lines(lines: Sequence[str], app: str) -> Tuple[Set[int], Set[int]]:
    """Lines to leave out of `app`'s copy, and the {{kit.only}} and {{kit.end}} marker lines among them."""
    hidden: Set[int] = set()
    markers: Set[int] = set()
    current: Optional[List[str]] = None
    for number, line in enumerate(lines):
        start = ONLY_START.match(line)
        if start:
            if current is not None:
                raise RenderError(f"line {number + 1}: a {{{{kit.only}}}} block starts inside another one")
            current = (start.group(1) or "").split()
            unknown = [name for name in current if name not in APPS]
            if unknown or not current:
                raise RenderError(f"line {number + 1}: {{{{kit.only}}}} must list apps separated by spaces, from {', '.join(APPS)}")
            markers.add(number)
        elif ONLY_END.match(line):
            if current is None:
                raise RenderError(f"line {number + 1}: {{{{kit.end}}}} has no {{{{kit.only}}}} block to end")
            current = None
            markers.add(number)
        elif current is not None and app not in current:
            hidden.add(number)
    if current is not None:
        raise RenderError("a {{kit.only}} block has no {{kit.end}} line")
    return hidden | markers, markers


def reverse_paths(line: str, values: Values) -> str:
    """Turn hook commands and this machine's own folder paths back into placeholders, on whole path prefixes."""
    for key, command in sorted(values.hooks.items(), key=lambda pair: len(pair[1]), reverse=True):
        if command:
            pattern = re.compile(r"(?<![\w./\\-])" + re.escape(command) + r"(?=$|[\s\"'`;|&<>)\]])")
            line = pattern.sub(lambda _match, key=key: "{{kit.hook " + key + "}}", line)
    for value, token in values.path_pairs():
        pattern = re.compile(r"(?<![\w.\-/\\~])" + re.escape(value) + r"(?=$|[/\\\s\"'`),;:\]>}]|\.(?:\s|$))")
        line = pattern.sub(lambda _match: token, line)
    return line


# ---------------------------------------------------------------- line projections


@dataclass
class Projection:
    """A rendered text plus, for each rendered line, the source line it came from."""

    lines: List[str]
    source_index: List[int]

    @property
    def text(self) -> str:
        return "".join(self.lines)


LineRule = Callable[[int, str], Optional[str]]


def project(source_lines: Sequence[str], rule: LineRule) -> Projection:
    lines, index = [], []
    for number, line in enumerate(source_lines):
        out = rule(number, line)
        if out is None:
            continue
        if out.count("\n") != line.count("\n"):
            raise RenderError("a placeholder value contains a line break")
        lines.append(out)
        index.append(number)
    return Projection(lines, index)


def carry_back(source_lines: Sequence[str], old: Projection, edited_lines: Sequence[str], reverse: Callable[[int, str], str],
               markers: Collection[int] = ()) -> List[str]:
    """Rebuild source lines so that projecting them again gives the edited lines.

    Lines the edit left alone come back from the source unchanged, placeholders and all.
    `reverse(j, line)` maps an edited line j back to source form. Source lines that the
    projection dropped, such as another app's fields, stay in place. `markers` are the
    {{kit.only}} and {{kit.end}} lines: an edit that adds lines next to one of them could
    belong inside the block or outside it, so it raises RenderError instead of guessing.
    """
    mapped = set(old.source_index)
    out: List[str] = []
    cursor = 0

    def keep_unmapped(until: int) -> None:
        nonlocal cursor
        for k in range(cursor, until):
            if k not in mapped:
                out.append(source_lines[k])
        cursor = max(cursor, until)

    opcodes = difflib.SequenceMatcher(None, list(old.lines), list(edited_lines), autojunk=False).get_opcodes()
    for number, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            for i in range(i1, i2):
                keep_unmapped(old.source_index[i])
                out.append(source_lines[old.source_index[i]])
                cursor = old.source_index[i] + 1
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            # Line for line: each edited line takes the place of the line it replaced.
            for i, j in zip(range(i1, i2), range(j1, j2)):
                keep_unmapped(old.source_index[i])
                out.append(reverse(j, edited_lines[j]))
                cursor = old.source_index[i] + 1
            continue
        if markers and _unclear_at_marker(opcodes, number, old, edited_lines, markers, len(source_lines)):
            raise RenderError("the edit adds or removes lines at the edge of a {{kit.only}} block, so which apps they are for is unclear; make this change in the module's file")
        if i1 < i2:
            keep_unmapped(old.source_index[i1])
        for j in range(j1, j2):
            out.append(reverse(j, edited_lines[j]))
        if i1 < i2:
            last = old.source_index[i2 - 1]
            for k in range(old.source_index[i1], last + 1):
                if k not in mapped:
                    out.append(source_lines[k])
            cursor = last + 1
    keep_unmapped(len(source_lines))
    if any(not line.endswith("\n") for line in out[:-1]):
        raise RenderError("the edited copy has no line break after its last line, and lines for other apps follow it in the module's file; add the line break")
    return out


def _unclear_at_marker(opcodes, number: int, old: Projection, edited_lines: Sequence[str], markers: Collection[int], total: int) -> bool:
    """Whether a changed run of lines could equally sit on either side of a {{kit.only}} or {{kit.end}} line.

    Lines added or removed next to a line with the same text can be matched one line
    earlier or later with the same result, so the run is widened over such lines first.
    """
    tag, i1, i2, j1, j2 = opcodes[number]
    index = old.source_index
    lowest = opcodes[number - 1][1] if number > 0 and opcodes[number - 1][0] == "equal" else i1
    highest = opcodes[number + 1][2] if number + 1 < len(opcodes) and opcodes[number + 1][0] == "equal" else i2
    moved = list(edited_lines[j1:j2]) if tag == "insert" else list(old.lines[i1:i2]) if tag == "delete" else []
    left = right = 0
    if moved:
        size = len(moved)
        while i1 - left - 1 >= lowest and old.lines[i1 - left - 1] == moved[(size - 1 - left) % size]:
            left += 1
        while i2 + right < highest and old.lines[i2 + right] == moved[right % size]:
            right += 1
    first, stop = i1 - left, i2 + right
    if tag == "delete":
        if not (left or right):
            return False
        low, high = index[first], index[stop - 1]
    else:
        low = index[first - 1] if first > 0 else -1
        high = index[stop] if stop < len(index) else total
    return any(low < k < high for k in markers)


# ---------------------------------------------------------------- plain templates


def render_text(source: str, values: Values) -> Projection:
    lines = source.splitlines(keepends=True)
    hidden, _markers = only_lines(lines, values.app)
    return project(lines, lambda number, line: None if number in hidden else substitute(line, values))


def carry_back_text(source: str, old_render: str, edited: str, values: Values) -> str:
    old = render_text(source, values)
    if old.text != old_render:
        raise RenderError("the source no longer renders to the copy that was written, so the edit cannot be carried back exactly")
    source_lines = source.splitlines(keepends=True)
    _hidden, markers = only_lines(source_lines, values.app)
    lines = carry_back(source_lines, old, edited.splitlines(keepends=True), lambda _j, line: reverse_paths(line, values), markers)
    return "".join(lines)


# ---------------------------------------------------------------- subagent definitions


def split_frontmatter(text: str) -> Tuple[List[str], int, int]:
    """Return the lines, and the indexes of the opening and closing --- lines, or -1."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return lines, -1, -1
    for number in range(1, len(lines)):
        if lines[number].rstrip("\r\n") == "---":
            return lines, 0, number
    raise RenderError("a subagent definition opens a --- header block without closing it")


def front_entries(lines: Sequence[str], start: int, end: int) -> List[Tuple[str, int, int]]:
    """Top-level header fields as (key, first line, line after the last) in source order."""
    entries: List[Tuple[str, int, int]] = []
    for number in range(start + 1, end):
        match = FRONT_KEY.match(lines[number])
        if match:
            if entries:
                key, first, _ = entries[-1]
                entries[-1] = (key, first, number)
            entries.append((match.group(1), number, end))
    return entries


def _md_rule(app: str, values: Values, lines: Sequence[str]) -> Tuple[LineRule, Dict[str, str], Set[int]]:
    _, start, end = split_frontmatter("".join(lines))
    drop, markers = only_lines(lines, app)
    if start >= 0 and any(number <= end for number in markers):
        raise RenderError("a {{kit.only}} block is in the header block; write a field for one app with its name in front, such as claude.tools")
    renamed: Dict[str, str] = {}
    own = app + "."
    if start >= 0:
        for key, first, after in front_entries(lines, start, end):
            if key.startswith(APP_PREFIXES) and not key.startswith(own):
                drop.update(range(first, after))
            elif key.startswith(own):
                renamed[key[len(own):]] = key

    def rule(number: int, line: str) -> Optional[str]:
        if number in drop:
            return None
        if start >= 0 and start < number < end:
            match = FRONT_KEY.match(line)
            if match and match.group(1).startswith(own):
                line = match.group(1)[len(own):] + ":" + match.group(2) + line[match.end():]
        return substitute(line, values)

    return rule, renamed, markers


def render_agent_md(source: str, app: str, values: Values) -> Projection:
    lines = source.splitlines(keepends=True)
    rule, _renamed, _markers = _md_rule(app, values, lines)
    return project(lines, rule)


def carry_back_agent_md(source: str, app: str, old_render: str, edited: str, values: Values) -> str:
    lines = source.splitlines(keepends=True)
    rule, renamed, markers = _md_rule(app, values, lines)
    old = project(lines, rule)
    if old.text != old_render:
        raise RenderError("the subagent definition changed since this copy was written")
    plain_keys = set()
    _, start, end = split_frontmatter(source)
    if start >= 0:
        plain_keys = {key for key, _f, _a in front_entries(lines, start, end) if not key.startswith(APP_PREFIXES)}
    edited_lines = edited.splitlines(keepends=True)
    _, e_start, e_end = split_frontmatter(edited)

    def reverse(j: int, line: str) -> str:
        line = reverse_paths(line, values)
        if e_start >= 0 and e_start < j < e_end:
            match = FRONT_KEY.match(line)
            if match:
                key = match.group(1)
                if key in renamed:
                    line = renamed[key] + ":" + match.group(2) + line[match.end():]
                elif key not in plain_keys and key not in SHARED_AGENT_KEYS and not key.startswith(APP_PREFIXES):
                    line = app + "." + key + ":" + match.group(2) + line[match.end():]
        return line

    return "".join(carry_back(lines, old, edited_lines, reverse, markers))


def yaml_scalar(raw: str):
    """Read a one-line YAML value: quoted or plain string, boolean, or number."""
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        body = raw[1:-1]
        return re.sub(r'\\(["\\/nt])', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def yaml_quote(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if text and re.fullmatch(r"[A-Za-z0-9_./][A-Za-z0-9 _./,()'-]*", text) and text not in ("true", "false") and not text.endswith(" "):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_fields(source: str, values: Values) -> Tuple[Dict[str, object], str]:
    lines = source.splitlines(keepends=True)
    _, start, end = split_frontmatter(source)
    fields: Dict[str, object] = {}
    if start >= 0 and any(ONLY_START.match(line) or ONLY_END.match(line) for line in lines[start:end + 1]):
        raise RenderError("a {{kit.only}} block is in the header block; write a field for one app with its name in front, such as codex.sandbox_mode")
    if start >= 0:
        for key, first, after in front_entries(lines, start, end):
            if key in SHARED_AGENT_KEYS or key.startswith("codex."):
                if any(lines[k].strip() for k in range(first + 1, after)):
                    raise RenderError(f"field {key} must fit on one line to reach Codex")
                name = key[len("codex."):] if key.startswith("codex.") else key
                value = yaml_scalar(substitute(FRONT_KEY.match(lines[first]).group(2), values))
                fields[name] = value
        body = "".join(lines[end + 1:])
    else:
        body = source
    return fields, render_text(body, values).text


def toml_multiline(text: str) -> str:
    if "'''" not in text:
        return "'''\n" + text + "'''"
    escaped = text.replace("\\", "\\\\").replace('"""', '""\\"')
    return '"""\n' + escaped + '"""'


def render_agent_codex(source: str, values: Values) -> str:
    fields, body = codex_fields(source, values)
    if "name" not in fields:
        raise RenderError("a subagent definition needs a name field")
    out = []
    for key, value in fields.items():
        out.append(f"{toml.key(key)} = {toml.value(value)}\n")
    out.append("developer_instructions = " + toml_multiline(body) + "\n")
    return "".join(out)


def carry_back_agent_codex(source: str, old_render: str, edited: str, values: Values) -> str:
    """Map an edit of the Codex TOML copy back to the Markdown source."""
    try:
        new = toml.loads(edited)
        old = toml.loads(old_render)
    except toml.TOMLDecodeError as error:
        raise RenderError(f"the edited Codex subagent file is not valid TOML: {error}") from None
    if old != toml.loads(render_agent_codex(source, values)):
        raise RenderError("the subagent definition changed since this copy was written")
    lines = source.splitlines(keepends=True)
    _, start, end = split_frontmatter(source)
    if start < 0:
        raise RenderError("a subagent definition needs a --- header block")
    entries = front_entries(lines, start, end)
    by_field = {}
    for key, first, after in entries:
        if key in SHARED_AGENT_KEYS:
            by_field[key] = (key, first)
        elif key.startswith("codex."):
            by_field[key[len("codex."):]] = (key, first)
    header = lines[: end]
    removals = set()
    additions = []
    for field_name in set(old) | set(new):
        if field_name == "developer_instructions":
            continue
        if old.get(field_name) == new.get(field_name):
            continue
        if field_name in new and not isinstance(new[field_name], (str, int, float, bool)):
            raise RenderError(f"the Codex field {field_name} is not a single value; agent-kit-sync syncs only text, numbers, and true or false")
        if field_name in by_field:
            key, first = by_field[field_name]
            if field_name not in new:
                removals.add(first)
            else:
                header[first] = f"{key}: {yaml_quote(new[field_name])}\n"
        elif field_name in new:
            source_key = field_name if field_name in SHARED_AGENT_KEYS else "codex." + field_name
            additions.append(f"{source_key}: {yaml_quote(new[field_name])}\n")
    header = [line for number, line in enumerate(header) if number not in removals] + additions
    old_body = str(old.get("developer_instructions", ""))
    new_body = str(new.get("developer_instructions", ""))
    source_body = "".join(lines[end + 1:])
    body = source_body if old_body == new_body else carry_back_text(source_body, old_body, new_body, values)
    return "".join(header) + lines[end] + body
