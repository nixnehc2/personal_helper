from contextlib import redirect_stdout
from email import policy
from email.message import EmailMessage
from io import StringIO
import imaplib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent import email_workflow
from agent.email_index import EmailIndex
from agent.main import main, parse_tool_command, run_turn
from agent.tools import FileTools, TOOLS
from test_email_memory import ScriptClient, create


CONFIG = dict(EMAIL_ACCOUNT="user@example.test", EMAIL_AUTH_CODE="secret", EMAIL_IMAP_HOST="imap.example.test")
SOURCE = dict(account="user@example.test", host="imap.example.test", folder="INBOX", uidvalidity="100")


class DownloadMailbox:
    def __init__(self, raw):
        self.raw = raw
        self.fetches = []
        self.validity = b"100"
        self.return_uid = b"42"
        self.size_offset = 0
        self.fail = False

    def login(self, account, password): return "OK", []
    def select(self, folder, readonly):
        assert folder == "INBOX" and readonly
        return "OK", [b"2"]
    def response(self, name): return name, [self.validity]
    def logout(self): return "BYE", []
    def uid(self, command, uid, query):
        self.fetches.append((command, uid, query))
        assert (command, uid, query) == ("fetch", "42", "(UID RFC822.SIZE BODY.PEEK[])")
        if self.fail:
            raise imaplib.IMAP4.abort("secret")
        header = b"1 (UID " + self.return_uid + b" RFC822.SIZE " + str(len(self.raw) + self.size_offset).encode() + b" BODY[] {999}"
        return "OK", [(header, self.raw), b")"]


