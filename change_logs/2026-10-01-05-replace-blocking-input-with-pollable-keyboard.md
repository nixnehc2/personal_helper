# 2026-10-01: Replace Blocking input() Thread with Pollable Keyboard for call_for_user

- Date: 2026-10-01 (Asia/Shanghai)
- Commit: same commit as this log

## Purpose

Replace the blocking input() thread model in call_for_user with a pollable Windows keyboard approach using msvcrt. This eliminates zombie terminal reader threads that persist after Feishu answers, and enables immediate termination when Feishu answers or cancel/invalid states arrive.

## Changes

### gent/event_runtime.py

- **Added WindowsKeyboard class**: Uses msvcrt.kbhit() / msvcrt.getwch() for non-blocking keyboard polling. Guarded by os.name == "nt" for non-Windows compatibility.
- **Added _wait_for_user_request()**: Unified wait function that polls both SQLite (for Feishu/remote answers and cancel/invalid status) and keyboard (for terminal input) in a single loop. No background threads, no blocking input(). Returns {"status": "answered"/"cancelled"/"invalid"/"exit", ...}.
- **Updated un_event call_for_user section**: Now uses _wait_for_user_request with keyboard parameter. Handles cancelled/invalid/exit states from the wait result.
- **Deprecated _start_terminal_input_thread**: Removed from call_for_user flow. No longer creates background input() threads.
- **Kept _wait_for_answer**: Legacy function preserved for backward compatibility but no longer used in the main flow.

### gent/feishu_client.py

- **Fixed /cancel routing**: Now uses _find_waiting_request(msg.chat_id) instead of broken get_waiting_for_event(""). Shows "没有等待回答的请求" when no matching request found.

### gent/user_requests.py

- **Added get_latest_waiting_for_chat(chat_id)**: Finds the most recent waiting request for a given Feishu chat_id.

### 	ests/test_automation_protocol_violation.py

- **Added FakeKeyboard class**: Test keyboard that returns pre-programmed key sequences for testing _wait_for_user_request.
- **Replaced TestWaitForAnswer with TestWaitForUserRequest**: 11 new tests covering terminal Enter, Feishu answer, Feishu-while-typing, cancelled, invalid, Ctrl+C, backspace, first-response-wins, threaded Feishu wake, empty Enter.
- **Added TestGetLatestWaitingForChat**: 5 tests for the new UserRequestManager method.
- **Updated integration tests**: 	est_recovery_completes_via_call_for_user and 	est_normal_call_for_user_resumes_directly now patch _wait_for_user_request instead of the old thread-based functions.

### 	ests/test_feishu_dual_channel.py

- Removed TestWaitForAnswer and TestTerminalInputThread (now tested via TestWaitForUserRequest in the protocol violation test file).

## Verification

- python -m pytest tests/test_automation_protocol_violation.py -q: 31 passed
- Full focused suite: 96 passed

## Known Issues and Boundaries

- msvcrt.getwch() may not handle Chinese IME composition sequences correctly. V1 only supports basic single-character input. If IME issues are found, a higher-level Windows Console read approach would be needed, but the pollable (non-blocking) requirement must still be met.
- Non-Windows platforms raise OSError when _wait_for_user_request is called without an injected keyboard adapter. Import of gent.event_runtime is safe on all platforms.
