"""The launcher every registered command calls. Setup copies this file into the kit folder.

It does not change during an update: it reads which tool version is current and runs it.
It imports only os and sys, because every hook call pays for its start-up.
"""

import os
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(KIT, "tool")


def valid(path):
    return os.path.isfile(os.path.join(path, "agent_kit_sync", "__init__.py"))


def current_version():
    try:
        with open(os.path.join(TOOL, "current"), encoding="utf-8") as handle:
            name = handle.read().strip()
        if name and valid(os.path.join(TOOL, name)):
            return os.path.join(TOOL, name)
    except OSError:
        pass
    # The pointer is missing or damaged: fall back to the newest complete version.
    try:
        versions = [os.path.join(TOOL, n) for n in os.listdir(TOOL)]
    except OSError:
        return None
    versions = [p for p in versions if os.path.isdir(p) and valid(p)]
    versions.sort(key=os.path.getmtime, reverse=True)
    return versions[0] if versions else None


def main():
    version = current_version()
    if version is None:
        sys.stderr.write(
            "agent-kit-sync cannot find any installed version in " + TOOL + ".\n"
            "Run its setup again from the README to reinstall it.\n"
        )
        # Exit 1: agent apps show this as a hook error, and block the action only
        # on events registered to block when the dispatcher cannot run.
        return 1
    sys.path.insert(0, version)
    os.environ.setdefault("AGENT_KIT_SYNC_HOME", KIT)
    launcher = os.path.abspath(__file__)
    if len(sys.argv) == 4 and sys.argv[1] == "hook":
        # The path every hook takes: skip the command-line parser to start faster.
        from agent_kit_sync.dispatch import main as dispatch_main
        return dispatch_main(KIT, launcher, sys.argv[2], sys.argv[3])
    from pathlib import Path
    from agent_kit_sync.cli import main as cli_main
    return cli_main(sys.argv[1:], launcher=Path(launcher))


if __name__ == "__main__":
    sys.exit(main())
