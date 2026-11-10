# Lessons learned

## Preserve existing project directories

Sync installs managed configuration. Existing project directory names are user choices. Sync must preserve their paths and data, including when an older config requests a directory migration. Keep path references as written in the source templates. A change to naming guidance never authorizes moving existing data.

## 2026-11-08: Test hooks with the files the machine installs

1. **What happened:** A config commit changed a synced hook to import a new support file and added that file as a new item. On a machine whose policy asks before new items, the support file was held for approval while the changed hook installed. Every hook call in Claude Code, Codex, and Cursor then failed with `ModuleNotFoundError` until the support file was approved.
2. **Root cause:** Hook tests ran in the whole config checkout, where every file of the module existed, so they could not see that this machine would install the hook without one of its files. The check decided each item on its own and never compared a hook with the rest of its module as installed.
3. **Prevention:** Test every hook in a folder built from the planner's copies for its module, after every item of the sync is decided, in the incoming install, in `agent-kit-sync test`, and before a local edit is pushed. Retest every hook that will run in a module whose installed files change. When a new version of a module breaks a hook that works with the installed files, keep the whole module as installed and raise an alert.
4. **How to verify:** Run `tests/test_sync.py` class `HookStagingTests`. Each case reproduces one way a hook could install without a file it needs: a held, disabled, undeclared, removed, or changed support file, a templated file, a hook set back to its earlier version, and a push of an edit that needs a file held on the pushing machine.

## 2026-11-08: Hide native child launches and reserve workers before spawning

1. **What happened:** During Windows setup and verification, the owner reported extensive terminal-window creation and exhausted memory. The windows were closed before a complete process-parent record could be preserved, so every window's origin was not established.
2. **Root cause evidence:** Automated child launch sites lacked consistent Windows hiding flags. The detached launcher combined flags for which Windows ignores `CREATE_NO_WINDOW`. Sync triggers started workers before acquiring the sync lock, and workers could wait for quiet edits before that lock. Test isolation also removed inherited background suppression.
3. **Prevention:** Route automated library and test launches through the hidden-process helper. Reject incompatible console flags, reserve an inherited operating-system worker lease before spawning, run hooks sequentially, and explicitly disable automatic sync in tests. Preserve pending work for the bounded worker loop or next trigger. Apply the same hidden-launch contract to hook grandchildren. Use mocked checks and the smallest bounded native check; avoid visible reproductions and process stress loops.
4. **How to verify:** Check launch options without starting a process. Run a small native child and verify it has no console. Start one leased child, confirm a duplicate request starts no second child, and verify ownership releases after exit. Check source launch sites, nested hooks, test isolation, and serial execution. Record evidence and platform limits without claiming an unlimited guarantee over arbitrary programs an agent might execute.

## 2026-11-08: Apply local disable decisions before incoming approval policy

1. **What happened:** Declining a new item disabled installation, but the next sync raised the same incoming approval request again.
2. **Root cause:** Incoming policy evaluated every manifest item before the planner applied machine-local disable overrides.
3. **Prevention:** Check both module and item disable overrides before creating incoming requests, running incoming tests, or recording a source version as installed. Close only the obsolete incoming request and preserve saved local edits. Keep hook names available for rendering other enabled items, and clear disabled incoming requests even when their sources have left the inventory.
4. **How to verify:** Decline an item, sync repeatedly, and check that it stays absent without another incoming alert. Disable a whole module with existing requests and confirm its incoming requests close while an outgoing saved edit survives. Enable a previously declined item under review-all policy and confirm first installation still needs approval. Disable a referenced hook and confirm other items still render while that hook is removed; disable an item with an old request after its manifest entry disappears and confirm the request closes.

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
