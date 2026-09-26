"""Incremental UID sync, migration and atomic failure acceptance."""
import json
import imaplib
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_email_index as fixtures
from agent.email_index import EmailIndex, scope_key, update_email_index


class IncrementalTests(unittest.TestCase):
    setUp = fixtures.IndexTests.setUp
    sync = fixtures.IndexTests.sync

    def progress(self):
        return EmailIndex(self.path).read()["sync_progress"][scope_key(fixtures.CONFIG)]

    def fetches(self):
        return [args[0] for command, args in self.mailbox.calls if command == "fetch"]

    def test_initial_empty_second_boundary_filter_and_two_new(self):
        self.assertEqual(self.sync()["mode"], "full")
        self.assertEqual(self.fetches(), [b"1,2"])
        self.mailbox.calls.clear()
        second = self.sync()
        self.assertEqual(second["mode"], "incremental")
        self.assertEqual(second["queried_uid_count"], 1)  # Boundary UID returned by server.
        self.assertEqual(second["fetched_header_count"], 0)
        self.assertEqual(self.fetches(), [])
        self.mailbox.headers.update({b"3": b"Message-ID: <three>\r\n\r\n", b"4": b"Message-ID: <four>\r\n\r\n"})
        result = self.sync()
        self.assertEqual(self.fetches(), [b"3,4"])
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(self.progress()["max_uid"], 4)

    def test_restart_uses_saved_progress(self):
        self.sync()
        code = """from unittest.mock import patch
from agent.email_index import update_email_index
import sys,json
sys.path.insert(0,'tests')
from test_email_index import Mailbox,CONFIG
mailbox=Mailbox()
with patch('agent.email_index.imaplib.IMAP4_SSL',return_value=mailbox):
 result=update_email_index(CONFIG,sys.argv[1])
print(json.dumps({'mode':result['mode'],'fetched':result['fetched_header_count']}))
"""
        result = json.loads(subprocess.check_output([sys.executable, "-c", code, str(self.path)], text=True))
        self.assertEqual(result, dict(mode="incremental", fetched=0))

    def test_missing_and_parse_failures_are_permanently_skipped(self):
        self.mailbox.malformed = True
        self.mailbox.headers[b"3"] = b"not a valid header\r\n\r\n"
        result = self.sync()
        self.assertEqual(result["skipped_uids"], ["2", "3"])
        failures = EmailIndex(self.path).read()["sync_failures"]
        self.assertEqual([f["error_type"] for f in failures], ["missing_header", "parse_error"])
        self.assertTrue(all(set(("host", "account", "folder", "uidvalidity", "imap_uid", "failed_at")) <= f.keys() for f in failures))
        self.assertEqual(self.progress()["max_uid"], 3)
        self.mailbox.malformed = False
        self.mailbox.calls.clear()
        self.sync()
        self.assertEqual(self.fetches(), [])
        self.assertEqual(len(EmailIndex(self.path).read()["emails"]), 1)

    def test_batch_failure_records_all_and_continues_next_batch(self):
        self.mailbox.headers = {str(i).encode(): f"Message-ID: <{i}>\r\n\r\n".encode() for i in range(1, 206)}
        original = self.mailbox.uid
        def uid(command, *args):
            if command == "fetch" and args[0].startswith(b"1,"):
                raise imaplib.IMAP4.error("private authorization contents")
            return original(command, *args)
        with patch.object(self.mailbox, "uid", side_effect=uid):
            result = self.sync()
        self.assertEqual(result["success_count"], 105)
        self.assertEqual(result["skipped_count"], 100)
        self.assertEqual(self.progress()["max_uid"], 205)
        self.assertNotIn("private", self.path.read_text(encoding="utf-8"))
        self.mailbox.calls.clear()
        self.sync()
        self.assertEqual(self.fetches(), [])

    def test_connection_login_select_and_search_failures_leave_progress_unchanged(self):
        self.sync()
        before = self.path.read_bytes()
        failures = [patch("agent.email_index.imaplib.IMAP4_SSL", side_effect=OSError("private")),
                    patch.object(self.mailbox, "login", return_value=("NO", [])),
                    patch.object(self.mailbox, "select", return_value=("NO", [])),
                    patch.object(self.mailbox, "uid", return_value=("NO", []))]
        for failure in failures:
            with failure, self.assertRaises(ValueError):
                self.sync()
            self.assertEqual(self.path.read_bytes(), before)

    def test_save_failure_keeps_records_and_progress_together(self):
        self.sync()
        before = self.path.read_bytes()
        self.mailbox.headers[b"3"] = b"bad header\r\n\r\n"
        with patch("agent.email_index.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.sync()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.progress()["max_uid"], 2)
        self.assertEqual(self.sync()["skipped_uids"], ["3"])
        self.assertEqual(self.progress()["max_uid"], 3)

    def test_legacy_upgrade_and_uidvalidity_keep_id_and_import(self):
        self.sync()
        data = EmailIndex(self.path).read()
        data.pop("sync_progress")
        data.pop("sync_failures")
        for row in data["emails"]:
            row.pop("in_reply_to")
            row.pop("references")
        data["emails"][0].update(imported=True, imported_at="2026-09-24")
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.mailbox.headers[b"1"] = b"Message-ID: <one>\r\nIn-Reply-To: <parent>\r\nReferences: <root>\r\n\r\n"
        self.mailbox.calls.clear()
        self.assertEqual(self.sync()["mode"], "full")
        first = EmailIndex(self.path).get(1)
        self.assertTrue(first["imported"])
        self.assertEqual(first["in_reply_to"], "<parent>")
        self.assertEqual(first["references"], "<root>")
        self.assertEqual(self.fetches(), [b"1,2"])
        self.mailbox.validity = b"200"
        self.mailbox.headers = {b"8": self.mailbox.headers[b"1"]}
        self.assertEqual(self.sync()["mode"], "full")
        first = EmailIndex(self.path).get(1)
        self.assertEqual(first["imap_uid"], "8")
        self.assertTrue(first["imported"])

    def test_progress_is_scoped_and_invalid_progress_forces_full_sync(self):
        self.sync()
        for changes in (dict(EMAIL_ACCOUNT="other@example.com"), dict(EMAIL_FOLDER="Archive"), dict(EMAIL_IMAP_HOST="imap.other")):
            self.assertEqual(update_email_index(dict(fixtures.CONFIG, **changes), self.path)["mode"], "full")
        data = EmailIndex(self.path).read()
        self.assertEqual(len(data["sync_progress"]), 4)
        data["sync_progress"][scope_key(fixtures.CONFIG)]["max_uid"] = "invalid"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.sync()["mode"], "full")
