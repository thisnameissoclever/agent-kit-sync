---
name: agent-kit-sync-resolve
description: Handle an agent-kit-sync alert or held change by explaining it to the user, recommending a choice, and applying the answer. Use when an agent-kit-sync alert appears, or when the user asks about held changes, sync conflicts, or why something did not sync.
---

# Resolve agent-kit-sync alerts and held changes

1. Run `{{kit.cli}} status` for open alerts, then `{{kit.cli}} held --json` for held changes.
2. For each held change, read its summary and any saved files listed under `files`. Explain to the user, in one or two sentences, what happened and what each choice does. Recommend one choice, using the table below.
3. Ask the user with your app's question tool. Apply the answer with `{{kit.cli}} held resolve ID CHOICE`. If it says approval needs a terminal, tell the user to run that same command in a terminal outside any agent session.
4. Run `{{kit.cli}} sync` to apply it.

| Kind | What happened | Usually recommend |
|---|---|---|
| `edit` | A local edit failed the checks or could not be carried back exactly | Fix the problem in the installed copy, then `retry`; `discard` when the edit was a mistake |
| `outgoing` | A local edit waits for approval before leaving this machine | `approve` after showing the user the change |
| `incoming` | A change from the config repo waits for approval | `apply` for a change the user recognizes; `decline` otherwise, which keeps the earlier version here |
| `new-item` | A new item from the config repo waits for approval | `apply` after showing what it is; `decline` disables it here |
| `removal` | An item was removed from the config repo | `apply` |
| `setting` | Another program changed a synced setting | `restore` once; `always-restore` when the same program keeps resetting it; `keep-and-sync` when the user changed it on purpose |
| `deletion` | Copies vanished in a way that looks like an app reset | `restore`; `delete-everywhere` only when the user meant it |
| `merge-conflict` | Two machines changed the same lines | Compare both versions; `discard` keeps the other machine's, `use-this` keeps this machine's |
| `edit-conflict` | Two apps' copies on this machine were edited differently | `discard`, unless the saved edit has something the kept one lacks |
| `collision` | An app already has an item with the same name that agent-kit-sync did not install | Compare them as the setup skill compares duplicates |
| `checkout-edit` | A file changed in the config repo checkout failed the checks, so it was set back | Fix what the summary reports in the saved file, then `use-this` and sync; `discard` when the change was a mistake |

For an alert that is not a held change, follow the command it gives. An alert that a hook failed its tests on this machine, or has no tests, means the hook stays off here: run `{{kit.cli}} test MODULE` to see why, then fix the hook or its tests as the portable-scripts skill describes. When the tests fail only on this machine's operating system, tell the user which system and which line. A "cannot find its current version" error from a hook means the dispatcher cannot start; tell the user to run `agent-kit-sync repair` in a terminal.
