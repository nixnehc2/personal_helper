# 2026-10-01: Fix call_for_user Resume and Feishu Wake-up

- Date: 2026-10-01 (Asia/Shanghai)
- Commit: same commit as this log

## Purpose

Fix two issues in the Automation `call_for_user` flow:

1. After `call_for_user` receives a user answer, the runtime incorrectly falls into `waiting_feedback` and calls `read()` again, overwriting the answer
2. When a user answers via Feishu, the answer is written to SQLite but never wakes up `_wait_for_answer()` because `answer_event.set()` is not called

## Changes

### `agent/event_runtime.py`

**Problem 1 fix - call_for_user answer resume:**
- Added `user_was_answered` flag before the inner turn loop
- When `call_for_user` receives a valid answer, sets `user_was_answered = True` before breaking out of the inner loop
- After the inner loop, checks `user_was_answered` first and `continue`s the outer loop, which starts a new `run_turn(... user=user_answer)` directly
- This ensures the answer flows straight to the next Agent turn without entering `waiting_feedback`

**Problem 2 fix - Feishu answer wake-up:**
- Modified `_wait_for_answer` to poll the SQLite database each timeout cycle
- After `answer_event.wait(timeout)`, checks if the request status is `answered` in the DB
- If answered (by Feishu or any remote channel), populates `answer_holder` from the DB and sets `answer_event`
- The database is the authoritative state source; `answer_event` is only a latency-reduction signal

### `tests/test_automation_protocol_violation.py`

**Updated tests:**
- `test_recovery_completes_via_call_for_user_then_complete_event`: Now captures all `user` args to `run_turn` and asserts `captured_users[2] == "yes"`. Uses `unexpected_read` guard that fails the test if `read()` is called after `call_for_user` answer.

**New tests:**
- `test_normal_call_for_user_resumes_directly`: Normal (non-recovery) call_for_user flow. Verifies the second `run_turn` receives the user answer directly, with `unexpected_read` guard.
- `TestWaitForAnswer.test_terminal_answer_wakes_immediately`: Terminal answer sets `answer_event` and returns immediately.
- `TestWaitForAnswer.test_feishu_answer_discovered_via_sqlite_polling`: Feishu answer only writes to SQLite; `_wait_for_answer` discovers it by polling the DB.
- `TestWaitForAnswer.test_feishu_answer_discovered_in_thread`: `_wait_for_answer` in a thread wakes up when Feishu writes to SQLite (no `answer_event.set()`).
- `TestWaitForAnswer.test_first_response_wins_feishu_then_terminal`: Feishu answers first, terminal rejected.
- `TestWaitForAnswer.test_first_response_wins_terminal_then_feishu`: Terminal answers first, Feishu rejected.

## Verification

- `python -m pytest tests/test_automation_protocol_violation.py -q`: 21 passed
- `python -m pytest tests/test_automation_protocol_violation.py tests/test_call_for_user.py tests/test_user_requests.py tests/test_feishu_dual_channel.py tests/test_waiting_for_user.py tests/test_complete_event.py -q`: 90 passed

## Known Issues and Boundaries

- `_wait_for_answer` polls SQLite at the same interval as `answer_event.wait(timeout=0.5)`. In the worst case, a Feishu answer takes up to 0.5s to be discovered. This is acceptable for human-response latency.
- The `user_was_answered` flag is reset at the start of each outer loop iteration, ensuring it does not leak across turns.
