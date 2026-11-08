# Lessons learned

## 2026-11-08: Explain setup decisions and create required subdirectories

1. **What happened:** Setup asked for approval using skill names and linked comparisons without enough explanation in the questions. It also treated missing module directories as a reason to disable the module.
2. **Root cause:** The setup instructions did not require a self-contained explanation of each proposed change or distinguish a missing directory from missing user data.
3. **Prevention:** Explain each item's purpose, affected apps, current behavior, proposed differences, and merge strategy in the approval question. Join child directory names to the approved parent, reuse existing directories, and create required missing subdirectories. Preserve existing contents and distinguish directory creation from restoring data.
4. **How to verify:** Review a setup question without opening its links and confirm that the reader can understand the decision. Walk through setup with an existing worktrees folder and an absent `_status/state` directory: reuse the former, create the latter under the approved projects folder, and preserve any existing files. A missing data file must receive its own functional assessment.
