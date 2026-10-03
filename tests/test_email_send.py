import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent.email_drafts import DraftStore
from agent.main import main, parse_tool_command, run_turn, resolve_email_feedback
from agent.tools import FileTools, TOOLS


class FakeSMTP:
    messages = []

    def __init__(self, host, port, *, timeout, context):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        return False

    def login(self, account, password):
        self.account = account
        self.password = password

    def send_message(self, message):
        FakeSMTP.messages.append(message)


def call(name, arguments):
    return dict(content=[dict(type="tool_use", id="call1", name=name, input=arguments)],
                stop_reason="tool_use")


class SendEmailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "memory"
        self.root.mkdir()
        (self.root / "AGENT.md").write_text("Read _INDEX.md first.", encoding="utf-8")
        self.drafts = Path(temporary.name) / "drafts"
        patcher = patch("agent.email_drafts.DRAFTS_PATH", self.drafts)
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeSMTP.messages = []
        smtp = patch("agent.email_send.smtplib.SMTP_SSL", FakeSMTP)
        smtp.start()
        self.addCleanup(smtp.stop)
        self.settings = {"EMAIL_ACCOUNT": "me@qq.com", "EMAIL_AUTH_CODE": "secret",
                         "EMAIL_SMTP_HOST": "smtp.qq.com", "EMAIL_SMTP_PORT": "465"}
        settings = patch("agent.email_send.load_settings", return_value=self.settings)
        settings.start()
        self.addCleanup(settings.stop)

    def files(self, confirm):
        return FileTools(self.root, lambda *_: "no", lambda *_: "no", confirm)

    def request_and_answer(self, files, draft_id, answer):
        result = files.execute("send_email", {"draft_id": draft_id})
        if "error" in result:
            return result
        self.assertEqual(result["status"], "waiting_feedback")
        self.assertEqual(FakeSMTP.messages, [])
        feedback = resolve_email_feedback(files, answer, emit=lambda _: None)
        return json.loads(feedback.split("：", 1)[1])

    def draft(self, **content):
        return DraftStore().save(dict(
            to=content.pop("to", "teacher@example.com"),
            subject=content.pop("subject", "关于周五讨论"),
            body=content.pop("body", "张老师您好，周五下午可以参加讨论。"), **content), None)

    def test_successful_send_marks_snapshot_sent(self):
        draft = self.draft()
        result = self.request_and_answer(self.files(lambda _: True), draft["id"], "yes")
        self.assertNotIn("error", result)
        self.assertEqual(result["status"], "sent")
        self.assertTrue(result["sent_at"])
        self.assertTrue(result["sent_message_id"].startswith("<"))
        message = FakeSMTP.messages[0]
        self.assertEqual((message["From"], message["To"], message["Subject"]),
                         ("me@qq.com", "teacher@example.com", "关于周五讨论"))
        self.assertEqual(message.get_content(), "张老师您好，周五下午可以参加讨论。\n")
        self.assertEqual(DraftStore().read(1)["status"], "sent")

    def test_no_never_connects_or_changes_draft(self):
        draft = self.draft()
        result = self.request_and_answer(self.files(lambda _: False), draft["id"], "no")
        self.assertEqual(result["status"], "cancelled")
        self.assertIn("发送已取消", result["display"])
        self.assertEqual(FakeSMTP.messages, [])
        self.assertEqual(DraftStore().read(1)["status"], "draft")

    def test_smtp_failure_keeps_draft_recoverable(self):
        draft = self.draft()
        def fail(*arguments, **keywords):
            raise OSError("network down")
        with patch("agent.email_send.smtplib.SMTP_SSL", fail):
            result = self.request_and_answer(self.files(lambda _: True), draft["id"], "yes")
        self.assertIn("SMTP 连接或发送失败", result["error"])
        self.assertEqual(DraftStore().read(1)["status"], "draft")

    def test_sent_draft_is_rejected_without_second_confirmation(self):
        draft = self.draft()
        confirmations = []
        files = self.files(lambda value: confirmations.append(value) or True)
        self.request_and_answer(files, draft["id"], "yes")
        result = files.execute("send_email", {"draft_id": draft["id"]})
        self.assertIn("拒绝重复发送", result["error"])
        self.assertEqual(len(confirmations), 0)
        self.assertEqual(len(FakeSMTP.messages), 1)

    def test_missing_recipient_is_rejected_before_confirmation(self):
        draft = self.draft(to=None)
        confirmations = []
        result = self.files(lambda value: confirmations.append(value) or True).execute(
            "send_email", {"draft_id": draft["id"]})
        self.assertIn("缺少有效收件人邮箱", result["error"])
        self.assertEqual(confirmations, [])
        self.assertEqual(FakeSMTP.messages, [])

    def test_confirmation_snapshot_is_frozen(self):
        draft = self.draft()
        files = self.files(lambda _: True)
        files.execute("send_email", {"draft_id": draft["id"]})
        changed = dict(files.pending_email_send, body="changed after request")
        (self.drafts / "1.json").write_text(json.dumps(changed), encoding="utf-8")
        result = resolve_email_feedback(files, "yes", emit=lambda _: None)
        self.assertIn("草稿已变化", result)
        self.assertEqual(FakeSMTP.messages, [])
        self.assertEqual(DraftStore().read(1)["body"], changed["body"])

    def test_agent_and_direct_command_use_same_tool(self):
        self.assertEqual(parse_tool_command("/send_email 12"),
                         ("send_email", {"draft_id": 12}))
        for command in ("/send_email", "/send_email 12 13", "/send_email 0"):
            with self.assertRaises(ValueError):
                parse_tool_command(command)
        self.draft()
        client = Mock(complete=Mock(side_effect=[
            call("send_email", {"draft_id": 1}),
            dict(content=[dict(type="text", text="邮件已发送")], stop_reason="end_turn")]))
        files = self.files(lambda _: True)
        messages = []
        run_turn(client, files, messages, "这封邮件可以发了", emit=lambda _: None)
        result = next(item for message in messages[1:]
                      for item in message["content"]
                      if item.get("type") == "tool_result")
        self.assertFalse(result["is_error"])
        self.assertEqual(json.loads(result["content"])["status"], "waiting_feedback")
        self.assertEqual(FakeSMTP.messages, [])
        client.complete.assert_called_once()
        self.assertIn("send_email", {spec["name"] for spec in TOOLS})

    def test_send_ends_turn_and_skips_remaining_batch_tools(self):
        self.draft()
        files = self.files(lambda _: True)
        client = Mock(complete=Mock(return_value=dict(stop_reason="tool_use", content=[
            dict(type="tool_use", id="send", name="send_email", input=dict(draft_id=1)),
            dict(type="tool_use", id="write", name="write_memory", input=dict(path="pending/skipped.md", content="candidate"))])))
        messages = []
        run_turn(client, files, messages, "send", emit=lambda _: None)
        client.complete.assert_called_once()
        self.assertEqual(len(messages[-1]["content"]), 2)
        self.assertTrue(messages[-1]["content"][1]["is_error"])
        self.assertFalse(files.policy._active())
        self.assertEqual(FakeSMTP.messages, [])
        self.assertIn("error", files.execute("send_email", {"draft_id": 1}))

    def test_waiting_input_releases_execution_and_yes_reacquires(self):
        import threading
        from agent.scheduler import OSLock, RUNTIME
        self.draft()
        files = self.files(lambda _: True)
        waiting, resume = threading.Event(), threading.Event()
        errors = []
        answers = iter(["/send_email 1", "yes", "/exit"])
        def read(prompt):
            answer = next(answers)
            if answer == "yes":
                waiting.set()
                if not resume.wait(10):
                    raise RuntimeError("other Agent could not execute")
            return answer
        def smtp(message, settings, connect=None):
            lock = OSLock(RUNTIME / "execution.lock")
            acquired = lock.acquire()
            lock.release()
            self.assertFalse(acquired)
            FakeSMTP.messages.append(message)
        def worker():
            try:
                main(start_background_consumer=False, automation_db_path=self.root.parent / "automations.sqlite3")
            except BaseException as error:
                errors.append(error)
        with patch("sys.argv", ["agent.main"]), patch("agent.main.FileTools", return_value=files), patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), patch("agent.main.Client", return_value=Mock(model="test")), patch("agent.event_runtime.BackgroundConsumer"), patch("builtins.input", side_effect=read), patch("agent.email_send.smtp_deliver", side_effect=smtp), redirect_stdout(StringIO()):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(waiting.wait(5))
                self.assertEqual(FakeSMTP.messages, [])
                lock = OSLock(RUNTIME / "execution.lock")
                self.assertTrue(lock.acquire())
                lock.release()
                other = FileTools(self.root)
                with other.policy.scheduler.turn(lambda _: None):
                    self.assertFalse(other.policy._active())
                other.policy.close()
            finally:
                resume.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(DraftStore().read(1)["status"], "sent")

    def test_invalid_input_no_and_exit_keep_draft(self):
        self.draft()
        files = self.files(lambda _: True)
        files.execute("send_email", {"draft_id": 1})
        self.assertIsNone(resolve_email_feedback(files, "sure send it", emit=lambda _: None))
        self.assertIsNotNone(files.pending_email_send)
        resolve_email_feedback(files, "no", emit=lambda _: None)
        self.assertIsNone(files.pending_email_send)
        self.assertEqual(FakeSMTP.messages, [])
        other = FileTools(self.root)
        with other.policy.scheduler.turn(lambda _: None):
            pass
        other.policy.close()
        self.assertEqual(DraftStore().read(1)["status"], "draft")
        self.assertNotIn("send_email_confirmed", {t["name"] for t in TOOLS})
        self.assertIn("error", files.execute("send_email_confirmed", {"draft_id": 1}))

    def test_manual_approval_exit_and_eof_do_not_send(self):
        self.draft()
        for ending in ("/exit", EOFError(), KeyboardInterrupt()):
            with self.subTest(ending=type(ending).__name__):
                files = self.files(lambda _: True)
                with patch("sys.argv", ["agent.main"]), patch("agent.main.FileTools", return_value=files), patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), patch("agent.main.Client", return_value=Mock(model="test")), patch("agent.event_runtime.BackgroundConsumer"), patch("builtins.input", side_effect=["/send_email 1", ending]), redirect_stdout(StringIO()):
                    self.assertEqual(main(start_background_consumer=False, automation_db_path=self.root.parent / "automations.sqlite3"), 0)
                self.assertIsNone(files.pending_email_send)
                self.assertEqual(FakeSMTP.messages, [])
                self.assertEqual(DraftStore().read(1)["status"], "draft")

    def test_memory_transaction_must_end_before_email_wait(self):
        self.draft()
        files = self.files(lambda _: True)
        try:
            files.write_memory("pending/test.md", "candidate")
            self.assertIn("error", files.execute("send_email", {"draft_id": 1}))
            self.assertIsNone(files.pending_email_send)
        finally:
            files.policy.close()

    def test_default_memory_commit_never_reads_input(self):
        from agent.scheduler import OSLock
        files = FileTools(self.root)
        self.addCleanup(files.policy.close)
        with patch("builtins.input", side_effect=AssertionError("must not prompt")), redirect_stdout(StringIO()):
            with files.policy.scheduler.turn(lambda _: None):
                files.write_memory("self/test.md", "synthetic preference")
                self.assertFalse((self.root / "self/test.md").exists())
                self.assertEqual(files.commit_memory_changes()["status"], "committed")
        self.assertEqual((self.root / "self/test.md").read_text(), "synthetic preference")
        self.assertFalse(files.policy._active())
        lock = OSLock(self.root / ".memory-owner.lock")
        self.assertTrue(lock.acquire())
        lock.release()

    def test_slash_command_calls_same_tool_and_runtime_confirmation(self):
        self.draft()
        client = Mock(model="test")
        files = self.files(lambda _: True)
        with patch("sys.argv", ["agent.main"]), \
                patch("agent.main.FileTools", return_value=files), \
                patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), \
                patch("agent.main.Client", return_value=client), \
                patch("builtins.input", side_effect=["/send_email 1", "yes", "/exit"]), \
                patch.object(files, "execute", wraps=files.execute) as execute, \
                redirect_stdout(StringIO()):
            self.assertEqual(main(start_background_consumer=False, automation_db_path=self.root.parent / "automations.sqlite3"), 0)
        execute.assert_called_once_with("send_email", {"draft_id": 1})
        self.assertEqual(DraftStore().read(1)["status"], "sent")


if __name__ == "__main__":
    unittest.main()
