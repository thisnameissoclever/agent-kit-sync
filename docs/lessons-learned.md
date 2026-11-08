# Lessons learned

## 2026-11-08: Explain setup decisions and create required subdirectories

1. **What happened:** Setup asked for approval using skill names and linked comparisons without enough explanation in the questions. It also treated missing module directories as a reason to disable the module.
2. **Root cause:** The setup instructions did not require a self-contained explanation of each proposed change or distinguish a missing directory from missing user data.
3. **Prevention:** Explain each item's purpose, affected apps, current behavior, proposed differences, and merge strategy in the approval question. Join child directory names to the approved parent, reuse existing directories, and create required missing subdirectories. Preserve existing contents and distinguish directory creation from restoring data.
4. **How to verify:** Review a setup question without opening its links and confirm that the reader can understand the decision. Walk through setup with an existing worktrees folder and an absent `_status/state` directory: reuse the former, create the latter under the approved projects folder, and preserve any existing files. A missing data file must receive its own functional assessment.

## 2026-11-08: Keep hosted tests optional and available on demand

1. **What happened:** Hosted tests were configured to run automatically for pushes to main and pull requests, and repository documentation treated their results as a merge requirement. The owner requested on-demand checks without that forced requirement.
2. **Root cause:** The workflow triggers and documented merge policy tied routine repository changes to hosted test execution.
3. **Prevention:** Keep the test jobs available through `workflow_dispatch` only and run them only when explicitly requested. Default to relevant local checks and independent review with fresh context, recording results, platform limits, and any deliberate exception. Preserve the pull request process and other branch protections.
4. **How to verify:** Compare the full workflow `jobs` block with the prior version to confirm the nine-job matrix, permissions, commands, and timeouts are preserved. Confirm `workflow_dispatch` is the only trigger, required hosted status checks are removed from branch protection, other protection settings are preserved, and the contribution and security documentation agree. Do not dispatch hosted tests to verify this policy change.
