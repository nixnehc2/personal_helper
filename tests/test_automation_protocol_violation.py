"""Tests for Automation protocol violation detection and recovery."""
import contextlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agent.event_runtime import classify_automation_turn, MAX_PROTOCOL_RECOVERY_ATTEMPTS
from agent.user_requests import UserRequestManager


class TestClassifyAutomationTurn(unittest.TestCase):
    """Unit tests for the pure classify_automation_turn function."""

    def _session(self, **kw):
        s = MagicMock()
        s.event_complete = False
        s.pending_email_send = None
        s.pending_approval = None
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def _event(self, **kw):
        e = dict(event_id="evt1", source="qq", content="test", occurred_at=time.time())
        e.update(kw)
        return e

    def test_completed_via_reply(self):
        event = self._event(reply="done", phase="delivery")
        self.assertEqual(classify_automation_turn(event, self._session()), "completed")

    def test_completed_via_session_flag(self):
        event = self._event()
        session = self._session(event_complete=True)
        self.assertEqual(classify_automation_turn(event, session), "completed")

    def test_waiting_for_user_via_phase(self):
        for phase in ("waiting_feedback", "waiting_for_user"):
            event = self._event(phase=phase)
            self.assertEqual(classify_automation_turn(event, self._session()), "waiting_for_user",
                             f"phase={phase}")

    def test_waiting_for_approval_email(self):
        event = self._event()
        session = self._session(pending_email_send={"id": 1})
        self.assertEqual(classify_automation_turn(event, session), "waiting_for_approval")

    def test_waiting_for_approval_generic(self):
        event = self._event()
        session = self._session(pending_approval={"action": "send_email"})
        self.assertEqual(classify_automation_turn(event, session), "waiting_for_approval")

    def test_protocol_violation_plain_finish(self):
        event = self._event(phase="running")
        session = self._session()
        self.assertEqual(classify_automation_turn(event, session), "protocol_violation")

    def test_reply_takes_precedence_over_everything(self):
        event = self._event(reply="ok", phase="running")
        session = self._session(event_complete=False, pending_email_send={"id": 1})
        self.assertEqual(classify_automation_turn(event, session), "completed")


class TestRecoveryConstants(unittest.TestCase):
    def test_max_attempts_is_one(self):
        self.assertEqual(MAX_PROTOCOL_RECOVERY_ATTEMPTS, 1)


@contextlib.contextmanager
def _noop_turn(*args, **kwargs):
    """A no-op replacement for scheduler.turn that doesn't acquire locks."""
    yield


