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
SHOWN = re.compile(r"^\s*<!-- agent-kit-sync (?:only [a-z ]+|end only) -->\s*$")


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
    reverse_skip: Tuple[str, ...] = ()

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
            if value == "" and name == "app":
                raise RenderError("{{kit.app}} has no value in a support file, because one copy serves every app")
            if value == "":
                raise RenderError(f"{{{{kit.{name}}}}} has no value on this machine")
            return value
        raise RenderError(f"unknown placeholder {{{{kit.{name}}}}}")

    def path_pairs(self) -> List[Tuple[str, str]]:
        pairs = [(getattr(self, name), "{{kit." + name + "}}") for name in PATH_NAMES if getattr(self, name) and name not in self.reverse_skip]
        return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def substitute(text: str, values: Values, header: bool = False) -> str:
    """Replace each placeholder in `text`. With `header`, `text` is a line of a --- header block."""
    def value(match) -> str:
        found = values.lookup(match.group(1), match.group(2))
        if header and match.group(1) == "hook" and in_double_quotes(text, match.start()):
            # A hook command in a JSON or YAML double-quoted string, as in a subagent's hooks field: a quote
            # or backslash in it, from a path quoted for the shell, would end the string or start an escape.
            found = escape_double_quoted(found)
        return found

    return PLACEHOLDER.sub(value, text)


def in_double_quotes(text: str, at: int) -> bool:
    """Whether position `at` is inside a double-quoted string, counting quotes not escaped by a backslash."""
    inside = False
    escaped = False
    for char in text[:at]:
        if escaped:
            escaped = False
        elif char == "\\" and inside:
            escaped = True
        elif char == '"':
            inside = not inside
    return inside


