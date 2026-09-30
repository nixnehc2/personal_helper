"""Tests for Automation protocol violation detection and recovery."""
import contextlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agent.event_runtime import classify_automation_turn, MAX_PROTOCOL_RECOVERY_ATTEMPTS


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
             patch("agent.event_runtime._start_terminal_input_thread", lambda read, rm, request_id, answer_event, answer_holder: None), \
             patch("agent.event_runtime._wait_for_answer", lambda rm, request_id, ae, ah, emit: ("yes", "terminal")):
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
             patch("agent.event_runtime._start_terminal_input_thread", lambda read, rm, request_id, answer_event, answer_holder: None), \
             patch("agent.event_runtime._wait_for_answer", lambda rm, request_id, ae, ah, emit: ("blue", "terminal")):
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


class TestWaitForAnswer(unittest.TestCase):
    """Tests for _wait_for_answer with SQLite polling (Feishu wake-up)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test_requests.sqlite3"

    def test_terminal_answer_wakes_immediately(self):
        """Terminal answer sets answer_event -> _wait_for_answer returns immediately."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_answer

        mgr = UserRequestManager(path=self.db_path)
        req_id = "req-1"
        mgr.create_request(req_id, "evt1", 1, "sess1", "call_for_user", "prompt?")

        answer_event = threading.Event()
        answer_holder = {}

        # Simulate terminal: submit_answer + set event
        mgr.submit_answer(req_id, "terminal answer", "terminal")
        answer_holder["answer"] = "terminal answer"
        answer_holder["source"] = "terminal"
        answer_event.set()

        answer, source = _wait_for_answer(mgr, req_id, answer_event, answer_holder, lambda t: None)
        self.assertEqual(answer, "terminal answer")
        self.assertEqual(source, "terminal")

    def test_feishu_answer_discovered_via_sqlite_polling(self):
        """Feishu answer only writes to SQLite (no answer_event.set()).

        _wait_for_answer must discover it by polling the DB.
        """
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_answer

        mgr = UserRequestManager(path=self.db_path)
        req_id = "req-feishu"
        mgr.create_request(req_id, "evt1", 1, "sess1", "call_for_user", "prompt?")

        answer_event = threading.Event()
        answer_holder = {}

        # Simulate Feishu: only write to SQLite, no answer_event.set()
        result = mgr.submit_answer(req_id, "feishu answer", "feishu")
        self.assertEqual(result, "success")

        # _wait_for_answer should discover the answer by polling
        answer, source = _wait_for_answer(mgr, req_id, answer_event, answer_holder, lambda t: None)
        self.assertEqual(answer, "feishu answer")
        self.assertEqual(source, "feishu")
        self.assertTrue(answer_event.is_set(), "answer_event should be set after DB discovery")

    def test_feishu_answer_discovered_in_thread(self):
        """_wait_for_answer running in a thread wakes up when Feishu writes to SQLite."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_answer

        mgr = UserRequestManager(path=self.db_path)
        req_id = "req-thread"
        mgr.create_request(req_id, "evt1", 1, "sess1", "call_for_user", "prompt?")

        answer_event = threading.Event()
        answer_holder = {}
        result_box = []

        def wait_in_thread():
            ans, src = _wait_for_answer(mgr, req_id, answer_event, answer_holder, lambda t: None)
            result_box.append((ans, src))

        t = threading.Thread(target=wait_in_thread, daemon=True)
        t.start()

        # Wait a moment, then submit Feishu answer (no answer_event.set())
        time.sleep(0.3)
        mgr.submit_answer(req_id, "feishu thread answer", "feishu")

        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "_wait_for_answer should have returned")
        self.assertEqual(len(result_box), 1)
        self.assertEqual(result_box[0], ("feishu thread answer", "feishu"))

    def test_first_response_wins_feishu_then_terminal(self):
        """If Feishu answers first, terminal answer is rejected."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_answer

        mgr = UserRequestManager(path=self.db_path)
        req_id = "req-frw"
        mgr.create_request(req_id, "evt1", 1, "sess1", "call_for_user", "prompt?")

        answer_event = threading.Event()
        answer_holder = {}

        # Feishu answers first
        result = mgr.submit_answer(req_id, "F", "feishu")
        self.assertEqual(result, "success")

        # Terminal tries to answer second
        result2 = mgr.submit_answer(req_id, "T", "terminal")
        self.assertEqual(result2, "already_resolved")

        # _wait_for_answer should get the Feishu answer
        answer, source = _wait_for_answer(mgr, req_id, answer_event, answer_holder, lambda t: None)
        self.assertEqual(answer, "F")
        self.assertEqual(source, "feishu")

    def test_first_response_wins_terminal_then_feishu(self):
        """If terminal answers first, Feishu answer is rejected."""
        from agent.user_requests import UserRequestManager
        from agent.event_runtime import _wait_for_answer

        mgr = UserRequestManager(path=self.db_path)
        req_id = "req-frw2"
        mgr.create_request(req_id, "evt1", 1, "sess1", "call_for_user", "prompt?")

        answer_event = threading.Event()
        answer_holder = {}

        # Terminal answers first
        result = mgr.submit_answer(req_id, "T", "terminal")
        self.assertEqual(result, "success")
        answer_holder["answer"] = "T"
        answer_holder["source"] = "terminal"
        answer_event.set()

        # Feishu tries to answer second
        result2 = mgr.submit_answer(req_id, "F", "feishu")
        self.assertEqual(result2, "already_resolved")

        # _wait_for_answer should get the terminal answer
        answer, source = _wait_for_answer(mgr, req_id, answer_event, answer_holder, lambda t: None)
        self.assertEqual(answer, "T")
        self.assertEqual(source, "terminal")


if __name__ == "__main__":
    unittest.main()
