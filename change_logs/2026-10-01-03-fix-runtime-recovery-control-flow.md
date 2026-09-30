# 2026-10-01: Fix Automation Runtime Recovery Control Flow

- Date: 2026-10-01 (Asia/Shanghai)
- Commit: same commit as this log

## Purpose

Fix control flow issues in the Automation protocol violation recovery implementation:

1. `call_for_user` after recovery was misclassified as recovery failure (`classify_automation_turn` did not check `call_for_user_active`)
2. Recovery instruction was duplicated in messages (outer append + run_turn append)
3. Recovery completion parsed reply from messages instead of using `files.event_reply`
4. Normal turn and recovery turn had two separate post-turn processing paths

## Changes

### `agent/event_runtime.py`

- **Unified turn outcome handling**: Refactored `run_event` main loop into nested `while True` structure. Inner loop runs the agent turn then checks structural state in order: `event_complete` -> `call_for_user_active` -> `pending_email_send/pending_approval` -> protocol_violation. The same path runs regardless of whether the turn was normal, post-user-answer, or recovery.
- **Recovery uses unified path**: Recovery no longer has separate complete_event/call_for_user decision logic. Recovery only sets `user` to the recovery instruction and `continue`s the inner loop, letting the next `run_turn` go through the standard flow.
- **`complete_event` reply uses `files.event_reply`**: When the event completes, uses `files.event_reply` directly instead of parsing `messages[-1]`.
- **Eliminated recovery instruction duplication**: No longer pre-appends `recovery_instruction` to messages then pops it; instead sets `user = recovery_instruction` and lets `run_turn` handle conversation history.

### `tests/test_automation_protocol_violation.py`

Added 3 new regression tests (replaced 2 old tests, kept all 7 classify unit tests + 1 constant test):

| Test | Scenario |
|------|----------|
| `test_recovery_completes_via_call_for_user_then_complete_event` | protocol_violation -> recovery -> call_for_user -> user answer -> complete_event (full chain) |
| `test_recovery_instruction_only_once` | Verifies recovery instruction appears exactly once in run_turn calls |
| `test_complete_event_reply_used_from_files` | Verifies event reply uses `files.event_reply` not message parsing |

Updated retained tests:

| Test | Change |
|------|--------|
| `test_recovery_completes_when_agent_calls_complete_event` | Added `event["reply"]` assertion |
| `test_no_recovery_when_complete_event_called_normally` | Added `event["reply"]` assertion |
| `test_old_waiting_feedback_phase_does_not_mask_violation` | Retained |
| `test_recovery_again_plain_text_leads_to_recovery_failed` | Retained |

## Verification

- `python -m pytest tests/test_automation_protocol_violation.py tests/test_complete_event.py tests/test_call_for_user.py -q`: 40 passed
- `python -m pytest tests/test_waiting_for_user.py tests/test_user_requests.py tests/test_feishu_dual_channel.py -q`: 44 passed
- `py_compile.compile(agent/event_runtime.py)`: syntax OK

## Known Issues and Boundaries

- `classify_automation_turn` function is kept in the module for unit tests but runtime no longer calls it. Could be marked test-only in the future.
- `_create_feishu_client` initialization fails in call_for_user test (lark_oapi Client.builder missing) but does not affect functionality (graceful fallback to terminal-only mode).