class ImportClient:
    model = "test"

    def __init__(self, batches=(), agent_call=False):
        self.processor = ScriptClient(batches)
        self.agent_call = agent_call
        self.outer_calls = 0
        self.import_calls = 0
        self.result = None

    def complete(self, system, messages, tools):
        # Every previous tool_use must already have matching results. In
        # particular, nested EML processing cannot reuse an outstanding call.
        for i, message in enumerate(messages):
            if message["role"] == "assistant" and isinstance(message["content"], list):
                ids = {b["id"] for b in message["content"] if b["type"] == "tool_use"}
                if ids:
                    assert i + 1 < len(messages), "unanswered tool_use sent to model"
                    assert ids == {b["tool_use_id"] for b in messages[i + 1]["content"]}
        if "This is an email import task" in system:
            self.import_calls += 1
            assert not {"email", "import_email"} & {s["name"] for s in tools}
            return self.processor.complete(system, messages, tools)
        assert self.agent_call
        self.outer_calls += 1
        if self.outer_calls == 1:
            assert "import_email" in {s["name"] for s in tools}
            return dict(content=[dict(type="tool_use", id="import", name="import_email", input={"id": 1523})], stop_reason="tool_use")
        self.result = json.loads(messages[-1]["content"][0]["content"])
        return dict(content=[dict(type="text", text="done")], stop_reason="end_turn")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "memory"
        self.root.mkdir()
        (self.root / "AGENT.md").write_text("Test protocol", encoding="utf-8")
        self.files = FileTools(self.root, lambda changes: {}, lambda *args: "no")
        self.index = EmailIndex(self.base / "data/email/index.json")
        with self.index.locked():
            self.index.write(dict(version=1, next_id=1523, emails=[]))
        self.index.merge(SOURCE, [dict(imap_uid="42", message_id="<one>", subject="附件", **{"from": "a@example.test"}, date=""),
                                  dict(imap_uid="43", message_id="<two>", subject="Other", **{"from": "b@example.test"}, date="")])
        message = EmailMessage(policy=policy.SMTP)
        message["Message-ID"] = "<one>"
        message["Subject"] = "附件"
        message.set_content("项目资料正文")
        message.add_attachment(bytes(range(256)), maintype="application", subtype="octet-stream", filename="附件.bin")
        self.raw = message.as_bytes()
        self.mailbox = DownloadMailbox(self.raw)
        self.transport = patch("agent.email_import.imaplib.IMAP4_SSL", return_value=self.mailbox).start()
        self.addCleanup(patch.stopall)
        patch("agent.email_import.INDEX_PATH", self.index.path).start()
        patch("agent.email_import.load_config", return_value=CONFIG).start()

    def execute(self, client=None, id=1523):
        with self.files.email_context(client or ImportClient(), emit=lambda _: None):
            return self.files.execute("import_email", {"id": id})

    def chat(self, text, client):
        output = StringIO()
        with patch("sys.argv", ["agent.main"]), patch("agent.main.FileTools", return_value=self.files), \
                patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), \
                patch("agent.main.Client", return_value=client), \
                patch("builtins.input", side_effect=[text, "/exit"]), redirect_stdout(output):
            self.assertEqual(main(), 0)
        return output.getvalue()

    def test_direct_command_and_agent_use_same_tool_and_processor(self):
        with patch.object(self.files, "import_email", wraps=self.files.import_email) as tool, \
                patch("agent.email_workflow.process_eml", wraps=email_workflow.process_eml) as process:
            output = self.chat("/import_email 1523", ImportClient())
            self.assertIn("processed", output)
            tool.assert_called_once_with(id=1523)
            process.assert_called_once()
            self.assertEqual(process.call_args.args[0], self.index.path.parent / "raw/1523.eml")
        self.assertTrue(self.index.get(1523)["imported"])
        # A separate clean fixture state exercises the Agent caller's first import.
        with self.index.locked():
            data = self.index.read()
            data["emails"][0].update(imported=False, imported_at=None)
            self.index.write(data)
        client = ImportClient(agent_call=True)
        with patch.object(self.files, "import_email", wraps=self.files.import_email) as tool, \
                patch("agent.email_workflow.process_eml", wraps=email_workflow.process_eml) as process:
            run_turn(client, self.files, [], "请导入 1523", emit=lambda _: None)
            tool.assert_called_once_with(id=1523)
            process.assert_called_once()
        self.assertTrue(client.result["imported"])
        self.assertTrue(self.index.get(1523)["imported"])
        self.assertIsNone(self.files._email_context)

    def test_local_email_and_indexed_import_share_processor(self):
        path = self.base / "local.eml"
        path.write_bytes(self.raw)
        with patch("agent.email_workflow.process_eml", wraps=email_workflow.process_eml) as process:
            self.chat(f'/email "{path}"', ImportClient())
            self.assertEqual(process.call_count, 1)
            self.assertFalse(self.index.get(1523)["imported"])
            self.assertEqual(self.execute()["status"], "processed")
            self.assertEqual(process.call_count, 2)

    def test_complete_attachment_bytes_saved_and_state_set_after_processing(self):
        original = email_workflow.process_eml
        def inspect(path, *args, **kwargs):
            self.assertEqual(path.read_bytes(), self.raw)
            self.assertFalse(self.index.get(1523)["imported"])
            return original(path, *args, **kwargs)
        client = ImportClient([[create("projects/imported.md", "candidate")]])
        with patch("agent.email_workflow.process_eml", side_effect=inspect):
            result = self.execute(client)
        self.assertTrue(result["imported"])
        self.assertIsNotNone(self.index.get(1523)["imported_at"])
        self.assertFalse(self.index.get(1524)["imported"])
        self.assertEqual(len(self.mailbox.fetches), 1)
        self.assertEqual(Path(result["eml_path"]).read_bytes(), self.raw)
        self.assertEqual((self.files.workspace_root / result["raw"]["path"]).read_bytes(), self.raw)
        self.assertTrue((self.files.workspace_root / "projects/imported.md").exists())
        self.assertFalse((self.root / "projects/imported.md").exists())

    def test_duplicate_never_downloads_or_calls_processor(self):
        self.execute()
        before = self.index.path.read_bytes()
        with patch("agent.email_import.download_eml") as download, patch("agent.email_workflow.process_eml") as process:
            result = self.execute()
        self.assertEqual(result["note"], "该邮件已经导入")
        download.assert_not_called()
        process.assert_not_called()
        self.assertEqual(self.index.path.read_bytes(), before)

    def test_invalid_id_and_arguments_never_connect(self):
        for id in (999, 0, -1, True, "1523"):
            self.assertIn("error", self.execute(id=id))
        for args in ({"id": 1523, "force": True}, {"id": 1523, "imported": True}, {}):
            self.assertIn("error", self.files.execute("import_email", args))
        self.transport.assert_not_called()
        spec = next(t for t in TOOLS if t["name"] == "import_email")
        self.assertEqual(spec["input_schema"]["properties"], {"id": {"type": "integer"}})

    def test_download_failure_preserves_index(self):
        before = self.index.path.read_bytes()
        self.mailbox.fail = True
        result = self.execute()
        self.assertIn("IMAP", result["error"])
        self.assertNotIn("secret", result["error"])
        self.assertEqual(self.index.path.read_bytes(), before)
        self.assertFalse((self.index.path.parent / "raw/1523.eml").exists())

    def test_failed_agent_retries_cached_bytes_and_does_not_skip_archived_raw(self):
        broken = ImportClient()
        broken.complete = Mock(side_effect=RuntimeError("model failed"))
        self.assertIn("error", self.execute(broken))
        self.assertFalse(self.index.get(1523)["imported"])
        self.assertTrue((self.index.path.parent / "raw/1523.eml").exists())
        self.transport.reset_mock()
        client = ImportClient([[create("projects/retry.md", "recovered")]])
        result = self.execute(client)
        self.transport.assert_not_called()
        self.assertGreater(client.import_calls, 0)
        self.assertTrue(result["imported"])
        self.assertTrue((self.files.workspace_root / "projects/retry.md").exists())

    def test_memory_tool_failure_does_not_mark_imported(self):
        result = self.execute(ImportClient([[create("../escape.md", "bad")]]))
        self.assertIn("error", result)
        self.assertFalse(self.index.get(1523)["imported"])
        self.assertIsNone(self.index.get(1523)["imported_at"])

    def test_uidvalidity_uid_message_id_and_size_mismatch_rejected(self):
        for field, value in (("validity", b"200"), ("return_uid", b"43"), ("size_offset", 1),
                             ("raw", self.raw.replace(b"<one>", b"<wrong>"))):
            with self.subTest(field=field), patch.object(self.mailbox, field, value):
                self.assertIn("error", self.execute())
                self.assertFalse(self.index.get(1523)["imported"])
        self.assertFalse((self.index.path.parent / "raw/1523.eml").exists())

    def test_corrupt_cache_is_downloaded_again(self):
        with patch("agent.email_workflow.process_eml", side_effect=RuntimeError("failed")):
            self.execute()
        (self.index.path.parent / "raw/1523.eml").write_bytes(b"corrupted")
        self.assertTrue(self.execute()["imported"])
        self.assertEqual(len(self.mailbox.fetches), 2)

    def test_index_write_failure_preserves_false_and_other_rows(self):
        before = self.index.path.read_bytes()
        with patch.object(EmailIndex, "write", side_effect=OSError("disk full")):
            self.assertIn("error", self.execute())
        self.assertEqual(self.index.path.read_bytes(), before)
        self.assertFalse(self.index.path.with_suffix(".lock").exists())

    def test_concurrent_import_and_recursive_tool_calls_are_rejected(self):
        directory = self.index.path.parent / "raw"
        directory.mkdir()
        lock = directory / "1523.lock"
        lock.touch()
        self.assertIn("正在导入", self.execute()["error"])
        self.transport.assert_not_called()
        lock.unlink()
        result = self.execute(ImportClient([[("import_email", {"id": 1524})]]))
        self.assertIn("error", result)
        self.assertFalse(self.index.get(1523)["imported"])
        self.assertEqual(len(self.mailbox.fetches), 1)

    def test_command_parser(self):
        self.assertEqual(parse_tool_command("/import_email 1523"), ("import_email", {"id": 1523}))
        for text in ("/import_email", "/import_email abc", "/import_email -1", "/import_email 0", "/import_email 1 2", "/import_email 1 --force"):
            with self.assertRaises(ValueError): parse_tool_command(text)

    def test_sync_during_processing_preserves_new_records_and_import_state(self):
        original = email_workflow.process_eml
        def sync_then_process(*args, **kwargs):
            self.index.merge(SOURCE, [dict(imap_uid="44", message_id="<three>", subject="New", **{"from": ""}, date="")])
            return original(*args, **kwargs)
        with patch("agent.email_workflow.process_eml", side_effect=sync_then_process):
            self.assertTrue(self.execute()["imported"])
        self.assertEqual(len(self.index.read()["emails"]), 3)
        self.assertFalse(self.index.get(1525)["imported"])

    def test_changed_identity_during_processing_is_not_marked_imported(self):
        original = email_workflow.process_eml
        def change_then_process(*args, **kwargs):
            self.index.merge(dict(SOURCE, uidvalidity="200"), [dict(imap_uid="99", message_id="<one>", subject="Same", **{"from": ""}, date="")])
            return original(*args, **kwargs)
        with patch("agent.email_workflow.process_eml", side_effect=change_then_process):
            self.assertIn("索引发生变化", self.execute()["error"])
        self.assertFalse(self.index.get(1523)["imported"])

    def test_raw_write_failure_and_wrong_account_do_not_start_processing(self):
        with patch("agent.email_import.atomic_bytes", side_effect=OSError("disk full")), \
                patch("agent.email_workflow.process_eml") as process:
            self.assertIn("error", self.execute())
            process.assert_not_called()
        self.assertFalse(self.index.get(1523)["imported"])
        self.transport.reset_mock()
        with patch("agent.email_import.load_config", return_value=dict(CONFIG, EMAIL_ACCOUNT="other@example.test")):
            self.assertIn("配置与该邮件索引不一致", self.execute()["error"])
        self.transport.assert_not_called()

    def test_review_no_retains_temporary_but_processing_completes(self):
        client = ImportClient([[create("projects/review.md", "candidate")], [("commit_memory_changes", {})]])
        result = self.execute(client)
        self.assertTrue(result["imported"])
        self.assertFalse((self.root / "projects/review.md").exists())
        self.assertTrue((self.files.workspace_root / "projects/review.md").exists())

    def test_agent_local_email_tool_does_not_escape_memory_root(self):
        outside = self.base / "external.eml"
        outside.write_bytes(self.raw)
        with self.files.email_context(ImportClient(), emit=lambda _: None):
            self.assertIn("error", self.files.execute("email", {"path": str(outside)}))


if __name__ == "__main__":
    unittest.main()
