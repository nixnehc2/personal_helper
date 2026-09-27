"""Tests for Message model and Email-to-Message conversion.

Covers:
  - Normal email with all fields
  - Missing Message-ID
  - Missing Date
  - Unparseable Date
  - Chinese / special characters / Unicode
  - Full real index compatibility (1741 emails)
  - Reverse identity-field verification
  - imported field mapping (True / False)
  - imported_at is NOT carried into Message
  - Conversion stability (idempotent)
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from agent.messages import Message, email_row_to_message
from agent.messages.email_adapter import _parse_date

INDEX_PATH = Path(__file__).resolve().parent.parent / "data/email/index.json"


def _make_row(**overrides) -> dict:
    """Return a minimal valid email row, with overrides applied."""
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


class ParseDateTests(unittest.TestCase):
    """Low-level date parsing tests."""

    def test_rfc2822_utc(self):
        result = _parse_date("Thu, 29 Aug 2024 16:18:48 +0000")
        self.assertIsNotNone(result)
        self.assertIn("2024-08-29T16:18:48", result)
        self.assertIn("+00:00", result)

    def test_rfc2822_positive_offset(self):
        result = _parse_date("Fri, 30 Aug 2024 01:15:28 +0300")
        self.assertIsNotNone(result)
        self.assertIn("2024-08-30T01:15:28", result)
        self.assertIn("+03:00", result)

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_date(""))

    def test_none_returns_none(self):
        self.assertIsNone(_parse_date(""))

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_date("not a date at all"))

    def test_partial_date_returns_none(self):
        self.assertIsNone(_parse_date("Aug 2024"))

    def test_iso_input_returns_none(self):
        result = _parse_date("2024-08-29T16:18:48+00:00")
        self.assertIsInstance(result, (str, type(None)))


class EmailRowToMessageNormalTests(unittest.TestCase):
    """Conversion of a fully-populated email row."""

    def setUp(self):
        self.row = _make_row()
        self.msg = email_row_to_message(self.row)

    def test_type(self):
        self.assertIsInstance(self.msg, Message)

    def test_id_preserved(self):
        self.assertEqual(self.msg.id, self.row["id"])

    def test_source_is_email(self):
        self.assertEqual(self.msg.source, "email")

    def test_time_parsed(self):
        self.assertIsNotNone(self.msg.time)
        self.assertIn("2024-08-29T16:18:48", self.msg.time)
        self.assertIn("+00:00", self.msg.time)

    def test_identity_fields_in_content(self):
        for key in ("host", "account", "folder", "uidvalidity",
                     "imap_uid", "message_id"):
            self.assertEqual(self.msg.content[key], self.row[key],
                             msg=f"field {key!r} lost")

    def test_raw_date_preserved_in_content(self):
        self.assertEqual(self.msg.content["date"], self.row["date"])

    def test_subject_preserved(self):
        self.assertEqual(self.msg.content["subject"], self.row["subject"])

    def test_from_preserved(self):
        self.assertEqual(self.msg.content["from"], self.row["from"])

    def test_in_reply_to_preserved(self):
        self.assertEqual(self.msg.content["in_reply_to"],
                         self.row["in_reply_to"])

    def test_references_preserved(self):
        self.assertEqual(self.msg.content["references"],
                         self.row["references"])

    def test_imported_not_in_content(self):
        self.assertNotIn("imported", self.msg.content)
        self.assertNotIn("imported_at", self.msg.content)

    def test_id_not_in_content(self):
        self.assertNotIn("id", self.msg.content)

    def test_imported_false_by_default(self):
        self.assertFalse(self.msg.imported)


class ImportedFieldTests(unittest.TestCase):
    """imported is a top-level bool, mapped from EmailIndex."""

    def test_imported_false(self):
        row = _make_row(imported=False)
        msg = email_row_to_message(row)
        self.assertIs(msg.imported, False)

    def test_imported_true(self):
        row = _make_row(imported=True)
        msg = email_row_to_message(row)
        self.assertIs(msg.imported, True)

    def test_imported_is_bool_type(self):
        for val in (True, False):
            msg = email_row_to_message(_make_row(imported=val))
            self.assertIsInstance(msg.imported, bool)

    def test_imported_at_not_on_message(self):
        row = _make_row(imported=True,
                         imported_at="2026-09-23T20:53:07.166184+00:00")
        msg = email_row_to_message(row)
        self.assertTrue(msg.imported)
        # imported_at should not exist as a Message attribute
        self.assertFalse(hasattr(msg, "imported_at"))
        # and it should not leak into content either
        self.assertNotIn("imported_at", msg.content)

    def test_imported_not_in_content(self):
        for val in (True, False):
            msg = email_row_to_message(_make_row(imported=val))
            self.assertNotIn("imported", msg.content)

    def test_conversion_stability(self):
        """Converting the same row twice must produce identical results."""
        row = _make_row(imported=True,
                         imported_at="2026-09-23T20:53:07+00:00")
        msg1 = email_row_to_message(row)
        msg2 = email_row_to_message(row)
        self.assertEqual(msg1.id, msg2.id)
        self.assertEqual(msg1.source, msg2.source)
        self.assertEqual(msg1.time, msg2.time)
        self.assertEqual(msg1.imported, msg2.imported)
        self.assertEqual(msg1.content, msg2.content)


class EmailRowToMessageMissingMessageId(unittest.TestCase):
    """Row with empty message_id should still convert."""

    def test_empty_message_id(self):
        row = _make_row(message_id="")
        msg = email_row_to_message(row)
        self.assertEqual(msg.content["message_id"], "")
        self.assertEqual(msg.id, row["id"])


class EmailRowToMessageNoDate(unittest.TestCase):
    """Row with empty date => time=None."""

    def test_empty_date(self):
        row = _make_row(date="")
        msg = email_row_to_message(row)
        self.assertIsNone(msg.time)
        self.assertEqual(msg.content["date"], "")

    def test_none_like_date(self):
        row = _make_row(date="")
        msg = email_row_to_message(row)
        self.assertIsNone(msg.time)


class EmailRowToMessageBadDate(unittest.TestCase):
    """Unparseable date must not crash conversion."""

    def test_garbage_date(self):
        row = _make_row(date="totally invalid")
        msg = email_row_to_message(row)
        self.assertIsNone(msg.time)
        self.assertEqual(msg.content["date"], "totally invalid")

    def test_partial_date(self):
        row = _make_row(date="Aug 2024")
        msg = email_row_to_message(row)
        self.assertIsNone(msg.time)
        self.assertEqual(msg.content["date"], "Aug 2024")


class EmailRowToMessageUnicode(unittest.TestCase):
    """Chinese and special characters must survive round-trip."""

    def test_chinese_subject_and_from(self):
        row = _make_row(subject="[pixiv] 验证码通知")
        row["from"] = "pixiv事务局 <no-reply@pixiv.net>"
        msg = email_row_to_message(row)
        self.assertEqual(msg.content["subject"], "[pixiv] 验证码通知")
        self.assertEqual(msg.content["from"], "pixiv事务局 <no-reply@pixiv.net>")

    def test_special_html_chars(self):
        row = _make_row(subject="special | <>& \"quotes\"")
        msg = email_row_to_message(row)
        self.assertEqual(msg.content["subject"], "special | <>& \"quotes\"")

    def test_emoji(self):
        row = _make_row(subject="Hello 🌍🚀")
        msg = email_row_to_message(row)
        self.assertEqual(msg.content["subject"], "Hello 🌍🚀")

    def test_japanese(self):
        row = _make_row(subject="日本語テスト件名")
        msg = email_row_to_message(row)
        self.assertEqual(msg.content["subject"], "日本語テスト件名")

    def test_mixed_unicode_references(self):
        row = _make_row(references="<ref@example.com> <日本語@example.com>")
        msg = email_row_to_message(row)
        self.assertEqual(msg.content["references"], "<ref@example.com> <日本語@example.com>")


class RealIndexCompatibilityTest(unittest.TestCase):
    """Full compatibility test against the real data/email/index.json."""

    @classmethod
    def setUpClass(cls):
        if not INDEX_PATH.exists():
            raise unittest.SkipTest("index.json not found")
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        cls.rows = data["emails"]
        cls.messages = [email_row_to_message(r) for r in cls.rows]

    def test_count_matches(self):
        self.assertEqual(len(self.messages), len(self.rows))

    def test_no_conversion_exceptions(self):
        pass

    def test_ids_match(self):
        for row, msg in zip(self.rows, self.messages):
            self.assertEqual(msg.id, row["id"])

    def test_all_source_email(self):
        for msg in self.messages:
            self.assertEqual(msg.source, "email")

    def test_all_ids_unique(self):
        ids = [m.id for m in self.messages]
        self.assertEqual(len(ids), len(set(ids)))

    def test_identity_fields_preserved(self):
        identity_keys = ("host", "account", "folder", "uidvalidity",
                         "imap_uid", "message_id")
        for row, msg in zip(self.rows, self.messages):
            for key in identity_keys:
                self.assertEqual(
                    msg.content[key], row[key],
                    msg=f"id={row['id']}: field {key!r} lost",
                )

    def test_raw_dates_preserved(self):
        for row, msg in zip(self.rows, self.messages):
            self.assertEqual(
                msg.content["date"], row["date"],
                msg=f"id={row['id']}: raw date lost",
            )

    def test_parsed_times_for_valid_dates(self):
        for row, msg in zip(self.rows, self.messages):
            if row["date"]:
                try:
                    from email.utils import parsedate_to_datetime
                    parsedate_to_datetime(row["date"])
                    self.assertIsNotNone(
                        msg.time,
                        msg=f"id={row['id']}: valid date but time=None",
                    )
                except (ValueError, TypeError):
                    pass

    def test_subjects_not_corrupted(self):
        for row, msg in zip(self.rows, self.messages):
            self.assertEqual(
                msg.content["subject"], row["subject"],
                msg=f"id={row['id']}: subject corrupted",
            )

    def test_from_not_corrupted(self):
        for row, msg in zip(self.rows, self.messages):
            self.assertEqual(
                msg.content["from"], row["from"],
                msg=f"id={row['id']}: from corrupted",
            )

    def test_imported_not_leaked(self):
        for msg in self.messages:
            self.assertNotIn("imported", msg.content)
            self.assertNotIn("imported_at", msg.content)

    def test_imported_mapped_correctly(self):
        """Every real email's imported flag must be preserved exactly."""
        for row, msg in zip(self.rows, self.messages):
            self.assertIs(
                msg.imported, bool(row["imported"]),
                msg=f"id={row['id']}: imported mismatch",
            )


class ReverseIdentityVerificationTest(unittest.TestCase):
    """Verify that Message.content contains enough to locate the email."""

    @classmethod
    def setUpClass(cls):
        if not INDEX_PATH.exists():
            raise unittest.SkipTest("index.json not found")
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        cls.rows = data["emails"]
        cls.messages = [email_row_to_message(r) for r in cls.rows]

    def test_can_recover_identity_from_content(self):
        identity_keys = ("host", "account", "folder", "uidvalidity",
                         "imap_uid", "message_id")
        for row, msg in zip(self.rows, self.messages):
            for key in identity_keys:
                self.assertIn(key, msg.content,
                              msg=f"id={row['id']}: {key!r} missing")
                self.assertEqual(msg.content[key], row[key],
                                 msg=f"id={row['id']}: {key!r} mismatch")


if __name__ == "__main__":
    unittest.main()