class TestProtocolViolationRecovery(unittest.TestCase):
    """Integration tests for the recovery mechanism in run_event."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "memory"
        self.root.mkdir()
        (self.root / "AGENT.md").write_text("Test", encoding="utf-8")
        self.emitted = []

    def _emit(self, text):
        self.emitted.append(text)

    def _mock_event(self):
        return dict(
            event_id="test-evt-001", source="qq", content="test message",
            occurred_at=time.time(), phase="running", messages=[],
            active_session="tok123", data={"message": {"id": "msg1"}},

        )

    def _mock_rule(self, event):
        """Rule with the event in pending_events so deliver() can remove it."""
        return dict(id=1, name="test-rule", status="active", mode="once", trigger_type="event", cursor=None, next_check_at=None, pending_events=[event])

    def _create_files(self):
        from agent.tools import FileTools
        files = FileTools(self.root)
        self.addCleanup(files.policy.close)
        files.event_session = True
        files.event_complete = False
        files.policy.scheduler.turn = _noop_turn
        return files

    def _make_change_event_mock(self, event, rule):
        """Mock change_event that applies the mutation and returns the event."""
        def fake_change_event(store, rule_id, event_id, change_fn, allow_paused=False):
            change_fn(rule, event)
            return event
        return MagicMock(side_effect=fake_change_event)

    def _setup_store(self, rule):
        store = MagicMock()
        store.path = str(self.tmp.name)
        store.settings = {}
        store.manage.return_value = {"rule": rule}
        store.decode.return_value = rule
        return store

    @patch("agent.automation_qq.read_event_qq", return_value={"text": "hi", "sender": {}})
    @patch("agent.main.run_turn")
    def test_recovery_completes_when_agent_calls_complete_event(self, mock_run_turn, mock_qq):
        """Agent forgets complete_event -> recovery -> agent calls complete_event -> event complete."""
        files = self._create_files()
        event = self._mock_event()
        rule = self._mock_rule(event)
        store = self._setup_store(rule)
        mock_ce = self._make_change_event_mock(event, rule)

        call_count = [0]
        def fake_run_turn(client, f, messages, user, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                messages.append({"role": "assistant", "content": [
                    {"type": "text", "text": "Done processing."}
                ]})
                f.event_complete = False
            else:
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "complete_event",
                     "input": {"reply": "Task completed."}}
                ]})
                f.event_complete = True
                f.event_reply = "Task completed."

        mock_run_turn.side_effect = fake_run_turn

        with patch("agent.event_runtime.change_event", mock_ce):
            from agent.event_runtime import run_event
            run_event(MagicMock(), files, store, 1, "test-evt-001", "tok123",
                      emit=self._emit, read=lambda p: "", automatic=False)

        self.assertTrue(files.event_complete)
        self.assertEqual(call_count[0], 2)
        self.assertEqual(event.get("reply"), "Task completed.")
        self.assertFalse(any("protocol_recovery_failed" in m for m in self.emitted))
        self.assertFalse(any("protocol_recovery_failed" in m for m in self.emitted))

    @patch("agent.automation_qq.read_event_qq", return_value={"text": "hi", "sender": {}})
    @patch("agent.main.run_turn")
    def test_no_recovery_when_complete_event_called_normally(self, mock_run_turn, mock_qq):
        """Agent correctly calls complete_event -> no recovery needed."""
        files = self._create_files()
        event = self._mock_event()
        rule = self._mock_rule(event)
        store = self._setup_store(rule)
        mock_ce = self._make_change_event_mock(event, rule)

        def fake_run_turn(client, f, messages, user, **kwargs):
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": "Task completed."}
            ]})
            f.event_complete = True
            f.event_reply = "Done."

        mock_run_turn.side_effect = fake_run_turn

        with patch("agent.event_runtime.change_event", mock_ce):
            from agent.event_runtime import run_event
            run_event(MagicMock(), files, store, 1, "test-evt-001", "tok123",
                      emit=self._emit, read=lambda p: "", automatic=True)

        self.assertTrue(files.event_complete)
        self.assertEqual(event.get("reply"), "Done.")
        violations = [m for m in self.emitted if "protocol_violation" in m]
        self.assertEqual(len(violations), 0, "Should not trigger protocol violation")

    @patch("agent.automation_qq.read_event_qq", return_value={"text": "hi", "sender": {}})
    @patch("agent.main.run_turn")
    def test_recovery_completes_via_call_for_user_then_complete_event(self, mock_run_turn, mock_qq):
        """Protocol violation -> recovery -> call_for_user -> user answer -> complete_event.

        Verifies:
        - The third run_turn receives user=="yes" (the call_for_user answer)
        - read() is NOT called after call_for_user answer (no extra waiting_feedback)
        - Event completes normally
        """
        files = self._create_files()
        event = self._mock_event()
        rule = self._mock_rule(event)
        store = self._setup_store(rule)
        mock_ce = self._make_change_event_mock(event, rule)

        captured_users = []

        def fake_run_turn(client, f, messages, user, **kwargs):
            captured_users.append(user)
            n = len(captured_users)
            if n == 1:
                # First turn: plain text -> protocol violation
                messages.append({"role": "assistant", "content": [
                    {"type": "text", "text": "Need more details."}
                ]})
                f.event_complete = False
            elif n == 2:
                # Recovery turn: agent calls call_for_user
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "call_for_user",
                     "input": {"prompt": "Should I finalize now?"}}
                ]})
                f.event_complete = False
                f.call_for_user_active = True
                f.call_for_user_prompt = "Should I finalize now?"
            else:
                # Third turn: agent completes after user answer
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu2", "name": "complete_event",
                     "input": {"reply": "Final result"}}
                ]})
                f.event_complete = True
                f.event_reply = "Final result"

        mock_run_turn.side_effect = fake_run_turn

        def unexpected_read(prompt):
            raise AssertionError(
                "call_for_user answer must resume Agent directly; "
                "Runtime must not enter waiting_feedback"
            )

        class FakeRequestManager:
            def create_request(self, request_id, event_id, rule_id, session, kind, prompt):
                return {"id": request_id}
            def get_request(self, request_id):
                return {"status": "waiting"}
            def submit_answer(self, request_id, answer, source):
                return "success"
            def mark_invalid(self, request_id):
                return None

        request_manager = FakeRequestManager()

        with patch("agent.event_runtime.change_event", mock_ce), \
             patch("agent.event_runtime._wait_for_user_request", lambda rm, rid, kb, emit: {"status": "answered", "answer": "yes", "source": "terminal"}):
            from agent.event_runtime import run_event
            run_event(
                MagicMock(), files, store, 1, "test-evt-001", "tok123",
                emit=self._emit, read=unexpected_read, automatic=True,
                request_manager=request_manager,
            )

        self.assertEqual(len(captured_users), 3)
        # First turn: initial event instruction (some string)
        self.assertIsInstance(captured_users[0], str)
        # Second turn: recovery instruction
        self.assertIn("protocol violation", captured_users[1])
        # Third turn: must be the user answer "yes", not a new read() call
        self.assertEqual(captured_users[2], "yes",
                         "Third Agent turn must receive the call_for_user answer as user input")
        self.assertTrue(files.event_complete)
        self.assertEqual(event.get("reply"), "Final result")
        self.assertFalse(any("protocol_recovery_failed" in m for m in self.emitted))

    @patch("agent.automation_qq.read_event_qq", return_value={"text": "hi", "sender": {}})
    @patch("agent.main.run_turn")
    def test_recovery_instruction_only_once(self, mock_run_turn, mock_qq):
        """Recovery turn must send the recovery instruction exactly once to the model."""
        files = self._create_files()
        event = self._mock_event()
        rule = self._mock_rule(event)
        store = self._setup_store(rule)
        mock_ce = self._make_change_event_mock(event, rule)

        instruction = "Runtime 检测到你在上一轮没有调用 complete_event 也没有调用 call_for_user。这是一个 Automation protocol violation。请根据任务实际完成情况，立即做出明确选择：任务已完成则调用 complete_event(reply=...)；需要用户输入则调用 call_for_user(prompt=...)。不要输出普通文本。"

        call_count = [0]
        captured_instructions = []

        def fake_run_turn(client, f, messages, user, **kwargs):
            call_count[0] += 1
            captured_instructions.append(user)
            if call_count[0] == 1:
                messages.append({"role": "assistant", "content": [
                    {"type": "text", "text": "Plain text only."}
                ]})
                f.event_complete = False
            else:
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "complete_event",
                     "input": {"reply": "Recovered"}}
                ]})
                f.event_complete = True
                f.event_reply = "Recovered"

        mock_run_turn.side_effect = fake_run_turn

        with patch("agent.event_runtime.change_event", mock_ce):
            from agent.event_runtime import run_event
            run_event(MagicMock(), files, store, 1, "test-evt-001", "tok123",
                      emit=self._emit, read=lambda p: "", automatic=True)

        self.assertEqual(call_count[0], 2)
        self.assertEqual(captured_instructions[-1], instruction)
        self.assertTrue(files.event_complete)
        self.assertEqual(event.get("reply"), "Recovered")
        self.assertFalse(any("protocol_recovery_failed" in m for m in self.emitted))
        self.assertEqual(captured_instructions.count(instruction), 1, "Recovery instruction must appear exactly once in run_turn calls")

    @patch("agent.automation_qq.read_event_qq", return_value={"text": "hi", "sender": {}})
    @patch("agent.main.run_turn")
    def test_old_waiting_feedback_phase_does_not_mask_violation(self, mock_run_turn, mock_qq):
        """Existing waiting_feedback phase must not prevent detecting a new violation."""
        files = self._create_files()
        event = self._mock_event()
        event["phase"] = "waiting_feedback"
        rule = self._mock_rule(event)
        store = self._setup_store(rule)
        mock_ce = self._make_change_event_mock(event, rule)

        def fake_run_turn(client, f, messages, user, **kwargs):
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": "Plain text only."}
            ]})
            f.event_complete = False

        mock_run_turn.side_effect = fake_run_turn

        with patch("agent.event_runtime.change_event", mock_ce):
            from agent.event_runtime import run_event
            responses = iter(["/exit"])
            run_event(MagicMock(), files, store, 1, "test-evt-001", "tok123",
                      emit=self._emit, read=lambda p: next(responses), automatic=True)

        self.assertTrue(any("protocol_violation" in m for m in self.emitted))
        self.assertTrue(any("protocol_recovery_failed" in m for m in self.emitted))
        self.assertEqual(event.get("phase"), "waiting_feedback")
        self.assertFalse(files.event_complete)

    @patch("agent.automation_qq.read_event_qq", return_value={"text": "hi", "sender": {}})
    @patch("agent.main.run_turn")
    def test_recovery_again_plain_text_leads_to_recovery_failed(self, mock_run_turn, mock_qq):
        """First turn plain text -> recovery still plain text -> recovery failed."""
        files = self._create_files()
        event = self._mock_event()
        rule = self._mock_rule(event)
        store = self._setup_store(rule)
        mock_ce = self._make_change_event_mock(event, rule)

        def fake_run_turn(client, f, messages, user, **kwargs):
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": "Still thinking."}
            ]})
            f.event_complete = False

        mock_run_turn.side_effect = fake_run_turn

        with patch("agent.event_runtime.change_event", mock_ce):
            from agent.event_runtime import run_event
            responses = iter(["/exit"])
            run_event(MagicMock(), files, store, 1, "test-evt-001", "tok123",
                      emit=self._emit, read=lambda p: next(responses), automatic=True)

        self.assertTrue(any("protocol_recovery_failed" in m for m in self.emitted))
        self.assertEqual(event.get("phase"), "waiting_feedback")
        self.assertFalse(files.event_complete)

    @patch("agent.automation_qq.read_event_qq", return_value={"text": "hi", "sender": {}})
    @patch("agent.main.run_turn")
    def test_complete_event_reply_used_from_files(self, mock_run_turn, mock_qq):
        """Event reply must come from files.event_reply, not from parsing messages."""
        files = self._create_files()
        event = self._mock_event()
        rule = self._mock_rule(event)
        store = self._setup_store(rule)
        mock_ce = self._make_change_event_mock(event, rule)

        def fake_run_turn(client, f, messages, user, **kwargs):
            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu1", "name": "complete_event",
                 "input": {"reply": "Explicit final answer"}}
            ]})
            f.event_complete = True
            f.event_reply = "Explicit final answer"

        mock_run_turn.side_effect = fake_run_turn

        with patch("agent.event_runtime.change_event", mock_ce):
            from agent.event_runtime import run_event
            run_event(MagicMock(), files, store, 1, "test-evt-001", "tok123",
                      emit=self._emit, read=lambda p: "", automatic=True)

        self.assertTrue(files.event_complete)
        self.assertEqual(event.get("reply"), "Explicit final answer")
        self.assertFalse(any("protocol_recovery_failed" in m for m in self.emitted))
        self.assertFalse(any("protocol_recovery_failed" in m for m in self.emitted))



    @patch("agent.automation_qq.read_event_qq", return_value={"text": "hi", "sender": {}})
    @patch("agent.main.run_turn")
    def test_normal_call_for_user_resumes_directly(self, mock_run_turn, mock_qq):
        """Normal call_for_user (no recovery): answer flows directly to next Agent turn."""
        files = self._create_files()
        event = self._mock_event()
        rule = self._mock_rule(event)
        store = self._setup_store(rule)
        mock_ce = self._make_change_event_mock(event, rule)

        captured_users = []

        def fake_run_turn(client, f, messages, user, **kwargs):
            captured_users.append(user)
            n = len(captured_users)
            if n == 1:
                # First turn: agent calls call_for_user directly
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "call_for_user",
                     "input": {"prompt": "What color?"}}
                ]})
                f.event_complete = False
                f.call_for_user_active = True
                f.call_for_user_prompt = "What color?"
            else:
                # Second turn: agent completes after user answer
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu2", "name": "complete_event",
                     "input": {"reply": "Color is blue"}}
                ]})
                f.event_complete = True
                f.event_reply = "Color is blue"

        mock_run_turn.side_effect = fake_run_turn

        def unexpected_read(prompt):
            raise AssertionError(
                "call_for_user answer must resume Agent directly; "
                "Runtime must not enter waiting_feedback"
            )

        class FakeRequestManager:
            def create_request(self, request_id, event_id, rule_id, session, kind, prompt):
                return {"id": request_id}
            def get_request(self, request_id):
                return {"status": "waiting"}
            def submit_answer(self, request_id, answer, source):
                return "success"
            def mark_invalid(self, request_id):
                return None

        request_manager = FakeRequestManager()

        with patch("agent.event_runtime.change_event", mock_ce), \
             patch("agent.event_runtime._wait_for_user_request", lambda rm, rid, kb, emit: {"status": "answered", "answer": "blue", "source": "terminal"}):
            from agent.event_runtime import run_event
            run_event(
                MagicMock(), files, store, 1, "test-evt-001", "tok123",
                emit=self._emit, read=unexpected_read, automatic=True,
                request_manager=request_manager,
            )

        self.assertEqual(len(captured_users), 2)
        self.assertEqual(captured_users[1], "blue",
                         "Second Agent turn must receive the call_for_user answer")
        self.assertTrue(files.event_complete)
        self.assertEqual(event.get("reply"), "Color is blue")


class FakeKeyboard:
    """Test keyboard that returns pre-programmed key sequences."""

    def __init__(self, keys):
        self._keys = list(keys)
        self._idx = 0

    def has_key(self):
        return self._idx < len(self._keys)

    def read_key(self):
        if self._idx >= len(self._keys):
            raise IndexError("No more keys")
        ch = self._keys[self._idx]
        self._idx += 1
        return ch


class TestWaitForUserRequest(unittest.TestCase):
    """Tests for _wait_for_user_request with FakeKeyboard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test_requests.sqlite3"

    def test_terminal_enter_submits_answer(self):
        """Typing 'hi' + Enter submits answer via SQLite."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")
        kb = FakeKeyboard(["h", "i", "\r"])

        result = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "hi")
        self.assertEqual(result["source"], "terminal")
        self.assertEqual(mgr.get_request("r1")["status"], "answered")

    def test_feishu_answer_terminates_wait(self):
        """Feishu answer in SQLite terminates wait without keyboard input."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")
        mgr.submit_answer("r1", "feishu answer", "feishu")
        kb = FakeKeyboard([])  # no keys pressed

        result = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "feishu answer")
        self.assertEqual(result["source"], "feishu")

    def test_feishu_answers_while_typing(self):
        """Feishu answer arrives mid-typing; partial input is discarded."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")

        # Simulate: type "hel", then Feishu answers before next key
        class PartialKeyboard:
            def __init__(self):
                self._keys = iter(["h", "e", "l"])
                self._feishu_after = 3
                self._count = 0
            def has_key(self):
                self._count += 1
                if self._count > self._feishu_after:
                    return False
                return True
            def read_key(self):
                return next(self._keys)

        kb = PartialKeyboard()
        # Feishu answers after 3 keystrokes
        mgr.submit_answer("r1", "A", "feishu")

        result = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "A")
        self.assertEqual(result["source"], "feishu")

    def test_cancelled_request_ends_wait(self):
        """Cancelled request in SQLite terminates wait."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")
        mgr.mark_cancelled("r1")
        kb = FakeKeyboard([])

        result = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0)
        self.assertEqual(result["status"], "cancelled")

    def test_invalid_request_ends_wait(self):
        """Invalidated request in SQLite terminates wait."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")
        mgr.mark_invalid("r1")
        kb = FakeKeyboard([])

        result = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0)
        self.assertEqual(result["status"], "invalid")

    def test_ctrl_c_returns_exit(self):
        """Ctrl+C returns exit status."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")
        kb = FakeKeyboard(["\x03"])

        result = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0)
        self.assertEqual(result["status"], "exit")

    def test_backspace_removes_last_char(self):
        """Backspace removes the last character from buffer."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")
        # Type "ab", backspace, "c", Enter
        kb = FakeKeyboard(["a", "b", "\b", "c", "\r"])

        result = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "ac")

    def test_first_response_wins_feishu_then_terminal(self):
        """Feishu answers first, terminal submit is already_resolved."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")
        mgr.submit_answer("r1", "F", "feishu")
        # Keyboard still has keys but DB already answered
        kb = FakeKeyboard(["T", "\r"])

        result = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "F")
        self.assertEqual(result["source"], "feishu")

    def test_feishu_answer_in_thread(self):
        """_wait_for_user_request in a thread wakes when Feishu writes to SQLite."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")

        # Keyboard with no keys (simulates user not typing)
        kb = FakeKeyboard([])
        result_box = []

        def wait_in_thread():
            r = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0.05)
            result_box.append(r)

        t = threading.Thread(target=wait_in_thread, daemon=True)
        t.start()

        time.sleep(0.2)
        mgr.submit_answer("r1", "feishu thread", "feishu")

        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "_wait_for_user_request should have returned")
        self.assertEqual(len(result_box), 1)
        self.assertEqual(result_box[0]["answer"], "feishu thread")
        self.assertEqual(result_box[0]["source"], "feishu")

    def test_empty_enter_redisplays_prompt(self):
        """Empty Enter does not submit; re-prompts."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_user_request

        mgr = UserRequestManager(path=self.db_path)
        mgr.create_request("r1", "evt1", 1, "s1", "call_for_user", "Q?")
        # Enter (empty), then "ok", Enter
        kb = FakeKeyboard(["\r", "o", "k", "\r"])

        result = _wait_for_user_request(mgr, "r1", kb, lambda t: None, poll_interval=0)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "ok")


