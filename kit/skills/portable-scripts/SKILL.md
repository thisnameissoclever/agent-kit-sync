---
name: portable-scripts
description: Required before any hook or script is synced, recommended for sync, or written to run on more than one machine. Checks it for anything that fails on macOS, Windows, or Linux, reports a verdict for each of the three to the user before it syncs, offers a rewrite that works on all three, and adds tests.
---

# Make a hook or script work on any machine

A synced hook or script runs on every machine the user has, and those machines can run macOS, Windows, or Linux. Something that works on one, such as a fixed path or a program that only one system has, breaks on the others, often silently.

This check is required. Do it before you add a hook or script to agent-kit-sync, before you recommend syncing one, and before you call one ready to run elsewhere. Never tell the user that a script works on another system without having done it.

## Check

1. Run `{{kit.cli}} test MODULE` for a hook already in a module. It reports every line that matches a known way to fail on macOS, Windows, or Linux, and it runs the hook's tests. For a script not in a module yet, read it yourself against the table below.
2. Read the script and its tests yourself as well. The automatic check knows only some patterns; the table lists more.
3. Check every file the hook runs or reads: its tests, the scripts it starts, and the fake programs its tests create.

| Problem | Example | Works everywhere |
|---|---|---|
| Fixed path to the user's files | `/Users/name/.claude/hooks/x.py`, `C:\Users\name` | `Path.home() / ".claude"`, or a path relative to the script: `Path(__file__).parent` |
| Fixed interpreter or program path | `/opt/homebrew/bin/python3`, `/bin/sh`, `/usr/bin/curl` | Let the dispatcher start it; inside a script, `sys.executable`, or the program's name found with `shutil.which` |
| Drive letters or mount points | `D:\Projects`, `/Volumes/Work` | The projects folder from the environment, or a placeholder in templated text |
| Shell syntax | `$(...)`, `&&`, backticks, `sed -i ''`, `shell=True` | Python's `subprocess.run([...])` with a list of arguments |
| Shell-only tools | `jq`, `grep -P`, `sed`, `awk` | Python's `json`, `re`, and string methods |
| Node package programs | `spawn("npx", ...)`, `node_modules/.bin/jest` | On Windows these are `.cmd` files, which `spawn` refuses. Start the package's JavaScript file with `process.execPath` |
| Fake programs in tests | A test that writes `#!/bin/sh` and runs `chmod` | Write the fake program in Python or JavaScript and start it with its interpreter |
| PATH lists | `f"{bin}:/usr/bin"` | `os.pathsep.join([...])` in Python, `path.delimiter` in Node |
| Path separators | `"dir/" + name` | `Path("dir") / name` |
| System-only modules and calls | `fcntl`, `pwd`, `os.getuid`, `signal.SIGALRM`, `winreg` | A standard-library call that exists everywhere, or a branch on `sys.platform` with a working path for each system |
| System-only programs | `osascript`, `launchctl`, `xdg-open`, `powershell` | A branch on `sys.platform` for each system, or `requires.os` in the module when it truly belongs to one system |
| Line endings and encodings | Reading without an encoding | `read_text(encoding="utf-8")`; compare with `.splitlines()` |
| App folders | Assuming `~/.claude` | `CLAUDE_CONFIG_DIR` and `CODEX_HOME` when set |
| Folders that may be missing | Assuming the projects folder is mounted | Check it exists and do nothing when it does not |
| Script language | A Bash or PowerShell script | Python, which every agent-kit-sync machine has |

Also check that the hook:

- reads its input from standard input as JSON and never waits for a terminal;
- exits 0 to allow, 2 to block with a reason on standard error, and never another code on purpose;
- finishes in well under its timeout, and does no network call without a short time limit;
- prints nothing to standard output on events where the app treats output as context, unless that is its purpose.

## Report the verdict

Before the hook or script syncs, tell the user the verdict for each system, and do not continue until every system the module installs on is a pass or the user decides otherwise:

| System | Verdict | Why |
|---|---|---|
| macOS | Works, or fails | The lines that fail, and how |
| Windows | Works, or fails | |
| Linux | Works, or fails | |

Say how you know. "Tests passed on macOS" is a run; "read for Windows" is a reading. A system you could only read, not run, is a reading, and the user should know which one it is. When the user asks whether a script works on a system, give this table.

When the script truly belongs on fewer systems, such as a macOS menu-bar helper, set `requires.os` in its module, such as `requires = { os = ["macos"] }`; the module then installs only there and is checked only for those systems.

When the automatic check reports a line that is safe, because the line only names the program or only runs on one system, add a comment on that line: `agent-kit-sync: portable because REASON`, with a real reason. The test command lists every such line, so the user can see them.

## Offer a rewrite

When the check finds problems, show the user the list and offer a rewrite, in Python using only the standard library, or in the hook's own language when the fix is small. Keep the hook's behavior the same. Ask before replacing the original.

## Add tests

Every synced hook needs tests. `{{kit.cli}} add` refuses a synced hook without them, an edit to a synced hook is held until its tests pass, and every machine, this one included, runs them before it installs a new or changed hook. A hook with no tests, or whose tests fail or cannot run on a machine, stays off that machine with an alert.

Write a test file next to the hook, such as `tests/test_NAME.py`, that runs the hook as a separate process with sample inputs and checks its exit code and output. Use only the standard library's `unittest`, or `node:test` for a JavaScript hook. Cover the case that allows, the case that blocks, and malformed input. Find the hook from the test's own location, such as `Path(__file__).resolve().parent.parent / "hook.py"`, never from a fixed path. Start the hook with `sys.executable`, or `process.execPath` in Node, never by its name or its `#!` line. List the test under `tests` in the hook's `module.toml` entry. agent-kit-sync runs it before every push and every install, with the user's folders pointed at a temporary one.
