from contextlib import closing, redirect_stdout
from email import policy
from email.message import EmailMessage
from io import StringIO
import imaplib
import json
import sqlite3
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
        # particular, import tools must answer the existing call before continuing.
        for i, message in enumerate(messages):
            if message["role"] == "assistant" and isinstance(message["content"], list):
                ids = {b["id"] for b in message["content"] if b["type"] == "tool_use"}
                if ids:
                    assert i + 1 < len(messages), "unanswered tool_use sent to model"
                    assert ids == {b["tool_use_id"] for b in messages[i + 1]["content"]}
        if self.agent_call and self.outer_calls == 0:
            self.outer_calls += 1
            return dict(content=[dict(type="tool_use", id="import", name="import_email", input={"id":1523})], stop_reason="tool_use")
        if self.agent_call and isinstance(messages[-1]['content'],list):
            self.result=json.loads(messages[-1]['content'][0]['content'])
        self.import_calls += 1
        return self.processor.complete(system,messages,tools)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.files.policy.close())
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
            self.assertEqual(main(start_background_consumer=False, automation_db_path=self.base / "automations.sqlite3"), 0)
        return output.getvalue()

    def test_direct_and_agent_import_share_loader_without_nested_turn(self):
        from agent.messages.importing import load_message_for_import
        with patch('agent.messages.importing.load_message_for_import',wraps=load_message_for_import) as load:
            self.assertIn('processed',self.chat('/import_email 1523',ImportClient()))
            load.assert_called_once()
        with self.index.locked():
            data=self.index.read(); data['emails'][0].update(imported=False,imported_at=None); self.index.write(data)
        client=ImportClient(agent_call=True)
        with patch('agent.main.run_turn',wraps=run_turn) as turn:
            turn(client,self.files,[],'请导入 1523',emit=lambda _:None)
            self.assertEqual(turn.call_count,1)
        self.assertEqual(client.result['status'],'loaded')
        self.assertFalse(client.result['imported'])
        self.assertTrue(self.index.get(1523)['imported'])

    def test_chat_isolates_store_and_never_creates_background_resources(self):
        from agent.automations import AutomationStore
        path = self.base / "automations.sqlite3"
        with patch("agent.automations.AutomationStore", wraps=AutomationStore) as store, \
                patch("agent.event_runtime.BackgroundConsumer") as consumer, \
                patch("agent.event_runtime.launch") as launch, \
                patch("agent.event_runtime.subprocess.Popen") as popen:
            self.chat("/import_email 1523", ImportClient())
        store.assert_called_once_with(path)
        consumer.assert_not_called()
        consumer.return_value.start.assert_not_called()
        consumer.return_value.close.assert_not_called()
        launch.assert_not_called()
        popen.assert_not_called()
        self.assertTrue(self.index.get(1523)["imported"])

    def test_chat_preserves_pending_event_in_isolated_database(self):
        from agent.automations import AutomationStore
        from test_automations import NOW, timed
        path = self.base / "automations.sqlite3"
        store = AutomationStore(path, {}, lambda: NOW)
        rule_id = store.manage("create", rule=timed())["rule"]["id"]
        pending = json.dumps([dict(event_id="test:pending", attempts=3,
                                   active_session=None, pid=None)])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE automations SET pending_events=? WHERE id=?", (pending, rule_id))
        before = store.manage("get", rule_id)["rule"]
        with patch("agent.event_runtime.launch") as launch:
            self.chat("/import_email 1523", ImportClient())
        launch.assert_not_called()
        self.assertEqual(store.manage("get", rule_id)["rule"], before)

    def test_main_defaults_start_and_close_consumer_with_default_store(self):
        # Mock both runtime dependencies: verify production defaults without opening a real DB.
        with patch("sys.argv", ["agent.main"]), \
                patch("agent.main.FileTools", return_value=self.files), \
                patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), \
                patch("agent.main.Client", return_value=Mock(model="test")), \
                patch("agent.automations.AutomationStore") as store, \
                patch("agent.event_runtime.BackgroundConsumer") as consumer, \
                patch("builtins.input", side_effect=["/exit"]), redirect_stdout(StringIO()):
            self.assertEqual(main(), 0)
        store.assert_called_once_with(None)
        consumer.assert_called_once_with(self.files, store.return_value)
        consumer.return_value.start.assert_called_once_with()
        consumer.return_value.close.assert_called_once_with()

    def test_local_email_keeps_legacy_processor_only(self):
        path=self.base/'local.eml'; path.write_bytes(self.raw)
        with patch('agent.email_workflow.process_eml',wraps=email_workflow.process_eml) as process:
            self.chat(f'/email "{path}"',ImportClient())
            self.assertEqual(process.call_count,1)
            self.assertFalse(self.index.get(1523)['imported'])
            self.assertEqual(self.execute()['status'],'processed')
            self.assertEqual(process.call_count,1)

    def test_single_email_payload_and_attachment_cache(self):
        client=ImportClient([[create('projects/imported.md','candidate')]])
        original=client.complete
        def inspect(system,messages,tools):
            self.assertFalse(self.index.get(1523)['imported'])
            payload=json.loads(messages[0]['content'].split('\n',1)[1])
            content=payload['external_message']['content']
            self.assertIn('项目资料正文',json.dumps(content,ensure_ascii=False))
            self.assertIn('附件.bin',json.dumps(content,ensure_ascii=False))
            self.assertNotIn('Other',json.dumps(payload))
            return original(system,messages,tools)
        client.complete=inspect
        result=self.execute(client)
        self.assertTrue(result['imported'])
        self.assertFalse(self.index.get(1524)['imported'])
        self.assertEqual((self.index.path.parent/'raw/1523.eml').read_bytes(),self.raw)
        self.assertFalse((self.root/'inbox/email').exists())
        self.assertTrue((self.files.workspace_root/'projects/imported.md').exists())
        self.assertFalse((self.root/'projects/imported.md').exists())

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
        self.assertEqual(result["status"], "processed")
        self.assertTrue((self.files.workspace_root / "projects/retry.md").exists())

    def test_memory_tool_failure_does_not_mark_imported(self):
        result = self.execute(ImportClient([[create("../escape.md", "bad")]]))
        self.assertIn("error", result)
        self.assertFalse(self.index.get(1523)["imported"])
        self.assertIsNone(self.index.get(1523)["imported_at"])

    def test_size_mismatch_downloads_and_imports(self):
        from agent.email_import import download_eml, identity
        self.mailbox.size_offset = 100 - len(self.raw)
        self.assertNotEqual(len(self.raw), 100)
        self.assertEqual(download_eml(identity(self.index.get(1523)), CONFIG), self.raw)
        result = self.execute()
        self.assertEqual(result["status"], "processed")
        self.assertTrue(result["imported"])
        self.assertEqual((self.index.path.parent / "raw/1523.eml").read_bytes(), self.raw)

    def test_matching_size_downloads_unchanged(self):
        from agent.email_import import download_eml, identity
        self.assertEqual(self.mailbox.size_offset, 0)
        self.assertEqual(download_eml(identity(self.index.get(1523)), CONFIG), self.raw)

    def test_uidvalidity_uid_and_message_id_mismatch_rejected(self):
        for field, value in (("validity", b"200"), ("return_uid", b"43"),
                             ("raw", self.raw.replace(b"<one>", b"<wrong>"))):
            with self.subTest(field=field), patch.object(self.mailbox, field, value):
                self.assertIn("error", self.execute())
                self.assertFalse(self.index.get(1523)["imported"])
        self.assertFalse((self.index.path.parent / "raw/1523.eml").exists())

    def test_corrupt_cache_is_downloaded_again(self):
        self.execute(Mock(complete=Mock(side_effect=RuntimeError("failed"))))
        (self.index.path.parent / "raw/1523.eml").write_bytes(b"corrupted")
        self.assertTrue(self.execute()["imported"])
        self.assertEqual(len(self.mailbox.fetches), 2)

    def test_index_write_failure_preserves_false_and_other_rows(self):
        before = self.index.path.read_bytes()
        with patch.object(EmailIndex, "write", side_effect=OSError("disk full")):
            self.assertIn("error", self.execute())
        self.assertEqual(self.index.path.read_bytes(), before)
        self.assertFalse(self.index.path.with_suffix(".lock").exists())

    def test_concurrent_import_rejected_before_network(self):
        directory=self.index.path.parent/'raw'; directory.mkdir()
        lock=directory/'1523.lock'; lock.touch()
        self.assertIn('正在导入',self.execute()['error'])
        self.transport.assert_not_called()
        lock.unlink()

    def test_command_parser(self):
        self.assertEqual(parse_tool_command("/import_email 1523"), ("import_email", {"id": 1523}))
        for text in ("/import_email", "/import_email abc", "/import_email -1", "/import_email 0", "/import_email 1 2", "/import_email 1 --force"):
            with self.assertRaises(ValueError): parse_tool_command(text)

    def test_sync_during_processing_preserves_new_records_and_import_state(self):
        client=ImportClient(); original=client.complete
        def complete(*args):
            self.index.merge(SOURCE,[dict(imap_uid='44',message_id='<three>',subject='New',**{'from':''},date='')])
            return original(*args)
        client.complete=complete
        self.assertTrue(self.execute(client)['imported'])
        self.assertEqual(len(self.index.read()['emails']),3)
        self.assertFalse(self.index.get(1525)['imported'])

    def test_changed_identity_during_processing_is_not_marked_imported(self):
        client=ImportClient(); original=client.complete
        def complete(*args):
            self.index.merge(dict(SOURCE,uidvalidity='200'),[dict(imap_uid='99',message_id='<one>',subject='Same',**{'from':''},date='')])
            return original(*args)
        client.complete=complete
        self.assertIn('索引发生变化',self.execute(client)['error'])
        self.assertFalse(self.index.get(1523)['imported'])

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

    def test_review_no_retains_temporary_and_marks_processed(self):
        client = ImportClient([[create("projects/review.md", "candidate")], [("commit_memory_changes", {})]])
        result = self.execute(client)
        self.assertTrue(result["imported"])
        self.assertEqual(result["status"], "processed")
        self.assertFalse((self.root / "projects/review.md").exists())
        self.assertTrue((self.files.workspace_root / "projects/review.md").exists())
        self.files.policy.discard(explicit=True)
        self.assertTrue(self.index.get(1523)["imported"])
        self.assertFalse((self.index.path.parent / "raw/1523.lock").exists())

    def test_agent_local_email_tool_does_not_escape_memory_root(self):
        outside = self.base / "external.eml"
        outside.write_bytes(self.raw)
        with self.files.email_context(ImportClient(), emit=lambda _: None):
            self.assertIn("error", self.files.execute("email", {"path": str(outside)}))

    def test_unified_query_read_and_import_use_general_agent(self):
        with patch("agent.email_index.INDEX_PATH", self.index.path):
            listing = self.files.execute("list_messages", dict(source="email", conversation="INBOX", limit=2))
            self.assertEqual(listing["count"], 2)
            self.assertEqual({row["source"] for row in listing["messages"]}, {"email"})
            viewed = self.files.execute("read_message", dict(source="email", id=1523))
            self.assertIn("项目资料正文", viewed["display"])
            self.assertFalse(self.index.get(1523)["imported"])
            self.assertFalse((self.root / "inbox/email").exists())
            with self.files.message_context(ImportClient(), emit=lambda _: None), \
                    patch("agent.email_workflow.process_eml", wraps=email_workflow.process_eml) as process:
                result = self.files.execute("import_message", dict(source="email", id=1523))
            self.assertTrue(result["imported"])
            process.assert_not_called()
        self.assertTrue(self.index.get(1523)["imported"])

    def test_unified_email_processed_before_memory_commit(self):
        with patch("agent.email_index.INDEX_PATH", self.index.path), \
                self.files.message_context(ImportClient([[create("projects/shared.md", "candidate")]]), emit=lambda _: None):
            result = self.files.execute("import_message", dict(source="email", id=1523))
        self.assertEqual(result["status"], "processed")
        self.assertEqual(self.execute()["status"], "already_imported")
        self.files.policy.confirm_transaction = lambda *args: "yes"
        self.files.policy.request_commit()
        self.assertTrue(self.index.get(1523)["imported"])


if __name__ == "__main__":
    unittest.main()