def escape_double_quoted(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def placeholders_in(text: str) -> List[str]:
    return [match.group(0) for match in PLACEHOLDER.finditer(text)]


def only_lines(lines: Sequence[str], app: str, line_offset: int = 0) -> Tuple[Set[int], Dict[int, str], Set[int]]:
    """Find the {{kit.only}} blocks for one app's copy.

    Returns the source lines to leave out (every line of a block for other apps, markers
    included), the marker lines of this app's own blocks with the comment line each
    becomes, and every marker line. `line_offset` is added to line numbers in errors.
    """
    hidden: Set[int] = set()
    shown: Dict[int, str] = {}
    markers: Set[int] = set()
    current: Optional[List[str]] = None
    for number, line in enumerate(lines):
        start = ONLY_START.match(line)
        if start:
            if current is not None:
                raise RenderError(f"line {number + 1 + line_offset}: a {{{{kit.only}}}} block starts inside another one")
            current = (start.group(1) or "").split()
            unknown = [name for name in current if name not in APPS]
            if unknown or not current:
                raise RenderError(f"line {number + 1 + line_offset}: {{{{kit.only}}}} must list apps separated by spaces, from {', '.join(APPS)}")
            markers.add(number)
            if app in current:
                shown[number] = "<!-- agent-kit-sync only " + " ".join(current) + " -->"
            else:
                hidden.add(number)
        elif ONLY_END.match(line):
            if current is None:
                raise RenderError(f"line {number + 1 + line_offset}: {{{{kit.end}}}} has no {{{{kit.only}}}} block to end")
            markers.add(number)
            if app in current:
                shown[number] = "<!-- agent-kit-sync end only -->"
            else:
                hidden.add(number)
            current = None
        elif SHOWN.match(line):
            raise RenderError(f"line {number + 1 + line_offset} has the form agent-kit-sync writes around lines for some apps; write {{{{kit.only APP}}}} and {{{{kit.end}}}} instead")
        elif current is not None and app not in current:
            hidden.add(number)
    if current is not None:
        raise RenderError("a {{kit.only}} block has no {{kit.end}} line")
    return hidden, shown, markers


def shown_line(comment: str, source_line: str) -> str:
    """The comment line a marker becomes, with the source line's own line break."""
    body = source_line.splitlines()[0] if source_line.splitlines() else ""
    return comment + source_line[len(body):]


def reverse_paths(line: str, values: Values, header: bool = False) -> str:
    """Turn hook commands and this machine's own folder paths back into placeholders, on whole path prefixes.
    With `header`, `line` is a line of a --- header block, where substitute escapes a command in double quotes."""
    forms = [(command, key) for key, command in values.hooks.items() if command]
    if header:
        forms += [(escape_double_quoted(command), key) for command, key in list(forms) if escape_double_quoted(command) != command]
    for command, key in sorted(forms, key=lambda pair: len(pair[0]), reverse=True):
        pattern = re.compile(r"(?<![\w./\\-])" + re.escape(command) + r"(?=$|[\s\"'`;|&<>)\]]|[.,:](?:\s|$))")
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


HIDDEN_EDGE = ("the edit adds lines next to a block of lines for other apps, or changes how many lines surround it, "
               "so where that block belongs among the new lines is unclear; make this change in the module's file")
NO_LINE_BREAK = "the edited copy has no line break after its last line, and lines for other apps follow it in the module's file; add the line break"


def carry_back(source_lines: Sequence[str], old: Projection, edited_lines: Sequence[str], reverse: Callable[[int, str], str],
               blocks: Collection[int] = ()) -> List[str]:
    """Rebuild source lines so that projecting them again gives the edited lines.

    Lines the edit left alone come back from the source unchanged, placeholders and all.
    `reverse(j, line)` maps an edited line j back to source form. Source lines that the
    projection dropped, such as another app's fields or lines, stay together in place.
    `blocks` are the source lines of {{kit.only}} blocks this copy leaves out. When the
    edit replaces lines around one, each edited line takes its replaced line's place, so
    the block keeps its place among the other lines. When the edit adds lines next to
    one, or changes how many lines surround it, the block's place among the new lines is
    unclear, so the edit is refused.
    """
    index = old.source_index
    mapped = set(index)
    out: List[str] = []
    cursor = 0
    unended = None  # Where the edit's last line went, when it has no line break.

    def keep_unmapped(until: int) -> None:
        nonlocal cursor
        for k in range(cursor, until):
            if k not in mapped:
                out.append(source_lines[k])
        cursor = max(cursor, until)

    def put(j: int) -> None:
        nonlocal unended
        line = edited_lines[j]
        if ONLY_START.match(line) or ONLY_END.match(line) or SHOWN.match(line):
            raise RenderError("the edit adds a {{kit.only}} or {{kit.end}} line, or an agent-kit-sync comment line; make that change in the module's file")
        if j == len(edited_lines) - 1 and line.splitlines() == [line]:
            unended = len(out)
        out.append(reverse(j, line))

    matcher = difflib.SequenceMatcher(None, list(old.lines), list(edited_lines), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for i in range(i1, i2):
                keep_unmapped(index[i])
                out.append(source_lines[index[i]])
                cursor = index[i] + 1
            continue
        if blocks and j2 - j1 > i2 - i1:
            before = index[i1 - 1] if i1 > 0 else -1
            after = index[i2] if i2 < len(index) else len(source_lines)
            if any(before < k < (index[i1] if i1 < i2 else after) or (i1 < i2 and index[i2 - 1] < k < after) for k in blocks):
                raise RenderError(HIDDEN_EDGE)
        if blocks and i1 < i2 and any(index[i1] < k < index[i2 - 1] for k in blocks):
            if i2 - i1 != j2 - j1:
                raise RenderError(HIDDEN_EDGE)
            # Line for line: each edited line takes the place of the line it replaced.
            for i, j in zip(range(i1, i2), range(j1, j2)):
                keep_unmapped(index[i])
                put(j)
                cursor = index[i] + 1
            continue
        if i1 < i2:
            keep_unmapped(index[i1])
        for j in range(j1, j2):
            put(j)
        if i1 < i2:
            last = index[i2 - 1]
            for k in range(index[i1], last + 1):
                if k not in mapped:
                    out.append(source_lines[k])
            cursor = last + 1
    keep_unmapped(len(source_lines))
    if unended is not None and unended != len(out) - 1:
        raise RenderError(NO_LINE_BREAK)
    return out


def _text_of(line: str) -> str:
    """A line without its line break."""
    parts = line.splitlines()
    return parts[0] if parts else ""


def carry_back_blocks(source_lines: Sequence[str], old: Projection, edited_lines: Sequence[str], reverse: Callable[[int, str], str],
                      shown: Dict[int, str], blocks: Collection[int]) -> List[str]:
    """Carry an edit back between the {{kit.only}} comment lines this app's copy shows.

    The comment lines mark exactly where each block for this app starts and ends, so each
    stretch between them is carried back on its own, and a line stays on the side of a
    comment line where the user put it. Blocks for other apps are not in this copy, and
    stay together in place. An edit that changes the comment lines, or moves a line from
    one side of a comment line to the other, is refused.
    """
    cuts_old = [i for i, k in enumerate(old.source_index) if k in shown]
    if not cuts_old:
        return carry_back(source_lines, old, edited_lines, reverse, blocks)
    cuts_new = [j for j, line in enumerate(edited_lines) if SHOWN.match(line)]
    if [_text_of(old.lines[i]) for i in cuts_old] != [_text_of(edited_lines[j]) for j in cuts_new]:
        raise RenderError("the edit adds, removes, or changes an agent-kit-sync comment line around lines for some apps; make that change in the module's file")
    src_cuts = [old.source_index[i] for i in cuts_old]
    bounds_old, bounds_new, bounds_src = [-1] + cuts_old + [len(old.lines)], [-1] + cuts_new + [len(edited_lines)], [-1] + src_cuts + [len(source_lines)]
    out: List[str] = []
    removed: Dict[str, Set[int]] = {}
    added: Dict[str, Set[int]] = {}
    for piece in range(len(bounds_old) - 1):
        o1, o2 = bounds_old[piece] + 1, bounds_old[piece + 1]
        n1, n2 = bounds_new[piece] + 1, bounds_new[piece + 1]
        s1, s2 = bounds_src[piece] + 1, bounds_src[piece + 1]
        old_piece = Projection(list(old.lines[o1:o2]), [k - s1 for k in old.source_index[o1:o2]])
        new_piece = list(edited_lines[n1:n2])
        piece_blocks = [k - s1 for k in blocks if s1 <= k < s2]
        out += carry_back(source_lines[s1:s2], old_piece, new_piece, lambda j, line, n1=n1: reverse(n1 + j, line), piece_blocks)
        if piece < len(src_cuts):
            edited_cut = edited_lines[cuts_new[piece]]
            out.append(_text_of(source_lines[src_cuts[piece]]) + edited_cut[len(_text_of(edited_cut)):])
        # Comment lines come in start and end pairs, so odd stretches are inside a block.
        region = piece if piece % 2 else -1
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_piece.lines, new_piece, autojunk=False).get_opcodes():
            if tag != "equal":
                for line in old_piece.lines[i1:i2]:
                    removed.setdefault(line.strip(), set()).add(region)
                for line in new_piece[j1:j2]:
                    added.setdefault(line.strip(), set()).add(region)
    for text, regions in added.items():
        if text and removed.get(text, set()) - regions:
            raise RenderError("the edit moves a line into or out of a block of lines for some apps, which would change what other apps see; make this change in the module's file")
    if any(line.splitlines() == [line] for line in out[:-1]):
        raise RenderError(NO_LINE_BREAK)
    return out


# ---------------------------------------------------------------- plain templates


def render_text(source: str, values: Values, line_offset: int = 0, header: bool = False) -> Projection:
    """Render one text file. With `header`, the file may start with a --- header block, which cannot hold a {{kit.only}} block."""
    lines = source.splitlines(keepends=True)
    hidden, shown, markers = only_lines(lines, values.app, line_offset)
    if header:
        _refuse_header_blocks(source, markers)

    def rule(number: int, line: str) -> Optional[str]:
        if number in hidden:
            return None
        if number in shown:
            return shown_line(shown[number], line)
        return substitute(line, values)

    return project(lines, rule)


def carry_back_text(source: str, old_render: str, edited: str, values: Values, line_offset: int = 0, header: bool = False) -> str:
    old = render_text(source, values, line_offset, header)
    if old.text != old_render:
        raise RenderError("the source no longer renders to the copy that was written, so the edit cannot be carried back exactly")
    source_lines = source.splitlines(keepends=True)
    hidden, shown, _markers = only_lines(source_lines, values.app, line_offset)
    lines = carry_back_blocks(source_lines, old, edited.splitlines(keepends=True), lambda _j, line: reverse_paths(line, values), shown, hidden)
    return "".join(lines)


def _refuse_header_blocks(source: str, markers: Collection[int]) -> None:
    if not markers:
        return
    _lines, start, end = split_frontmatter(source)
    if start >= 0 and any(number <= end for number in markers):
        raise RenderError("a {{kit.only}} block is in the header block; write a field for one app with its name in front, such as claude.tools")


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


def _md_rule(app: str, values: Values, lines: Sequence[str]) -> Tuple[LineRule, Dict[str, str], Dict[int, str]]:
    _, start, end = split_frontmatter("".join(lines))
    blocks, shown, markers = only_lines(lines, app)
    _refuse_header_blocks("".join(lines), markers)
    drop = set(blocks)
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
        if number in shown:
            return shown_line(shown[number], line)
        in_header = start >= 0 and start < number < end
        if in_header:
            match = FRONT_KEY.match(line)
            if match and match.group(1).startswith(own):
                line = match.group(1)[len(own):] + ":" + match.group(2) + line[match.end():]
        return substitute(line, values, header=in_header)

    return rule, renamed, shown, blocks


def render_agent_md(source: str, app: str, values: Values) -> Projection:
    lines = source.splitlines(keepends=True)
    rule, _renamed, _shown, _blocks = _md_rule(app, values, lines)
    return project(lines, rule)


def carry_back_agent_md(source: str, app: str, old_render: str, edited: str, values: Values) -> str:
    lines = source.splitlines(keepends=True)
    rule, renamed, shown, blocks = _md_rule(app, values, lines)
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
        line = reverse_paths(line, values, header=e_start >= 0 and e_start < j < e_end)
        if e_start >= 0 and e_start < j < e_end:
            match = FRONT_KEY.match(line)
            if match:
                key = match.group(1)
                if key in renamed:
                    line = renamed[key] + ":" + match.group(2) + line[match.end():]
                elif key not in plain_keys and key not in SHARED_AGENT_KEYS and not key.startswith(APP_PREFIXES):
                    line = app + "." + key + ":" + match.group(2) + line[match.end():]
        return line

    return "".join(carry_back_blocks(lines, old, edited_lines, reverse, shown, blocks))


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
                value = yaml_scalar(substitute(FRONT_KEY.match(lines[first]).group(2), values, header=True))
                fields[name] = value
        body = "".join(lines[end + 1:])
    else:
        body = source
    return fields, render_text(body, values, end + 1 if start >= 0 else 0).text


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
    rendered_body = render_text(source_body, values, end + 1).text
    if rendered_body != old_body and rendered_body.replace("\r\n", "\n") == old_body:
        # TOML readers turn CRLF line breaks in a multi-line string into LF, so a source
        # with CRLF line breaks is compared, and carried back, with its own line breaks.
        old_body = rendered_body
        new_body = new_body.replace("\r\n", "\n").replace("\n", "\r\n")
    body = source_body if old_body == new_body else carry_back_text(source_body, old_body, new_body, values, end + 1)
    return "".join(header) + lines[end] + body
