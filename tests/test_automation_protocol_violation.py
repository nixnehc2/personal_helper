"""Tests for Automation protocol violation detection and recovery."""
import contextlib
import tempfile
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
        """Protocol violation -> recovery -> call_for_user -> user answer -> complete_event."""
        files = self._create_files()
        event = self._mock_event()
        rule = self._mock_rule(event)
        store = self._setup_store(rule)
        mock_ce = self._make_change_event_mock(event, rule)

        call_count = [0]
        seen_user_call_ids = []

        def fake_run_turn(client, f, messages, user, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                messages.append({"role": "assistant", "content": [
                    {"type": "text", "text": "Need more details."}
                ]})
                f.event_complete = False
            elif call_count[0] == 2:
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "call_for_user",
                     "input": {"prompt": "Should I finalize now?"}}
                ]})
                f.event_complete = False
                f.call_for_user_active = True
                f.call_for_user_prompt = "Should I finalize now?"
            else:
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu2", "name": "complete_event",
                     "input": {"reply": "Final result"}}
                ]})
                f.event_complete = True
                f.event_reply = "Final result"

        mock_run_turn.side_effect = fake_run_turn

        class FakeRequestManager:
            def create_request(self, request_id, event_id, rule_id, session, kind, prompt):
                seen_user_call_ids.append(request_id)
                return {"id": request_id}
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
                emit=self._emit, read=lambda p: "", automatic=True,
                request_manager=request_manager,
            )

        self.assertEqual(call_count[0], 3)
        self.assertEqual(len(seen_user_call_ids), 1)
        self.assertTrue(files.event_complete)
        self.assertEqual(event.get("reply"), "Final result")
        self.assertFalse(any("protocol_recovery_failed" in m for m in self.emitted))
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



if __name__ == "__main__":
    unittest.main()
