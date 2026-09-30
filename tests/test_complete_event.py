"""Regression tests for complete_event parameter validation and error isolation."""
import tempfile
import unittest
from pathlib import Path
from agent.tools import FileTools

class CompleteEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "memory"
        root.mkdir()
        (root / "AGENT.md").write_text("Test", encoding="utf-8")
        self.files = FileTools(root)
        self.addCleanup(self.files.policy.close)
        self.files.event_session = True

    def test_normal_reply_succeeds(self):
        result = self.files._execute("complete_event", {"reply": "done"})
        self.assertEqual(result["status"], "event_complete")
        self.assertTrue(self.files.event_complete)
        self.assertEqual(self.files.event_reply, "done")

    def test_message_alias_succeeds(self):
        result = self.files._execute("complete_event", {"message": "done"})
        self.assertEqual(result["status"], "event_complete")
        self.assertTrue(self.files.event_complete)
        self.assertEqual(self.files.event_reply, "done")

    def test_unknown_param_reports_reply_error(self):
        result = self.files._execute("complete_event", {"foo": "bar"})
        self.assertIn("error", result)
        self.assertIn("reply", result["error"])
        self.assertNotIn("Memory", result["error"])
        # Should not misleadingly mention email
        err_lower = result["error"].lower()
        self.assertNotIn("email", err_lower)

    def test_empty_reply_fails(self):
        result = self.files._execute("complete_event", {"reply": ""})
        self.assertIn("error", result)
        self.assertIn("reply", result["error"])

    def test_whitespace_reply_fails(self):
        result = self.files._execute("complete_event", {"reply": "   "})
        self.assertIn("error", result)
        self.assertIn("reply", result["error"])

    def test_non_string_reply_fails(self):
        result = self.files._execute("complete_event", {"reply": 123})
        self.assertIn("error", result)
        self.assertIn("reply", result["error"])

    def test_active_memory_blocks_complete_event(self):
        self.files.write_memory("projects/tmp_note.md", "note")
        self.assertTrue(self.files.policy._active())
        result = self.files._execute("complete_event", {"reply": "done"})
        self.assertIn("error", result)
        self.assertIn("Memory", result["error"])
        self.files.commit_memory_changes()
        self.assertFalse(self.files.policy._active())
        result2 = self.files._execute("complete_event", {"reply": "done"})
        self.assertEqual(result2["status"], "event_complete")

    def test_active_memory_blocks_then_discard_allows(self):
        self.files.write_memory("projects/tmp_note.md", "note")
        self.assertTrue(self.files.policy._active())
        result = self.files._execute("complete_event", {"reply": "done"})
        self.assertIn("error", result)
        self.assertIn("Memory", result["error"])
        self.files.policy.discard(explicit=True)
        result2 = self.files._execute("complete_event", {"reply": "done"})
        self.assertEqual(result2["status"], "event_complete")

    def test_pending_email_blocks_complete_event(self):
        self.files.pending_email_send = {"id": 1, "to": "test@example.com", "subject": "Test"}
        result = self.files._execute("complete_event", {"reply": "done"})
        self.assertIn("error", result)
        # Error mentions email send request (Chinese)
        self.assertIn("\u90ae\u4ef6", result["error"])
        self.files.pending_email_send = None
        result2 = self.files._execute("complete_event", {"reply": "done"})
        self.assertEqual(result2["status"], "event_complete")

    def test_complete_event_rejected_after_call_for_user(self):
        self.files._execute("call_for_user", {"prompt": "question"})
        result = self.files._execute("complete_event", {"reply": "done"})
        self.assertIn("error", result)
        self.assertIn("call_for_user", result["error"])

    def test_call_for_user_rejected_after_complete_event(self):
        self.files._execute("complete_event", {"reply": "done"})
        result = self.files._execute("call_for_user", {"prompt": "question"})
        self.assertIn("error", result)
        self.assertIn("complete_event", result["error"])

if __name__ == "__main__":
    unittest.main()
