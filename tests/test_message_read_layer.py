"""Tests for the unified Message reading layer (Step 2.1).

Covers:
  - get_message("email", id) returns a correct Message
  - Nonexistent id raises ValueError
  - Unsupported source raises ValueError
  - imported correctly mapped
  - (source, id) semantics: same numeric id, different sources
  - list_messages("email") returns all messages
  - list_messages filter by imported
  - email_locator / email_identity helpers
"""

from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from agent.messages import Message, get_message, list_messages
from agent.messages.email_adapter import email_locator, email_identity
from agent.email_index import EmailIndex


def _make_row(**overrides) -> dict:
    base = {
        "id": 1,
        "host": "imap.qq.com",
        "account": "test@qq.com",
        "folder": "INBOX",
        "uidvalidity": "1628153749",
        "imap_uid": "42",
        "message_id": "<abc@example.com>",
        "subject": "Test Subject",
        "from": "sender@example.com",
        "date": "Thu, 29 Aug 2024 16:18:48 +0000",
        "imported": False,
        "imported_at": None,
        "in_reply_to": "<parent@example.com>",
        "references": "<ref1@example.com> <ref2@example.com>",
    }
    base.update(overrides)
    return base


INDEX_PATCH = "agent.email_index.INDEX_PATH"


class GetMessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index_path = Path(self.tmp.name) / "index.json"
        self.index = EmailIndex(self.index_path)
        self.index.write(dict(version=1, next_id=3, emails=[
            _make_row(id=1, imported=False),
            _make_row(id=2, imported=True, imap_uid="43", message_id="<def@example.com>"),
        ]))

    def test_normal_get(self):
        with unittest.mock.patch(INDEX_PATCH, self.index_path):
            msg = get_message("email", 1)
        self.assertIsInstance(msg, Message)
        self.assertEqual(msg.id, 1)
        self.assertEqual(msg.source, "email")
        self.assertFalse(msg.imported)
        self.assertEqual(msg.content["subject"], "Test Subject")

    def test_get_imported_message(self):
        with unittest.mock.patch(INDEX_PATCH, self.index_path):
            msg = get_message("email", 2)
        self.assertTrue(msg.imported)

    def test_nonexistent_id_raises(self):
        with unittest.mock.patch(INDEX_PATCH, self.index_path):
            with self.assertRaises(ValueError):
                get_message("email", 999)

    def test_unsupported_source_raises(self):
        with self.assertRaises(ValueError):
            get_message("qq", 1)

    def test_imported_correctly_mapped(self):
        with unittest.mock.patch(INDEX_PATCH, self.index_path):
            msg_false = get_message("email", 1)
            msg_true = get_message("email", 2)
        self.assertFalse(msg_false.imported)
        self.assertTrue(msg_true.imported)


class SourceIdSemanticsTests(unittest.TestCase):
    """(source, id) uniquely identifies a message; same numeric id in
    different sources are different messages."""

    def test_same_id_different_source_raises_for_unknown(self):
        with self.assertRaises(ValueError):
            get_message("qq", 1)

    def test_email_source_deterministic(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "index.json"
        EmailIndex(path).write(dict(version=1, next_id=2, emails=[_make_row(id=1)]))
        with unittest.mock.patch(INDEX_PATCH, path):
            msg = get_message("email", 1)
        self.assertEqual(msg.source, "email")
        self.assertEqual(msg.id, 1)


class ListMessagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index_path = Path(self.tmp.name) / "index.json"
        self.index = EmailIndex(self.index_path)
        self.index.write(dict(version=1, next_id=4, emails=[
            _make_row(id=1, imported=False),
            _make_row(id=2, imported=True, imap_uid="43", message_id="<def@example.com>"),
            _make_row(id=3, imported=False, imap_uid="44", message_id="<ghi@example.com>"),
        ]))

    def test_list_all(self):
        with unittest.mock.patch(INDEX_PATCH, self.index_path):
            msgs = list_messages("email")
        self.assertEqual(len(msgs), 3)

    def test_list_imported_only(self):
        with unittest.mock.patch(INDEX_PATCH, self.index_path):
            msgs = list_messages("email", imported=True)
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].imported)

    def test_list_unimported_only(self):
        with unittest.mock.patch(INDEX_PATCH, self.index_path):
            msgs = list_messages("email", imported=False)
        self.assertEqual(len(msgs), 2)
        self.assertFalse(msgs[0].imported)

    def test_list_unsupported_source(self):
        with self.assertRaises(ValueError):
            list_messages("qq")


class EmailLocatorTests(unittest.TestCase):
    def test_locator_returns_all_identity_fields(self):
        msg = Message(id=1, source="email", time=None, imported=False, content={
            "host": "imap.qq.com", "account": "test@qq.com", "folder": "INBOX",
            "uidvalidity": "1628153749", "imap_uid": "42", "message_id": "<abc@example.com>",
            "subject": "Test", "from": "a@b.com", "date": "",
        })
        loc = email_locator(msg)
        self.assertEqual(loc["host"], "imap.qq.com")
        self.assertEqual(loc["imap_uid"], "42")
        self.assertIn("message_id", loc)
        self.assertNotIn("subject", loc)

    def test_identity_returns_id_plus_locator(self):
        msg = Message(id=5, source="email", time=None, imported=False, content={
            "host": "imap.qq.com", "account": "test@qq.com", "folder": "INBOX",
            "uidvalidity": "1628153749", "imap_uid": "42", "message_id": "<abc@example.com>",
            "subject": "Test", "from": "a@b.com", "date": "",
        })
        ident = email_identity(msg)
        self.assertEqual(ident["id"], 5)
        self.assertEqual(ident["host"], "imap.qq.com")
        self.assertIn("id", ident)

    def test_non_email_raises(self):
        msg = Message(id=1, source="qq", time=None, imported=False, content={})
        with self.assertRaises(ValueError):
            email_locator(msg)
        with self.assertRaises(ValueError):
            email_identity(msg)


if __name__ == "__main__":
    unittest.main()
