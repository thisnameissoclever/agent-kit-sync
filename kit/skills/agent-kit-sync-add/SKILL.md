---
name: agent-kit-sync-add
description: Add a skill, subagent definition, hook, instruction section, setting, or support file to the user's agent-kit-sync config so it syncs to every machine. Use when the user asks to sync something, or after creating a skill, hook, or subagent definition the user wants synced.
---

# Add something to agent-kit-sync

1. Ask the user which module it belongs to. A module groups items that work together, such as a rule's instruction section and the hook that enforces it. Recommend an existing module when one fits; otherwise suggest a short lowercase name with hyphens.
2. Ask whether it should sync to every machine or stay on this one. Recommend a local module for anything about one employer, one machine, or one service only this machine runs.
3. For a hook or script, follow the portable-scripts skill first.
4. Run `{{kit.cli}} add PATH --module MODULE`. Add `--local` for a local module. For one section of `CLAUDE.md` or `AGENTS.md`, add `--heading "HEADING"`; the original text is wrapped in markers in place. For a hook, add `--events` with the events it handles, such as `pre_tool_use,stop`.
5. Open the module's `module.toml` and check the new entry:
    - For a hook, set `tools` to the tools it handles, `timeout`, and `on_failure` (`closed` only when the action must not proceed if the hook fails), and list its tests under `tests`.
    - For a skill whose `SKILL.md` mentions paths, use placeholders such as `{{kit.literal home}}` and list the file under `templates`.
    - For a subagent definition, write a header field that only one app understands with that app's name in front, such as `claude.tools`. Put body lines that only some apps should see between a `{{kit.literal only claude}}` line and a `{{kit.literal end}}` line.
    - For a hook that runs only while one subagent works, set the hook's `events = []` and call it from the subagent definition with `{{kit.literal hook MODULE/HOOK}}`.
6. Run `{{kit.cli}} test MODULE`, then `{{kit.cli}} sync`.

A setting is added by hand to `module.toml`:

```toml
[[settings]]
app = "claude"
key = "env.EXAMPLE"
value = "1"
```

Use `mode = "contains"` with a list value for a list that other software also adds to.
