from contextlib import closing, redirect_stdout
from datetime import timedelta
from io import StringIO
import json
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import test_automations as fixtures
from agent.automation_checker import check_once, loop_lock
from agent.automation_consumer import consume_once
from agent.main import main, parse_tool_command
from agent.tools import FileTools, TOOLS


def answer(text="请检查申请材料。"):
    return call("complete_event", reply=text)


def call(name, **arguments):
    return dict(content=[dict(type="tool_use", id="tool1", name=name, input=arguments)], stop_reason="tool_use")


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        fixtures.AutomationsTests.setUp(self)
        self.root = self.path.parent / "memory"
        self.root.mkdir(parents=True)
        (self.root / "AGENT.md").write_text("Test protocol", encoding="utf-8")
        self.confirm = Mock(return_value="no")
        self.files = FileTools(self.root, lambda _: {}, self.confirm)
        self.client = Mock()
        self.client.complete.return_value = answer()
        self.create()

    def create(self, kind="once"):
        config = dict(at=fixtures.NOW.isoformat()) if kind == "once" else dict(interval_seconds=120)
        self.store.manage("create", rule=fixtures.timed(kind, **config))
        check_once(self.store, fixtures.NOW)

    def row(self, id=1):
        return self.store.manage("get", id)["rule"]

    def consume(self, emit=lambda _: None):
        return consume_once(self.client, self.files, self.store, emit, read=lambda _: "/exit")

    def test_persist_before_display_and_once_completion(self):
        self.files.active_email_draft_id = 27
        output = []
        def emit(text):
            output.append(text)
            if text.startswith("[automation #"):
                self.assertEqual(self.row()["pending_events"][0]["reply"], "请检查申请材料。")
                self.assertEqual(self.row()["status"], "active")
        result = self.consume(emit)
        self.assertEqual(len(result["delivered"]), 1)
        self.assertEqual(sum("请检查申请材料。" in t for t in output), 1)
        self.assertEqual(self.row()["pending_events"], [])
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.files.active_email_draft_id, 27)
        self.consume()
        self.client.complete.assert_called_once()

    def test_model_failure_is_finite_and_other_events_continue(self):
        self.create()
        self.client.complete.side_effect = [RuntimeError("offline"), answer(), answer()]
        result = self.consume()
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(len(result["delivered"]), 1)
        event = self.row()["pending_events"][0]
        self.assertIsNone(event["reply"])
        self.assertEqual(event["attempts"], 1)
        self.assertEqual(event["last_error"], "offline")
        self.assertEqual(len(self.consume()["delivered"]), 1)
        self.assertEqual(self.client.complete.call_count, 3)

    def test_display_failure_then_fresh_process_uses_saved_reply(self):
        def emit(text):
            if text.startswith("[automation #"):
                raise OSError("terminal unavailable")
        self.assertEqual(len(self.consume(emit)["failed"]), 1)
        event = self.row()["pending_events"][0]
        self.assertEqual(event["reply"], "请检查申请材料。")
        self.assertEqual(event["last_error"], "terminal unavailable")
        code = """from agent.automation_consumer import consume_once
from agent.automations import AutomationStore
import sys,json
result=consume_once(None,None,AutomationStore(sys.argv[1],{}),emit=lambda text: None)
print(json.dumps(result))
"""
        result = json.loads(subprocess.check_output([sys.executable, "-c", code, str(self.path)], text=True))
        self.assertEqual(len(result["delivered"]), 1)
        self.assertEqual(self.row()["status"], "completed")
        self.client.complete.assert_called_once()

    def test_remove_and_completion_roll_back_together(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER fail_completion BEFORE UPDATE ON automations
                WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'completion failed'); END""")
        self.assertEqual(len(self.consume()["failed"]), 1)
        self.assertEqual(self.row()["status"], "active")
        self.assertIsNotNone(self.row()["pending_events"][0]["reply"])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER fail_completion")
        self.consume()
        self.client.complete.assert_called_once()

    def test_save_failure_does_not_display_final_reply(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER fail_save BEFORE UPDATE ON automations
                WHEN json_extract(NEW.pending_events,'$[0].reply') IS NOT NULL
                BEGIN SELECT RAISE(ABORT,'save failed'); END""")
        output = []
        self.assertEqual(len(self.consume(output.append)["failed"]), 1)
        self.assertFalse(any("请检查申请材料。" in text for text in output))
        self.assertIsNone(self.row()["pending_events"][0]["reply"])

    def test_interval_append_during_model_call_preserved(self):
        self.store.manage("cancel", 1)
        self.create("interval")
        def complete(*args):
            check_once(self.store, fixtures.NOW + timedelta(seconds=120))
            return answer()
        self.client.complete.side_effect = complete
        self.consume()
        self.assertEqual(self.row(2)["status"], "active")
        self.assertEqual(len(self.row(2)["pending_events"]), 1)
        self.assertIsNone(self.row(2)["pending_events"][0]["reply"])
        self.client.complete.assert_called_once()

    def test_schedule_edit_during_call_does_not_complete_new_schedule(self):
        def complete(*args):
            config = fixtures.timed("once", at=(fixtures.NOW + timedelta(hours=1)).isoformat())["trigger_config"]
            self.store.manage("update", 1, dict(trigger_config=config))
            return answer()
        self.client.complete.side_effect = complete
        self.consume()
        self.assertEqual(self.row()["status"], "active")
        self.assertIsNotNone(self.row()["next_check_at"])
        check_once(self.store, fixtures.NOW + timedelta(hours=1))
        self.assertEqual(len(self.row()["pending_events"]), 1)

    def test_snapshot_context_and_confirmation_preserved(self):
        snapshot = self.row()["pending_events"][0]["content"]
        self.store.manage("update", 1, dict(content="changed instruction"))
        self.client.complete.side_effect = [call("write_memory", path="pending/note.md", content="candidate"),
                                            call("commit_memory_changes"), dict(content=[dict(type="text", text="请反馈修改意见")], stop_reason="end_turn")]
        self.consume()
        self.confirm.assert_called_once()
        self.assertFalse((self.root / "pending/note.md").exists())
        self.assertFalse(self.files.policy.changes)
        self.assertTrue(self.row()["pending_events"][0]["suspended"])
        self.assertIsNone(self.row()["pending_events"][0]["reply"])
        user = self.client.complete.call_args_list[0].args[1][0]["content"]
        self.assertIn(snapshot, user)
        self.assertNotIn("changed instruction", user)

    def test_paused_cancelled_and_consumer_lock(self):
        self.store.manage("pause", 1)
        self.consume()
        self.client.complete.assert_not_called()
        self.store.manage("resume", 1)
        with loop_lock(self.store, "consumer"):
            with self.assertRaisesRegex(RuntimeError, "消费者"):
                self.consume()
        def complete(*args):
            self.store.manage("cancel", 1)
            return answer()
        self.client.complete.side_effect = complete
        output = []
        self.consume(output.append)
        self.assertFalse(any(t.startswith("[automation #") for t in output))
        self.assertEqual(self.row()["pending_events"], [])

    def test_pause_during_call_keeps_reply_for_resume(self):
        def complete(*args):
            self.store.manage("pause", 1)
            return answer()
        self.client.complete.side_effect = complete
        self.consume()
        self.assertIsNotNone(self.row()["pending_events"][0]["reply"])
        self.store.manage("resume", 1)
        self.consume()
        self.client.complete.assert_called_once()

    def test_send_request_waits_for_console_feedback(self):
        self.files.confirm_email = Mock(return_value=False)
        self.client.complete.side_effect = [call("send_email", draft_id=5)]
        snapshot = dict(id=5, to="a@example.test", subject="test", body="body")
        with patch("agent.email_send.request_email_send", return_value=snapshot), patch("agent.email_send.smtp_deliver") as smtp:
            self.consume()
        self.files.confirm_email.assert_not_called()
        smtp.assert_not_called()

    def test_chat_command_is_not_model_tool_or_chat_history(self):
        self.assertEqual(parse_tool_command("/automation consume"), ("automation_consume", {}))
        with self.assertRaises(ValueError):
            parse_tool_command("/automation consume 1")
        self.assertNotIn("automation_consume", [t["name"] for t in TOOLS])
        self.client.model = "test"
        inputs = []
        def complete(system, messages, tools):
            inputs.append(json.loads(json.dumps(messages)))
            return answer() if any(t["name"] == "complete_event" for t in tools) else dict(content=[dict(type="text", text="chat")], stop_reason="end_turn")
        self.client.complete.side_effect = complete
        with patch("agent.automation_consumer.AutomationStore", return_value=self.store), \
             patch("sys.argv", ["agent.main", "--root", str(self.root)]), \
             patch("agent.main.FileTools", return_value=self.files), \
             patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), \
             patch("agent.main.Client", return_value=self.client), \
             patch("builtins.input", side_effect=["hello", "/automation consume", "bye", "/exit"]), redirect_stdout(StringIO()):
            self.assertEqual(main(start_background_consumer=False, automation_db_path=self.path), 0)
        self.assertEqual(len(inputs[1]), 1)
        self.assertNotIn("hello", str(inputs[1]))
        self.assertNotIn("schedule:1:", str(inputs[2]))
