"""Entry-point integration tests use a fake mailbox and a private test index."""
from contextlib import redirect_stdout
from functools import partial
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent import email_index
from agent.main import main as chat_main, run_turn
from agent.tools import FileTools
from test_email_index import CONFIG, Mailbox


class EmailEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "memory"
        self.root.mkdir()
        (self.root / "AGENT.md").write_text("Test protocol", encoding="utf-8")
        self.path = Path(self.tmp.name) / "data/email/index.json"
        self.files = FileTools(self.root, lambda changes: {}, lambda *args: "no")
        self.mailbox = Mailbox()
        self.mailbox.headers = dict(Mailbox.headers)
        transport = patch("agent.email_index.imaplib.IMAP4_SSL", return_value=self.mailbox)
        transport.start()
        self.addCleanup(transport.stop)
        # Spy on the one production core while routing its storage to a temp directory.
        core = patch("agent.email_index.update_email_index",
                     wraps=partial(email_index.update_email_index, CONFIG, self.path))
        self.core = core.start()
        self.addCleanup(core.stop)

    def chat(self, command="/update_email"):
        output = StringIO()
        client = Mock(model="test")
        with patch("sys.argv", ["agent.main"]), \
                patch("agent.main.FileTools", return_value=self.files), \
                patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), \
                patch("agent.main.Client", return_value=client), \
                patch("builtins.input", side_effect=[command, "/exit"]), \
                patch.object(self.files, "execute", wraps=self.files.execute) as execute, \
                redirect_stdout(output):
            self.assertEqual(chat_main(), 0)
            execute.assert_called_once_with("update_email", {})
        client.complete.assert_not_called()
        return output.getvalue()

    def agent(self):
        responses = [dict(content=[dict(type="tool_use", id="sync", name="update_email", input={})],
                          stop_reason="tool_use"),
                     dict(content=[dict(type="text", text="同步完成")], stop_reason="end_turn")]
        def complete(system, messages, specs):
            spec = next(s for s in specs if s["name"] == "update_email")
            self.assertEqual(spec["input_schema"]["properties"], {})
            return responses.pop(0)
        messages = []
        run_turn(SimpleNamespace(complete=complete), self.files, messages, "同步邮箱", emit=lambda _: None)
        result = next(m["content"][0] for m in messages
                      if isinstance(m["content"], list) and m["content"][0]["type"] == "tool_result")
        return json.loads(result["content"]), result["is_error"]

    def cli(self):
        output = StringIO()
        with redirect_stdout(output):
            code = email_index.main()
        return code, output.getvalue()

    def test_slash_command_and_compatibility_spellings_use_tool(self):
        for command in ("/update_email", "update_email", "update_email()"):
            with self.subTest(command=command):
                self.assertIn("共 2 封", self.chat(command))
        self.assertEqual(self.core.call_count, 3)

    def test_agent_loop_receives_sync_result(self):
        result, is_error = self.agent()
        self.assertFalse(is_error)
        self.assertEqual((result["total"], result["added"]), (2, 2))
        self.assertTrue(all(not row["imported"] for row in result["emails"]))
        self.assertIn("未导入", result["table"])
        self.core.assert_called_once_with()
        self.assertEqual(self.files.policy.changes, {})

    def test_cli_uses_core_and_reports_success(self):
        code, output = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("共 2 封，新增 2 封", output)
        self.core.assert_called_once_with()

    def test_cross_entrypoint_sync_preserves_ids_and_import_state(self):
        self.chat()
        data = email_index.EmailIndex(self.path).read()
        data["emails"][0].update(imported=True, imported_at="2026-09-24")
        self.path.write_text(json.dumps(data), encoding="utf-8")
        result, is_error = self.agent()
        self.assertFalse(is_error)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["emails"], data["emails"])
        code, output = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("新增 0 封", output)
        self.assertEqual(email_index.EmailIndex(self.path).read(), data)
        self.assertEqual(self.core.call_count, 3)

    def test_failure_through_each_entrypoint_keeps_index(self):
        self.chat()
        before = self.path.read_bytes()
        self.mailbox.fail = True
        result, is_error = self.agent()
        self.assertTrue(is_error)
        self.assertIn("IMAP 获取邮件头失败", result["error"])
        self.assertNotIn("private", result["error"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("IMAP 获取邮件头失败", self.chat())
        self.assertEqual(self.path.read_bytes(), before)
        code, output = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("IMAP 获取邮件头失败", output)
        self.assertEqual(self.path.read_bytes(), before)

    def test_tool_cannot_accept_agent_supplied_index_state(self):
        self.chat()
        before = self.path.read_bytes()
        self.core.reset_mock()
        for arguments in ({"id": 99}, {"imported": True}, {"index_path": str(self.path)},
                          {"emails": []}):
            self.assertIn("error", self.files.execute("update_email", arguments))
        self.core.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("error", self.files.execute("create_file", {
            "path": "../data/email/index.json", "content": "{}"}))
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