class TestGetLatestWaitingForChat(unittest.TestCase):
    """Tests for UserRequestManager.get_latest_waiting_for_chat."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.sqlite3"
        self.mgr = UserRequestManager(path=self.db_path)

    def test_finds_correct_chat(self):
        self.mgr.create_request("r1", "ev1", 1, "s1", "call_for_user", "Q1")
        self.mgr.update_feishu_message("r1", "m1", "chat1")
        self.mgr.create_request("r2", "ev1", 1, "s1", "call_for_user", "Q2")
        self.mgr.update_feishu_message("r2", "m2", "chat2")

        result = self.mgr.get_latest_waiting_for_chat("chat1")
        self.assertIsNotNone(result)
        self.assertEqual(result["request_id"], "r1")

    def test_ignores_other_chat(self):
        self.mgr.create_request("r1", "ev1", 1, "s1", "call_for_user", "Q1")
        self.mgr.update_feishu_message("r1", "m1", "chat1")

        result = self.mgr.get_latest_waiting_for_chat("chat2")
        self.assertIsNone(result)

    def test_ignores_answered(self):
        self.mgr.create_request("r1", "ev1", 1, "s1", "call_for_user", "Q1")
        self.mgr.update_feishu_message("r1", "m1", "chat1")
        self.mgr.submit_answer("r1", "done", "feishu")

        result = self.mgr.get_latest_waiting_for_chat("chat1")
        self.assertIsNone(result)

    def test_ignores_cancelled(self):
        self.mgr.create_request("r1", "ev1", 1, "s1", "call_for_user", "Q1")
        self.mgr.update_feishu_message("r1", "m1", "chat1")
        self.mgr.mark_cancelled("r1")

        result = self.mgr.get_latest_waiting_for_chat("chat1")
        self.assertIsNone(result)

    def test_returns_latest_when_multiple(self):
        import time
        self.mgr.create_request("r1", "ev1", 1, "s1", "call_for_user", "Q1")
        self.mgr.update_feishu_message("r1", "m1", "chat1")
        time.sleep(0.01)
        self.mgr.create_request("r2", "ev1", 1, "s1", "call_for_user", "Q2")
        self.mgr.update_feishu_message("r2", "m2", "chat1")

        result = self.mgr.get_latest_waiting_for_chat("chat1")
        self.assertEqual(result["request_id"], "r2")




if __name__ == "__main__":
    unittest.main()
