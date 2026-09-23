import imaplib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.email_index import EmailIndex, update_email
from agent.tools import FileTools, TOOLS

CONFIG = dict(EMAIL_ACCOUNT="test@example.com", EMAIL_AUTH_CODE="private")


class Mailbox:
    headers = {b"1": b"Message-ID: <one>\r\nSubject: =?utf-8?b?5Lit5paH?=\r\nFrom: a@example.com\r\n\r\n",
               b"2": b"Message-ID: <two>\r\nSubject: \r\n\r\n"}
    validity = b"100"
    fail = False
    malformed = False

    def login(self, *args): return "OK", []
    def select(self, folder, readonly):
        assert readonly
        return "OK", [b"2"]
    def response(self, name): return name, [self.validity]
    def logout(self): return "BYE", []
    def uid(self, command, *args):
        if command == "search": return "OK", [b" ".join(self.headers)]
        assert args[1] == "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])"
        if self.fail and b"2" in args[0].split(b","):
            raise imaplib.IMAP4.abort("private")
        return "OK", [(b"1 (UID " + uid + b" BODY[HEADER.FIELDS] {10}", self.headers[uid])
                      for uid in args[0].split(b",") if not (self.malformed and uid == b"2")]


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "email/index.json"
        self.mailbox = Mailbox()
        self.mailbox.headers = dict(Mailbox.headers)
        self.mock = patch("agent.email_index.imaplib.IMAP4_SSL", return_value=self.mailbox)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def sync(self): return update_email(CONFIG, self.path)

    def test_first_repeat_new_and_imported_state(self):
        first = self.sync()
        self.assertEqual([r["id"] for r in first["emails"]], [1, 2])
        self.assertTrue(all(not r["imported"] and r["imported_at"] is None for r in first["emails"]))
        self.assertEqual(self.sync()["added"], 0)
        data = EmailIndex(self.path).read()
        data["emails"][0].update(imported=True, imported_at="2026-09-24")
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.mailbox.headers[b"3"] = b"Message-ID: <three>\r\nSubject: special | <>& \"\r\n\r\n"
        result = self.sync()
        self.assertEqual([r["id"] for r in result["emails"]], [1, 2, 3])
        self.assertTrue(result["emails"][0]["imported"])
        self.assertEqual(result["emails"][0]["imported_at"], "2026-09-24")
        self.assertEqual(result["emails"][0]["subject"], "中文")
        self.assertEqual(result["emails"][1]["subject"], "")
        self.assertIn("已导入", result["table"])
        self.assertIn("未导入", result["table"])

    def test_disconnect_preserves_exact_file_and_redacts_error(self):
        self.sync()
        before = self.path.read_bytes()
        self.mailbox.fail = True
        with self.assertRaisesRegex(ValueError, "IMAP 获取邮件头失败") as error:
            self.sync()
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(before, self.path.read_bytes())

    def test_parse_failure_skipped(self):
        self.mailbox.malformed = True
        result = self.sync()
        self.assertEqual(result["skipped_uids"], ["2"])
        self.assertEqual(result["total"], 1)

    def test_uidvalidity_and_message_id(self):
        self.sync()
        self.mailbox.validity = b"200"
        self.mailbox.headers = {b"8": Mailbox.headers[b"1"], b"1": b"Message-ID: <new>\r\n\r\n"}
        result = self.sync()
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["emails"][0]["imap_uid"], "8")
        self.assertEqual(result["emails"][0]["id"], 1)

    def test_missing_message_id_deduplicates_by_uid(self):
        self.mailbox.headers = {b"1": b"Subject: none\r\n\r\n"}
        self.sync()
        self.assertEqual(self.sync()["total"], 1)

    def test_atomic_write_failure_and_corrupt_index(self):
        self.sync()
        before = self.path.read_bytes()
        with patch("agent.email_index.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError): self.sync()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_suffix(".lock").exists())
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "索引格式损坏"): self.sync()
        self.assertEqual(self.path.read_text(), "broken")

    def test_empty_mailbox_initializes_and_lock_prevents_overwrite(self):
        self.mailbox.headers = {}
        self.assertEqual(self.sync()["total"], 0)
        self.path.with_suffix(".lock").touch()
        with self.assertRaisesRegex(ValueError, "正在同步"): self.sync()

    def test_tool_registered_without_arguments(self):
        tool = next(t for t in TOOLS if t["name"] == "update_email")
        self.assertEqual(tool["input_schema"]["properties"], {})

    def test_login_failure_does_not_create_index_or_expose_secret(self):
        with patch.object(self.mailbox, "login", side_effect=imaplib.IMAP4.error("private")):
            with self.assertRaisesRegex(ValueError, "登录") as error: self.sync()
        self.assertNotIn("private", str(error.exception))
        self.assertFalse(self.path.exists())

    def test_different_accounts_do_not_share_uid_or_message_id(self):
        self.sync()
        result = update_email(dict(CONFIG, EMAIL_ACCOUNT="other@example.com"), self.path)
        self.assertEqual(result["total"], 4)
        self.assertEqual([r["id"] for r in result["emails"]], [1, 2, 3, 4])

    def test_batches_and_bounded_agent_output(self):
        self.mailbox.headers = {str(i).encode(): f"Message-ID: <{i}>\r\n\r\n".encode() for i in range(1, 206)}
        result = self.sync()
        self.assertEqual(result["total"], 205)
        with patch("agent.email_index.update_email", return_value=result):
            displayed = FileTools.update_email(object())
        self.assertTrue(displayed["truncated"])
        self.assertEqual(len(displayed["emails"]), 100)
        self.assertEqual(len(EmailIndex(self.path).read()["emails"]), 205)


if __name__ == "__main__":
    unittest.main()
