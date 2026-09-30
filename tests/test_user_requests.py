"""Tests for UserRequestManager: creation, atomic submit, invalidation."""
import tempfile
import threading
import unittest
from pathlib import Path

from agent.user_requests import UserRequestManager


class TestCreateRequest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.mgr = UserRequestManager(self.db)

    def test_create_returns_waiting(self):
        req = self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "prompt?")
        self.assertEqual(req["status"], "waiting")
        self.assertEqual(req["request_id"], "r1")
        self.assertEqual(req["prompt"], "prompt?")

    def test_create_stores_in_db(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q1")
        stored = self.mgr.get_request("r1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "waiting")


class TestSubmitAnswer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.mgr = UserRequestManager(self.db)

    def test_terminal_first_wins(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        result = self.mgr.submit_answer("r1", "answer T", "terminal")
        self.assertEqual(result, "success")
        req = self.mgr.get_request("r1")
        self.assertEqual(req["status"], "answered")
        self.assertEqual(req["answer"], "answer T")
        self.assertEqual(req["answer_source"], "terminal")

    def test_feishu_rejected_after_terminal(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        self.mgr.submit_answer("r1", "answer T", "terminal")
        result = self.mgr.submit_answer("r1", "answer F", "feishu")
        self.assertEqual(result, "already_resolved")
        req = self.mgr.get_request("r1")
        self.assertEqual(req["answer"], "answer T")

    def test_feishu_first_wins(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        result = self.mgr.submit_answer("r1", "answer F", "feishu")
        self.assertEqual(result, "success")
        req = self.mgr.get_request("r1")
        self.assertEqual(req["answer_source"], "feishu")

    def test_terminal_rejected_after_feishu(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        self.mgr.submit_answer("r1", "answer F", "feishu")
        result = self.mgr.submit_answer("r1", "answer T", "terminal")
        self.assertEqual(result, "already_resolved")

    def test_double_submit_same_source_rejected(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        self.mgr.submit_answer("r1", "first", "terminal")
        result = self.mgr.submit_answer("r1", "second", "terminal")
        self.assertEqual(result, "already_resolved")

    def test_nonexistent_request_rejected(self):
        result = self.mgr.submit_answer("no_such", "answer", "terminal")
        self.assertEqual(result, "already_resolved")


class TestConcurrentSubmit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.mgr = UserRequestManager(self.db)

    def test_concurrent_submit_only_one_wins(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        results = []
        barrier = threading.Barrier(2, timeout=5)
        def submit(source, answer):
            barrier.wait()
            results.append(self.mgr.submit_answer("r1", answer, source))
        t1 = threading.Thread(target=submit, args=("terminal", "T"))
        t2 = threading.Thread(target=submit, args=("feishu", "F"))
        t1.start(); t2.start()
        t1.join(); t2.join()
        success_count = sum(1 for r in results if r == "success")
        already_count = sum(1 for r in results if r == "already_resolved")
        self.assertEqual(success_count, 1)
        self.assertEqual(already_count, 1)
        req = self.mgr.get_request("r1")
        self.assertEqual(req["status"], "answered")


class TestNoCrossTalk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.mgr = UserRequestManager(self.db)

    def test_answer_a_does_not_affect_b(self):
        self.mgr.create_request("rA", "e1", 1, "s1", "call_for_user", "QA")
        self.mgr.create_request("rB", "e1", 1, "s1", "call_for_user", "QB")
        self.mgr.submit_answer("rA", "answer A", "terminal")
        req_b = self.mgr.get_request("rB")
        self.assertEqual(req_b["status"], "waiting")


class TestInvalidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.mgr = UserRequestManager(self.db)

    def test_mark_invalid(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        self.mgr.mark_invalid("r1")
        req = self.mgr.get_request("r1")
        self.assertEqual(req["status"], "invalid")

    def test_submit_on_invalid_rejected(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        self.mgr.mark_invalid("r1")
        result = self.mgr.submit_answer("r1", "answer", "terminal")
        self.assertEqual(result, "already_resolved")

    def test_invalidate_waiting_for_event(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q1")
        self.mgr.create_request("r2", "e1", 1, "s1", "call_for_user", "Q2")
        self.mgr.create_request("r3", "e2", 1, "s1", "call_for_user", "Q3")
        self.mgr.invalidate_waiting_for_event("e1")
        self.assertEqual(self.mgr.get_request("r1")["status"], "invalid")
        self.assertEqual(self.mgr.get_request("r2")["status"], "invalid")
        self.assertEqual(self.mgr.get_request("r3")["status"], "waiting")

    def test_mark_invalid_does_not_affect_answered(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        self.mgr.submit_answer("r1", "done", "terminal")
        self.mgr.mark_invalid("r1")
        req = self.mgr.get_request("r1")
        self.assertEqual(req["status"], "answered")


class TestCancellation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.mgr = UserRequestManager(self.db)

    def test_mark_cancelled(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        self.mgr.mark_cancelled("r1")
        req = self.mgr.get_request("r1")
        self.assertEqual(req["status"], "cancelled")

    def test_submit_on_cancelled_rejected(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        self.mgr.mark_cancelled("r1")
        result = self.mgr.submit_answer("r1", "answer", "terminal")
        self.assertEqual(result, "already_resolved")


class TestRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.mgr = UserRequestManager(self.db)

    def test_new_request_after_invalid(self):
        self.mgr.create_request("r1", "e1", 1, "s1", "call_for_user", "Q?")
        self.mgr.mark_invalid("r1")
        req2 = self.mgr.create_request("r2", "e1", 1, "s1", "call_for_user", "Q?")
        self.assertEqual(req2["status"], "waiting")
        self.assertEqual(req2["request_id"], "r2")
        self.assertEqual(self.mgr.get_request("r1")["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
