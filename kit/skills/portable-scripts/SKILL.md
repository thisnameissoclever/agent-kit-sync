---
name: portable-scripts
description: Check a hook or script for anything that works on only one machine, offer a rewrite that works on macOS, Windows, and Linux, and add tests. Use before adding a hook or script to agent-kit-sync, and whenever writing a hook or script that should run on more than one machine.
---

# Make a hook or script work on any machine

A synced hook runs on every machine the user has. Something that works on one machine, such as a fixed path, breaks on the others, often silently.

## Check

Read the script and list every problem of these kinds, with the line:

| Problem | Example | Works everywhere |
|---|---|---|
| Fixed path to the user's files | `/Users/name/.claude/hooks/x.py`, `C:\Users\name` | `Path.home() / ".claude"`, or a path relative to the script: `Path(__file__).parent` |
| Fixed interpreter path | `/opt/homebrew/bin/python3` | Let the dispatcher start it; inside a script, `sys.executable` |
| Drive letters or mount points | `D:\Projects`, `/Volumes/Work` | The projects folder from the environment, or a placeholder in templated text |
| Shell syntax | `$(...)`, `&&`, backticks, `sed -i ''` | Python's `subprocess.run([...])` with a list of arguments |
| Shell-only tools | `jq`, `grep -P`, `sed`, `awk` | Python's `json`, `re`, and string methods |
| Path separators | `"dir/" + name` | `Path("dir") / name` |
| Line endings and encodings | Reading without an encoding | `read_text(encoding="utf-8")`; compare with `.splitlines()` |
| App folders | Assuming `~/.claude` | `CLAUDE_CONFIG_DIR` and `CODEX_HOME` when set |
| Folders that may be missing | Assuming the projects folder is mounted | Check it exists and do nothing when it does not |
| Script language | A Bash or PowerShell script | Python, which every agent-kit-sync machine has |

Also check that the hook:

- reads its input from standard input as JSON and never waits for a terminal;
- exits 0 to allow, 2 to block with a reason on standard error, and never another code on purpose;
- finishes in well under its timeout, and does no network call without a short time limit;
- prints nothing to standard output on events where the app treats output as context, unless that is its purpose.

## Offer a rewrite

When the check finds problems, show the user the list and offer a rewrite in Python using only the standard library. Keep the hook's behavior the same. Ask before replacing the original.

## Add tests

Write a test file next to the hook, such as `tests/test_NAME.py`, that runs the hook as a separate process with sample inputs and checks its exit code and output. Use only the standard library's `unittest`. Cover the case that allows, the case that blocks, and malformed input. List the test under `tests` in the hook's `module.toml` entry. agent-kit-sync runs it before every push and every install, with the user's folders pointed at a temporary one.
