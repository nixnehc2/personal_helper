"""Tests for call_for_user dual-channel (terminal + feishu) flow."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from agent.user_requests import UserRequestManager


class TestDualChannelFlow(unittest.TestCase):
    """Test the dual-channel flow using real UserRequestManager."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.mgr = UserRequestManager(self.db)

    def test_create_request_waiting(self):
        req = self.mgr.create_request("rc1", "ev1", 1, "sess1", "call_for_user", "question?")
        self.assertEqual(req["status"], "waiting")
        self.assertEqual(req["prompt"], "question?")

    def test_terminal_answer_wins(self):
        self.mgr.create_request("rc1", "ev1", 1, "sess1", "call_for_user", "Q?")
        result = self.mgr.submit_answer("rc1", "terminal answer", "terminal")
        self.assertEqual(result, "success")
        result2 = self.mgr.submit_answer("rc1", "feishu answer", "feishu")
        self.assertEqual(result2, "already_resolved")
        req = self.mgr.get_request("rc1")
        self.assertEqual(req["answer_source"], "terminal")

    def test_feishu_answer_wins(self):
        self.mgr.create_request("rc1", "ev1", 1, "sess1", "call_for_user", "Q?")
        result = self.mgr.submit_answer("rc1", "feishu answer", "feishu")
        self.assertEqual(result, "success")
        result2 = self.mgr.submit_answer("rc1", "terminal answer", "terminal")
        self.assertEqual(result2, "already_resolved")
        req = self.mgr.get_request("rc1")
        self.assertEqual(req["answer_source"], "feishu")

    def test_concurrent_answers(self):
        self.mgr.create_request("rc1", "ev1", 1, "sess1", "call_for_user", "Q?")
        results = []
        barrier = threading.Barrier(2, timeout=5)
        def submit(source, answer):
            barrier.wait()
            results.append(self.mgr.submit_answer("rc1", answer, source))
        t1 = threading.Thread(target=submit, args=("terminal", "T"))
        t2 = threading.Thread(target=submit, args=("feishu", "F"))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(sum(1 for r in results if r == "success"), 1)
        self.assertEqual(sum(1 for r in results if r == "already_resolved"), 1)

    def test_requests_do_not_crosstalk(self):
        self.mgr.create_request("rA", "ev1", 1, "s1", "call_for_user", "QA")
        self.mgr.create_request("rB", "ev1", 1, "s1", "call_for_user", "QB")
        self.mgr.submit_answer("rA", "answer A", "terminal")
        self.assertEqual(self.mgr.get_request("rB")["status"], "waiting")
        self.mgr.submit_answer("rB", "answer B", "feishu")
        self.assertEqual(self.mgr.get_request("rA")["answer"], "answer A")
        self.assertEqual(self.mgr.get_request("rB")["answer"], "answer B")

    def test_agent_exit_invalidates_request(self):
        self.mgr.create_request("rc1", "ev1", 1, "sess1", "call_for_user", "Q?")
        self.mgr.mark_invalid("rc1")
        req = self.mgr.get_request("rc1")
        self.assertEqual(req["status"], "invalid")
        result = self.mgr.submit_answer("rc1", "answer", "feishu")
        self.assertEqual(result, "already_resolved")

    def test_pending_event_preserved_after_agent_exit(self):
        self.mgr.create_request("rc1", "ev1", 1, "sess1", "call_for_user", "Q?")
        self.mgr.mark_invalid("rc1")
        req = self.mgr.get_request("rc1")
        self.assertEqual(req["event_id"], "ev1")
        self.assertEqual(req["status"], "invalid")

    def test_user_cancels_event(self):
        self.mgr.create_request("rc1", "ev1", 1, "sess1", "call_for_user", "Q?")
        self.mgr.mark_cancelled("rc1")
        req = self.mgr.get_request("rc1")
        self.assertEqual(req["status"], "cancelled")

    def test_retry_creates_new_request(self):
        self.mgr.create_request("r1", "ev1", 1, "s1", "call_for_user", "Q?")
        self.mgr.mark_invalid("r1")
        r2 = self.mgr.create_request("r2", "ev1", 1, "s1", "call_for_user", "Q?")
        self.assertEqual(r2["request_id"], "r2")
        self.assertEqual(r2["status"], "waiting")
        self.assertEqual(self.mgr.get_request("r1")["status"], "invalid")

    def test_feishu_info_update(self):
        self.mgr.create_request("rc1", "ev1", 1, "sess1", "call_for_user", "Q?")
        self.mgr.update_feishu_message("rc1", "msg123", "chat456")
        req = self.mgr.get_request("rc1")
        self.assertEqual(req["feishu_message_id"], "msg123")
        self.assertEqual(req["feishu_chat_id"], "chat456")

    def test_invalidate_waiting_for_event_batch(self):
        self.mgr.create_request("r1", "ev1", 1, "s1", "call_for_user", "Q1")
        self.mgr.create_request("r2", "ev1", 1, "s1", "call_for_user", "Q2")
        self.mgr.create_request("r3", "ev2", 1, "s1", "call_for_user", "Q3")
        self.mgr.invalidate_waiting_for_event("ev1")
        self.assertEqual(self.mgr.get_request("r1")["status"], "invalid")
        self.assertEqual(self.mgr.get_request("r2")["status"], "invalid")
        self.assertEqual(self.mgr.get_request("r3")["status"], "waiting")


class TestFeishuClientInit(unittest.TestCase):
    """Test FeishuClient initialization with mocked dependencies."""

    def test_feishu_client_creates_without_start(self):
        from agent.feishu_client import FeishuClient
        mgr = UserRequestManager(":memory:")
        client = FeishuClient(mgr)
        self.assertIsNone(client._http_client)
        self.assertFalse(client._running)

    @patch("agent.feishu_client.get_credentials", side_effect=ValueError("missing"))
    def test_create_feishu_client_handles_error(self, mock_creds):
        from agent.event_runtime import _create_feishu_client
        mgr = UserRequestManager(":memory:")
        result = _create_feishu_client(mgr)
        self.assertIsNone(result)


# _wait_for_answer and _start_terminal_input_thread have been replaced by
# _wait_for_user_request (tested in test_automation_protocol_violation.py).
# The legacy _wait_for_answer is kept for backward compatibility only.


if __name__ == "__main__":
    unittest.main()
