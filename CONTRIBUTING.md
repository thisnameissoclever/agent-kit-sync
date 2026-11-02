# Contributing to agent-kit-sync

Every change arrives through a pull request. The main branch is protected: a pull request merges only after the tests pass on Linux, macOS, and Windows, and pull requests from anyone other than the repository owner also need the owner's approval.

## Run the tests

Run the tests with Python 3.9 or later and Git, from the `tests` folder:

```sh
python3 -m unittest -v
```

The tests build fake home folders and a local bare Git repository, and use no network.

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
