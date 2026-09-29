# 2026-09-30 Post-Sync Auto Import

## Request / Purpose
After Email/QQ sync completes, automatically import new messages using the existing `import_message` flow.
Each source (email, QQ) maintains an independent cursor. First run establishes a baseline (does not import history).
Each message gets its own Agent turn. Failure stops processing for that source; cursor stays at last success.
Already-imported messages are skipped and cursor advances past them.

## Actual Changes
- **`agent/messages/auto_import.py`** — New module:
  - Cursor persistence in `data/messages/auto_import_state.json` (per-source: email uses max Message ID, QQ uses max rowid)
  - First-run baseline logic (sets cursor to current max, doesn't import history)
  - `auto_import_source(source, client, files)` — processes one source
  - `run_post_sync_auto_import(client, files)` — orchestrates both sources
  - Dynamic function lookup via `_source_max()` / `_source_messages_after()` for patchability
- **`agent/main.py`**:
  - `--auto-import` argparse flag for subprocess mode
  - Subprocess handler: creates Client + FileTools, runs `run_post_sync_auto_import`, prints result, exits
  - Post-sync hook in main loop: after `update_email` / `update_qq` tool results, runs auto-import inline
- **`agent/event_runtime.py`**:
  - `launch_auto_import(root)` — launches `--auto-import` subprocess in a new console window (Windows)
- **`tests/test_auto_import.py`** — 8 new tests:
  - Baseline: existing messages not imported, cursor set to max
  - New messages after baseline get imported
  - Old `imported=True` messages skipped, cursor advances
  - Failure stops cursor at last success, retry on next run
  - `already_imported` status skipped, cursor advances
  - Email/QQ sources independent (email failure doesn't block QQ)

## Executed Checks
- `tests/test_auto_import.py`: 8/8 passed
- Full suite: **552 passed**, 0 failed (114s)

## Known Failures or Limitations
- Auto-import subprocess requires Windows (`CREATE_NEW_CONSOLE`)
- `launch_auto_import()` is available but not yet wired into `tick()` — currently only used via the main-loop hook after interactive sync commands

## Commit
Same commit as this log.