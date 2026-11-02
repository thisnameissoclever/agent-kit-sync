## agent-kit-sync

agent-kit-sync syncs skills, subagent definitions, hooks, these instruction sections, and some settings to every agent app on every machine this user has. The command is `{{kit.cli}}`.

- Editing an installed copy changes it in every app and on every machine at the next sync.
- Deleting an installed copy deletes it everywhere. To remove something from this machine only, run `{{kit.cli}} disable MODULE/ITEM`.
- Before creating a skill, hook, or subagent definition, ask the user whether to sync it, then follow the agent-kit-sync-add skill.
- For a rule that should apply on every project and machine, run `{{kit.cli}} remember "RULE"` instead of saving it in project memory.
- When an agent-kit-sync alert appears, follow the agent-kit-sync-resolve skill.
