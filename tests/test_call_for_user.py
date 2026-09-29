"""Tests for call_for_user tool in Automation event sessions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent.main import run_turn
from agent.tools import FileTools, TOOLS


def answer(text="完成"):
    return dict(content=[dict(type="text", text=text)], stop_reason="end_turn")


def reply(text):
    return dict(content=[dict(type="text", text=text)], stop_reason="end_turn")


def call(name, args):
    return dict(content=[dict(type="tool_use", id=f"call_{name}", name=name, input=args)], stop_reason="tool_use")


class CallForUserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "memory"
        root.mkdir()
        (root / "AGENT.md").write_text("Test", encoding="utf-8")
        self.files = FileTools(root)
        self.addCleanup(self.files.policy.close)

    # Test 1: Normal chat does not have call_for_user or complete_event
    def test_normal_session_has_no_event_tools(self):
        specs = self.files.tool_specs
        names = [s["name"] for s in specs]
        self.assertNotIn("call_for_user", names)
        self.assertNotIn("complete_event", names)
        # All normal tools are present
        self.assertIn("automation", names)
        self.assertIn("import_message", names)
        self.assertIn("read_memory", names)

    # Test 2: Automation session has both call_for_user and complete_event
    def test_event_session_has_event_tools(self):
        self.files.event_session = True
        specs = self.files.tool_specs
        names = [s["name"] for s in specs]
        self.assertIn("call_for_user", names)
        self.assertIn("complete_event", names)

    # Test 3: call_for_user terminates the turn
    def test_call_for_user_terminates_turn(self):
        self.files.event_session = True
        client = Mock()
        client.complete.side_effect = [
            call("call_for_user", {"prompt": "几点提醒？"}),
        ]
        result = run_turn(client, self.files, [], "执行事件", emit=lambda _: None)
        self.assertEqual(result["status"], "call_for_user")
        self.assertEqual(result["prompt"], "几点提醒？")
        self.assertTrue(self.files.call_for_user_active)
        self.assertEqual(self.files.call_for_user_prompt, "几点提醒？")

    # Test 4: call_for_user and complete_event are mutually exclusive
    def test_call_for_user_and_complete_event_mutual_exclusion(self):
        self.files.event_session = True
        client = Mock()
        client.complete.return_value = dict(content=[
            dict(type="tool_use", id="c1", name="call_for_user", input={"prompt": "问题"}),
            dict(type="tool_use", id="c2", name="complete_event", input={"reply": "完成"}),
        ], stop_reason="tool_use")
        with self.assertRaises(ValueError) as ctx:
            run_turn(client, self.files, [], "执行事件", emit=lambda _: None)
        self.assertIn("call_for_user", str(ctx.exception))
        self.assertIn("complete_event", str(ctx.exception))
    # Test 5: Normal chat cannot call call_for_user
    def test_normal_chat_rejects_call_for_user(self):
        # call_for_user is not in tool_specs for normal sessions
        # But even if called directly via execute():
        result = self.files.execute("call_for_user", {"prompt": "问题"})
        self.assertIn("error", result)

    # Test 6: call_for_user requires event_session
    def test_call_for_user_requires_event_session(self):
        # Without event_session, call_for_user is not a recognized special case
        result = self.files._execute("call_for_user", {"prompt": "问题"})
        # It should try getattr(self, "call_for_user") which doesn't exist as a method
        self.assertIn("error", result)

    # Test 7: call_for_user with empty prompt is rejected
    def test_call_for_user_empty_prompt_rejected(self):
        self.files.event_session = True
        result = self.files._execute("call_for_user", {"prompt": ""})
        self.assertIn("error", result)
        result2 = self.files._execute("call_for_user", {"prompt": "   "})
        self.assertIn("error", result2)

    # Test 8: call_for_user with extra arguments is rejected
    def test_call_for_user_extra_args_rejected(self):
        self.files.event_session = True
        result = self.files._execute("call_for_user", {"prompt": "问题", "extra": "bad"})
        self.assertIn("error", result)

    # Test 9: Multiple call_for_user in different turns is allowed
    def test_multiple_call_for_user_across_turns(self):
        self.files.event_session = True
        # First call
        result1 = self.files._execute("call_for_user", {"prompt": "问题1"})
        self.assertEqual(result1["status"], "call_for_user")
        self.assertTrue(self.files.call_for_user_active)
        # Simulate turn reset (the event runtime would reset these between turns)
        self.files.call_for_user_active = False
        self.files.call_for_user_prompt = None
        # Second call
        result2 = self.files._execute("call_for_user", {"prompt": "问题2"})
        self.assertEqual(result2["status"], "call_for_user")

    # Test 10: Processing message blocks call_for_user
    def test_processing_message_blocks_call_for_user(self):
        self.files.event_session = True
        self.files.processing_message = True
        result = self.files._execute("call_for_user", {"prompt": "问题"})
        self.assertIn("error", result)

    # Test 11: Read only blocks call_for_user
    def test_read_only_blocks_call_for_user(self):
        self.files.event_session = True
        self.files.read_only = True
        result = self.files._execute("call_for_user", {"prompt": "问题"})
        self.assertIn("error", result)

    # Test 12: call_for_user during import_message (automation-origin)
    def test_call_for_user_available_during_import_in_event_session(self):
        """Automation session -> import_message -> call_for_user should work."""
        self.files.event_session = True
        specs = self.files.tool_specs
        names = [s["name"] for s in specs]
        self.assertIn("call_for_user", names)
        self.assertIn("import_message", names)

    # Test 13: complete_event rejects when call_for_user is active
    def test_complete_event_rejected_after_call_for_user(self):
        self.files.event_session = True
        # First call_for_user
        self.files._execute("call_for_user", {"prompt": "问题"})
        # Then try complete_event in same turn
        result = self.files._execute("complete_event", {"reply": "完成"})
        self.assertIn("error", result)
        self.assertIn("call_for_user", result["error"])

    # Test 14: call_for_user rejects when complete_event is active
    def test_call_for_user_rejected_after_complete_event(self):
        self.files.event_session = True
        # First complete_event
        self.files._execute("complete_event", {"reply": "完成"})
        # Then try call_for_user in same turn
        result = self.files._execute("call_for_user", {"prompt": "问题"})
        self.assertIn("error", result)
        self.assertIn("complete_event", result["error"])


if __name__ == "__main__":
    unittest.main()
