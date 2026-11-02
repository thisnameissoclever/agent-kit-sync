---
name: agent-kit-sync-setup
description: Set up agent-kit-sync on this machine for the first time, or add this machine to an existing config repo. Use when the user asks to set up, install, or onboard agent-kit-sync, or to sync their skills and hooks to a new computer.
---

# Set up agent-kit-sync on this machine

Run each step below in order. Steps that run a command print JSON; read it before the next step. The user answers every question before anything in an agent app's folders changes. Ask each question with your app's question tool, one decision per question, and give a recommendation for each.

The command is `{{kit.cli}}`. On a machine without agent-kit-sync, the README's setup prompt has you clone the tool repo first. Until step 2 finishes, run each command as `python3 -I -S CLONE/bootstrap.py setup ...`, where CLONE is that clone's folder; on Windows use `py -3` in place of `python3`.

## 1. Check prerequisites

Run `{{kit.cli}} setup check`.

- If `git.ok` or `python.ok` is false, show the user the matching command from `install_commands` and stop until they have installed it.
- Note the apps listed under `apps`. Only these apps get copies.

## 2. Choose the config repo

Ask the user whether they already have a config repo. Recommend a private repository on their own GitHub account.

- **Existing repo:** run `{{kit.cli}} setup init --config-url URL`. If the output has `"config_public": true`, warn the user, in these words or close to them: "Anyone on the internet can read this repository. It will hold your instructions and hook code, and often text about your work. agent-kit-sync accepts no responsibility for anything that leaks from a public config repo. Make it private before continuing, or confirm that you accept this." Continue only after the user confirms.
- **New repo:** run `{{kit.cli}} setup init --create`. The repository on GitHub is created in step 8, after the user's answers.

## 3. Collect this machine's values

Ask for each value, with the recommendation shown:

| Value | Recommendation |
|---|---|
| Machine name | The computer's host name, shortened |
| Tags | `work` or `personal`, plus any the config repo's modules require |
| Git identity for the config repo | The identity in the user's global Git settings; ask for another if the config repo belongs to a different account |
| Policy | `new_items = "ask"`, `code_changes = "ask"`, `other_changes = "apply"`, `outgoing = "ask-code"`, `tool_updates = "apply"`, `approval = "agent"` |

Explain the policy in one sentence per setting: with these values, text spreads by itself and code moves between machines only after the user approves it on the sending and the receiving machine. Explain that `approval = "terminal"` stops agents from approving held changes.

## 4. Choose the projects and worktrees folders

The projects folder is where the user keeps code repositories. Run `{{kit.cli}} setup scan` and read `projects_candidates`.

- When `kit.toml` in the config repo sets `defaults.projects_folder_name` and a candidate has that name, recommend it.
- When there are other candidates, list them and recommend the one holding the most repositories.
- When there are none, ask whether the user keeps repositories somewhere else, such as a network or USB drive, and recommend creating a folder in their home folder.

Recommend the worktrees folder inside the projects folder, named by `defaults.worktrees_folder_name` for this operating system, or `_worktrees` on macOS and Linux and `.worktrees` on Windows when the config repo sets none.

## 5. Suggest blocklist terms

The blocklist, `~/.agent-kit-sync/blocklist.txt`, holds terms that must never be pushed to the config repo, one per line. Suggest the user's employer name, taken from the domain of their work email address, and internal host names you saw in their instruction files. Ask the user to add or remove terms. Write the file.

## 6. Find duplicates

Run `{{kit.cli}} setup report` and read the report it writes. For each pair under "Possible duplicates", read both versions and decide whether they do the same job. Names and wording may differ; two items are duplicates when an agent following either one would act the same way. Also compare every local skill, subagent definition, hook, and instruction-file heading that the report did not pair against the config repo's modules, because the tool's signals miss duplicates with different names and little shared text.

For each duplicate, show the user a short excerpt of each version that shows the difference, and recommend one outcome:

- **The config repo's version replaces the local one**, when the local copy is an older version of the same item.
- **The local version replaces the config repo's**, when this machine has the newer or better version.
- **Merge the two and sync the result**, when each has rules the other lacks. Write the merged text and show it to the user before using it.

## 7. Recommend local items to add

For each local skill, subagent definition, hook, and instruction section that is not a duplicate, recommend one of:

- **Add to the config repo**, when it would help on every machine and has nothing specific to this machine or employer.
- **Add as a local module**, when it belongs to this machine only, such as instructions about an employer's systems.
- **Leave it alone**, when it comes from a plugin or another tool that manages it.

For each hook to add, follow the portable-scripts skill before adding it.

## 8. Apply the answers

1. For a new config repo, create it now. With the GitHub CLI: `gh repo create NAME --private --source ~/.agent-kit-sync/config --push`.
2. Add each chosen item with `{{kit.cli}} add PATH --module MODULE` (add `--local` for a local module, `--heading "HEADING"` for an instruction section).
3. Write the answers file, `~/.agent-kit-sync/state/setup-answers.toml`, with the machine values from steps 3 and 4 and these lists:
    - `approve`: each `module/item` the user approved to install.
    - `replace_existing`: each `app|module/item` where the config repo's version replaces a local file.
    - `remove_registrations`: one table per existing hook entry that was adopted into a module, with `file`, `event`, and `command` from the scan, so the hook does not run twice.
4. Run `{{kit.cli}} setup apply --answers ~/.agent-kit-sync/state/setup-answers.toml`.
5. Before installing any config repo hook the user has not seen on this machine, show them its code and ask.

## 9. Finish

1. Offer to add `~/.agent-kit-sync/bin` to the user's command path, and say which file you would change.
2. If Codex is installed, ask the user to open `/hooks` in Codex and approve the agent-kit-sync entries.
3. Run `{{kit.cli}} doctor` and report any problem it prints, with the fix.
