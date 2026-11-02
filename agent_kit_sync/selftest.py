"""A quick check that a tool version can install and carry back an edit. Run before switching to it."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    from . import render, sections, settings, toml
    from .fsutil import atomic_write

    values = render.Values(home="/h", projects="/h/p", worktrees="/h/p/w", module="/h/m", app="claude", os="linux", cli="x", hooks={})
    source = "Keep code in {{kit.projects}}.\nSecond line.\n"
    rendered = render.render_text(source, values).text
    edited = rendered.replace("Second", "Edited")
    back = render.carry_back_text(source, rendered, edited, values)
    assert back == "Keep code in {{kit.projects}}.\nEdited line.\n", back
    text = sections.apply("# Mine\n", {"m/a": "Rule.\n"}, ["m/a"])
    assert sections.extract(text) == {"m/a": "Rule.\n"}
    assert sections.apply(text, {}, ["m/a"]) == "# Mine\n"
    assert toml.loads(settings.toml_set("# c\n[t]\na = 1\n", "t.b", "x")) == {"t": {"a": 1, "b": "x"}}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "f.txt"
        atomic_write(path, b"one")
        assert path.read_bytes() == b"one"
    from . import dispatch, engine, register  # noqa: F401  (import check)
    print("self-test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
