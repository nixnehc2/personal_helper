# Repository guidelines

- After completing a user-requested task that changes files, run the focused tests, commit the work, and push it to the configured GitHub remote.

- For every completed task that changes code, configuration, tests, or documentation, create a corresponding Markdown change log in `change_logs/` and update `change_logs/README.md` before committing. Use `YYYY-MM-DD-NN-short-description.md` with the Asia/Shanghai date and a daily sequence number. Reuse that log for iterations within the same task; create a new log for each subsequent task.
- Follow `change_logs/TEMPLATE.md`: record the request/purpose, actual changes, executed checks and results, known failures or limitations, and commit association. Distinguish historical test results from checks run for the current task; never claim unrun checks passed. The current change may be identified as "same commit as this log" instead of a not-yet-known commit hash.
- Commit and push the corresponding log together with the task changes. Never include secrets, private Message content, personal Memory, or unrelated user edits in logs or commits.
- Keep project explanation documents in `change_logs/docs/` and maintain their links. Keep the root README as the navigation entry, root `AGENTS.md` as repository instructions, runtime `prompt.md`, and Markdown test fixtures at their required locations.
