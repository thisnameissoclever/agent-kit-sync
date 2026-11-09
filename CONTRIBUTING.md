# Contributing to agent-kit-sync

Every change arrives through a pull request. The main branch is protected, and pull requests from anyone other than the repository owner need the owner's approval. Hosted tests are optional and are not required to merge. Other branch protections still apply.

Before merging, default to running relevant checks locally and obtaining independent review with fresh context. Record the checked commit, base, commands, results, and any platform limits. Record any deliberate verification or review exception and its remaining risk.

## Run the tests

Run local checks sequentially and select them by the affected behavior. Inspect available memory and active workloads before heavy execution, and bound runtime and output. Automated Windows child launches must use `agent_kit_sync.processes`, including calls in test helpers; the source contract test rejects direct subprocess launch bypasses. Use mocked startup checks before a native process check. Never reproduce a window or memory incident with visible terminal launches or a process stress loop. Open a visible interactive terminal only when the user expressly requests it. Tests and self-tests explicitly disable automatic sync.

Run the tests with Python 3.9 or later and Git, from the `tests` folder:

```sh
python3 -m unittest -v
```

The tests build fake home folders and a local bare Git repository, and use no network.

The **Tests** workflow is available on demand through **Actions > Tests > Run workflow**. It runs nine combinations across Linux, macOS, Windows, and Python 3.9, 3.10, and 3.13. It does not run automatically on pushes or pull requests. Run it only when explicitly requested; opening, updating, or merging a pull request does not authorize hosted test runs.

## Keep personal and employer terms out of this repository

This repository is public. Before you commit, compare your changes with your own list of terms that must never appear here, such as your name, your employer, and its host names. Keep that list outside the repository, in `~/.agent-kit-sync/contributor-blocklist.txt`, one term per line, and install the check once:

```sh
cp scripts/pre-commit .git/hooks/pre-commit
```

The check runs only for contributors who install it, and the hosted tests cannot run it, because they cannot see your list.

## Rules for code

- Use only the Python standard library, plus the bundled `tomli` for Python before 3.11.
- Support Python 3.9 and later on macOS, Windows, and Linux.
- Build paths with `pathlib` or `os.path`, never by joining strings with `/`.
- Write every file through `fsutil.atomic_write`.
- Add a test for every behavior you add or change.
