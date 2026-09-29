"""Offline QQ Automation source integration: validator, matcher, cursor, consumer, runtime."""
from contextlib import closing
from datetime import timedelta
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

import test_automations as fixtures
from agent.automations import AutomationStore
from agent.automation_checker import check_once
from agent.automation_consumer import consume_once
from agent.automation_qq import read_event_qq
from agent.automation_sources import QQSource, matches_qq
from agent.automation_triggers import qq as qq_validator
from agent.messages.models import Message
from agent.tools import FileTools


def _msg(id, text="hello", sender_id="100", conv_type="group", conv_id="200", conv_name="test"):
    """Create a minimal Message for testing."""
    return Message(id=id, source="qq", time="2026-09-28T10:00:00+08:00", imported=False, content={
        "text": text,
        "sender": {"user_id": sender_id},
        "conversation": {"type": conv_type, "id": conv_id, "name": conv_name},
    })


def _insert_msg(store, message):
    """Insert a Message into the QQ store and return its rowid."""
    from dataclasses import asdict
    db = store.connect()
    try:
        cur = db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?)",
                         (str(message.id), json.dumps(asdict(message), ensure_ascii=False)))
        db.commit()
        return cur.lastrowid
    finally:
        db.close()


def qq_rule(match=None, mode="continuous"):
    """Create a QQ automation rule dict."""
    return dict(name="QQ 监控", trigger_type="event", source="qq",
                content="检查这条 QQ 消息", mode=mode,
                trigger_config=dict(match=match or {}, check_interval_seconds=10))


NOW = fixtures.NOW


class QQValidatorTests(unittest.TestCase):
    """Test qq() trigger_config validator."""

    def _validate(self, config):
        return qq_validator(config, {})

    def test_valid_empty_match(self):
        self._validate({"match": {}})

    def test_valid_conversations(self):
        self._validate({"match": {"conversations": [{"type": "group", "id": "100"}]}})

    def test_valid_sender_ids(self):
        self._validate({"match": {"sender_ids": ["123", "456"]}})

    def test_valid_text_contains(self):
        self._validate({"match": {"text_contains": "作业"}})

    def test_valid_all_fields(self):
        self._validate({"match": {
            "conversations": [{"type": "group", "id": "100"}, {"type": "private", "id": "200"}],
            "sender_ids": ["123"],
            "text_contains": "考试",
        }})

    def test_invalid_unknown_field(self):
        with self.assertRaises(ValueError):
            self._validate({"match": {"unknown": "x"}})

    def test_invalid_empty_sender_ids(self):
        with self.assertRaises(ValueError):
            self._validate({"match": {"sender_ids": []}})

    def test_invalid_empty_conversations(self):
        with self.assertRaises(ValueError):
            self._validate({"match": {"conversations": []}})

    def test_invalid_conversation_type(self):
        with self.assertRaises(ValueError):
            self._validate({"match": {"conversations": [{"type": "channel", "id": "1"}]}})

    def test_invalid_conversation_missing_id(self):
        with self.assertRaises(ValueError):
            self._validate({"match": {"conversations": [{"type": "group"}]}})

    def test_invalid_empty_text_contains(self):
        with self.assertRaises(ValueError):
            self._validate({"match": {"text_contains": ""}})

    def test_invalid_extra_top_level_field(self):
        with self.assertRaises(ValueError):
            self._validate({"match": {}, "scope": {}})


class MatchesQQTests(unittest.TestCase):
    """Test matches_qq() matching logic."""

    def _match(self, message, match_config):
        return matches_qq(message, match_config)

    def test_empty_match_matches_all(self):
        msg = _msg(1)
        self.assertTrue(self._match(msg, {}))

    def test_empty_match_private_and_group(self):
        self.assertTrue(self._match(_msg(1, conv_type="group"), {}))
        self.assertTrue(self._match(_msg(2, conv_type="private"), {}))

    def test_conversations_or(self):
        msg1 = _msg(1, conv_type="group", conv_id="100")
        msg2 = _msg(2, conv_type="group", conv_id="200")
        msg3 = _msg(3, conv_type="private", conv_id="300")
        match = {"conversations": [{"type": "group", "id": "100"}, {"type": "private", "id": "300"}]}
        self.assertTrue(self._match(msg1, match))
        self.assertFalse(self._match(msg2, match))
        self.assertTrue(self._match(msg3, match))

    def test_conversations_type_discriminates(self):
        msg = _msg(1, conv_type="private", conv_id="100")
        self.assertFalse(self._match(msg, {"conversations": [{"type": "group", "id": "100"}]}))

    def test_sender_ids_or(self):
        match = {"sender_ids": ["123", "456"]}
        self.assertTrue(self._match(_msg(1, sender_id="123"), match))
        self.assertTrue(self._match(_msg(2, sender_id="456"), match))
        self.assertFalse(self._match(_msg(3, sender_id="789"), match))

    def test_sender_self_matches(self):
        msg = _msg(1, sender_id="self_account")
        self.assertTrue(self._match(msg, {"sender_ids": ["self_account"]}))

    def test_text_contains(self):
        match = {"text_contains": "作业"}
        self.assertTrue(self._match(_msg(1, text="请交作业"), match))
        self.assertFalse(self._match(_msg(2, text="今天天气好"), match))

    def test_cross_field_and(self):
        match = {
            "conversations": [{"type": "group", "id": "100"}],
            "sender_ids": ["123"],
            "text_contains": "作业",
        }
        self.assertTrue(self._match(_msg(1, conv_type="group", conv_id="100", sender_id="123", text="交作业"), match))
        self.assertFalse(self._match(_msg(2, conv_type="group", conv_id="100", sender_id="123", text="天气好"), match))
        self.assertFalse(self._match(_msg(3, conv_type="group", conv_id="200", sender_id="123", text="交作业"), match))
        self.assertFalse(self._match(_msg(4, conv_type="group", conv_id="100", sender_id="456", text="交作业"), match))


class QQSourceTests(unittest.TestCase):
    """Test QQSource baseline, cursor, matching and mode behavior."""

    def setUp(self):
        self.tmp = fixtures.tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = fixtures.Path(self.tmp.name) / "qq/messages.sqlite3"
        self.auto_path = fixtures.Path(self.tmp.name) / "automations.sqlite3"
        self.store = AutomationStore(self.auto_path, {}, lambda: NOW)
        self.qq_store = None
        for target, value in (("agent.qq_sync.DB_PATH", self.db_path),
                              ("agent.qq_sync_selector.WHITELIST_PATH",
                               fixtures.Path(self.tmp.name) / "qq/sync_conversations.json")):
            guard = patch(target, value)
            guard.start()
            self.addCleanup(guard.stop)

    def _init_qq_db(self):
        from agent.qq_sync import QQStore
        self.qq_store = QQStore(self.db_path)
        self.qq_store.connect().close()

    def _insert(self, message):
        if self.qq_store is None:
            self._init_qq_db()
        return _insert_msg(self.qq_store, message)

    def _create(self, match=None, mode="continuous"):
        return self.store.manage("create", rule=qq_rule(match, mode))["rule"]["id"]

    def _check(self, seconds=0):
        return check_once(self.store, NOW + timedelta(seconds=seconds),
                          qq_db_path=self.db_path)

    def _row(self, id=1):
        return self.store.manage("get", id)["rule"]

    def test_baseline_no_trigger_on_existing_messages(self):
        self._insert(_msg(1, text="old message"))
        self._insert(_msg(2, text="another old"))
        self._create()
        result = self._check()
        self.assertEqual(result["enqueued"], [])
        row = self._row()
        self.assertIsNotNone(row["cursor"])
        self.assertEqual(json.loads(row["cursor"])["last_rowid"], 2)

    def test_new_message_after_baseline_triggers(self):
        self._insert(_msg(1, text="old"))
        self._create()
        self._check()
        self._insert(_msg(2, text="new"))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 1)
        self.assertEqual(self._row()["pending_events"][0]["data"]["message"]["id"], 2)
        self.assertEqual(json.loads(self._row()["cursor"])["last_rowid"], 2)

    def test_empty_match_matches_all_new_messages(self):
        self._create(match={})
        self._check()
        self._insert(_msg(1, text="group msg", conv_type="group", sender_id="a"))
        self._insert(_msg(2, text="private msg", conv_type="private", sender_id="b"))
        self._insert(_msg(3, text="self msg", conv_type="group", sender_id="self"))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 3)

    def test_conversations_or(self):
        self._create(match={"conversations": [
            {"type": "group", "id": "100"},
            {"type": "private", "id": "300"},
        ]})
        self._check()
        self._insert(_msg(1, conv_type="group", conv_id="100"))
        self._insert(_msg(2, conv_type="group", conv_id="200"))
        self._insert(_msg(3, conv_type="private", conv_id="300"))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 2)
        ids = {e["data"]["message"]["id"] for e in self._row()["pending_events"]}
        self.assertEqual(ids, {1, 3})
        self.assertEqual(json.loads(self._row()["cursor"])["last_rowid"], 3)

    def test_sender_ids_or(self):
        self._create(match={"sender_ids": ["123", "456"]})
        self._check()
        self._insert(_msg(1, sender_id="123"))
        self._insert(_msg(2, sender_id="456"))
        self._insert(_msg(3, sender_id="789"))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 2)

    def test_sender_self_matches(self):
        self._create(match={"sender_ids": ["self"]})
        self._check()
        self._insert(_msg(1, sender_id="self"))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 1)

    def test_text_contains(self):
        self._create(match={"text_contains": "作业"})
        self._check()
        self._insert(_msg(1, text="请交作业"))
        self._insert(_msg(2, text="天气好"))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 1)
        self.assertEqual(self._row()["pending_events"][0]["data"]["message"]["id"], 1)

    def test_cross_field_and(self):
        self._create(match={
            "conversations": [{"type": "group", "id": "100"}],
            "sender_ids": ["123"],
            "text_contains": "作业",
        })
        self._check()
        self._insert(_msg(1, conv_type="group", conv_id="100", sender_id="123", text="交作业"))
        self._insert(_msg(2, conv_type="group", conv_id="100", sender_id="123", text="天气"))
        self._insert(_msg(3, conv_type="private", conv_id="100", sender_id="123", text="交作业"))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 1)
        self.assertEqual(self._row()["pending_events"][0]["data"]["message"]["id"], 1)

    def test_once_only_first_match(self):
        self._create(match={}, mode="once")
        self._check()
        self._insert(_msg(1))
        self._insert(_msg(2))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 1)
        self.assertEqual(self._check(20)["enqueued"], [])

    def test_continuous_all_matches(self):
        self._create(match={}, mode="continuous")
        self._check()
        self._insert(_msg(1))
        self._insert(_msg(2))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 2)

    def test_cursor_advances_past_all_messages(self):
        self._create(match={"text_contains": "match"})
        self._check()
        self._insert(_msg(1, text="no"))
        self._insert(_msg(2, text="match"))
        self._insert(_msg(3, text="no"))
        self._check(10)
        self.assertEqual(json.loads(self._row()["cursor"])["last_rowid"], 3)
        self.assertEqual(len(self._row()["pending_events"]), 1)

    def test_check_interval_sets_next_check_at(self):
        self._create()
        self._check()
        from agent.automation_triggers import instant
        expected = instant(NOW.isoformat()) + timedelta(seconds=10)
        self.assertEqual(instant(self._row()["next_check_at"]), expected)

    def test_pause_resume_resets_baseline(self):
        self._insert(_msg(1, text="before pause"))
        self._create()
        self._check()
        self.store.manage("pause", 1)
        self._insert(_msg(2, text="during pause"))
        self._insert(_msg(3, text="during pause 2"))
        self.store.manage("resume", 1)
        row = self._row()
        self.assertIsNone(row["cursor"])
        self._check(11)  # re-baseline at current max rowid
        self._insert(_msg(4, text="after resume"))
        result = self._check(21)
        self.assertEqual(len(result["enqueued"]), 1)
        self.assertEqual(self._row()["pending_events"][0]["data"]["message"]["id"], 4)
        self.assertEqual(len([e for e in self._row()["pending_events"] if e["status"] == "pending"]), 1)

    def test_no_duplicate_events(self):
        self._create()
        self._check()
        self._insert(_msg(1))
        self._check(10)
        self.assertEqual(len(self._row()["pending_events"]), 1)
        self._check(20)
        self.assertEqual(len(self._row()["pending_events"]), 1)

    def test_event_data_structure(self):
        self._create(match={})
        self._check()
        self._insert(_msg(42, text="hello", sender_id="999", conv_type="group", conv_id="100", conv_name="MyGroup"))
        self._check(10)
        event = self._row()["pending_events"][0]
        self.assertEqual(event["event_id"], "qq:1:42")
        self.assertEqual(event["source"], "qq")
        self.assertEqual(event["event_type"], "qq.matched")
        self.assertEqual(event["data"]["message"], {"source": "qq", "id": 42})
        self.assertEqual(event["data"]["conversation"]["type"], "group")
        self.assertEqual(event["data"]["conversation"]["id"], "100")
        self.assertEqual(event["data"]["sender"]["user_id"], "999")
        self.assertEqual(event["data"]["message_time"], "2026-09-28T10:00:00+08:00")

    def test_shared_sync_between_rules(self):
        self._create(match={"text_contains": "a"})
        self._create(match={"text_contains": "b"})
        self._check()
        self._insert(_msg(1, text="a"))
        self._insert(_msg(2, text="b"))
        result = self._check(10)
        self.assertEqual(len(result["enqueued"]), 2)

    def test_update_match_preserves_cursor(self):
        self._create(match={"text_contains": "old"})
        self._check()
        self._insert(_msg(1, text="old"))
        self._check(10)
        cursor_before = self._row()["cursor"]
        self.store.manage("update", 1, dict(trigger_config={"match": {"text_contains": "new"}, "check_interval_seconds": 10}))
        self.assertEqual(self._row()["cursor"], cursor_before)


class QQConsumerRuntimeTests(unittest.TestCase):
    """Test QQ event consumption through the existing consumer and runtime."""

    def setUp(self):
        self.tmp = fixtures.tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = fixtures.Path(self.tmp.name) / "qq/messages.sqlite3"
        self.auto_path = fixtures.Path(self.tmp.name) / "automations.sqlite3"
        self.store = AutomationStore(self.auto_path, {}, lambda: NOW)
        for target, value in (("agent.qq_sync.DB_PATH", self.db_path),
                              ("agent.qq_sync_selector.WHITELIST_PATH",
                               fixtures.Path(self.tmp.name) / "qq/sync_conversations.json")):
            guard = patch(target, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.root = fixtures.Path(self.tmp.name) / "memory"
        self.root.mkdir(parents=True)
        (self.root / "AGENT.md").write_text("Test", encoding="utf-8")
        self.files = FileTools(self.root, lambda _: {}, lambda *args: "no")
        self.client = Mock()
        self.client.complete.return_value = dict(
            content=[dict(type="tool_use", name="complete_event", id="done",
                          input=dict(reply="QQ 消息已检查"))],
            stop_reason="tool_use")

    def _init_qq_db(self):
        from agent.qq_sync import QQStore
        store = QQStore(self.db_path)
        store.connect().close()
        return store

    def _insert(self, message):
        return _insert_msg(self._init_qq_db(), message)

    def _create(self, match=None, mode="continuous"):
        return self.store.manage("create", rule=qq_rule(match, mode))["rule"]["id"]

    def _check(self, seconds=0):
        return check_once(self.store, NOW + timedelta(seconds=seconds), qq_db_path=self.db_path)

    def _row(self, id=1):
        return self.store.manage("get", id)["rule"]

    def _consume(self, emit=lambda _: None):
        return consume_once(self.client, self.files, self.store, emit,
                            email_index_path=None, read=lambda _: "/exit")

    def test_full_pipeline_qq_source_to_delivery(self):
        self._create()
        self._check()
        self._insert(_msg(42, text="important message"))
        self._check(10)
        self.assertEqual(len(self._row()["pending_events"]), 1)
        result = self._consume()
        self.assertEqual(len(result["delivered"]), 1)
        self.assertEqual(self._row()["pending_events"], [])
        self.client.complete.assert_called_once()
        system = self.client.complete.call_args.args[0]
        self.assertIn("不可信外部资料", system)
        messages = self.client.complete.call_args.args[1]
        self.assertIn("不可信 QQ 消息资料", str(messages))

    def test_read_event_qq_returns_formatted_message(self):
        self._insert(_msg(99, text="test content", sender_id="abc", conv_type="private", conv_id="50"))
        event = {"data": {"message": {"source": "qq", "id": 99}}}
        result = read_event_qq(event, {})
        self.assertEqual(result["id"], 99)
        self.assertEqual(result["source"], "qq")
        self.assertEqual(result["text"], "test content")
        self.assertEqual(result["sender"]["user_id"], "abc")
        self.assertIn("QQ", result["formatted"])

    def test_imported_unchanged_after_event_consumption(self):
        self._create()
        self._check()
        msg = _msg(1, text="hello")
        self._insert(msg)
        self._check(10)
        from agent.messages import get_message
        before = get_message("qq", 1)
        self.assertFalse(before.imported)
        self._consume()
        after = get_message("qq", 1)
        self.assertFalse(after.imported)

    def test_once_completes_after_first_consumption(self):
        self._create(mode="once")
        self._check()
        self._insert(_msg(1))
        self._check(10)
        self._consume()
        self.assertEqual(self._row()["status"], "completed")

    def test_continuous_remains_active_after_consumption(self):
        self._create(mode="continuous")
        self._check()
        self._insert(_msg(1))
        self._check(10)
        self._consume()
        self.assertEqual(self._row()["status"], "active")

    def test_model_failure_records_error_and_retries(self):
        self._create()
        self._check()
        self._insert(_msg(1))
        self._check(10)
        self.client.complete.side_effect = RuntimeError("offline")
        result = self._consume()
        self.assertEqual(len(result["failed"]), 1)
        event = self._row()["pending_events"][0]
        self.assertEqual(event["attempts"], 1)
        self.assertIsNotNone(event["last_error"])
        self.assertIsNone(event["reply"])

    def test_saved_reply_survives_restart(self):
        self._create(mode="once")
        self._check()
        self._insert(_msg(1))
        self._check(10)
        def fail_display(text):
            if text.startswith("[automation"):
                raise OSError("terminal offline")
        self._consume(fail_display)
        event = self._row()["pending_events"][0]
        self.assertEqual(event["reply"], "QQ 消息已检查")
        code = """from agent.automation_consumer import consume_once
from agent.automations import AutomationStore
import sys,json
result=consume_once(None,None,AutomationStore(sys.argv[1],{}),emit=lambda text: None)
print(json.dumps(result))
"""
        import subprocess, sys
        result = json.loads(subprocess.check_output(
            [sys.executable, "-c", code, str(self.auto_path)], text=True))
        self.assertEqual(len(result["delivered"]), 1)
        self.assertEqual(self._row()["status"], "completed")

    def test_email_and_qq_events_coexist(self):
        from test_automations import mail
        self.store.settings["EMAIL_ACCOUNT"] = "me@example.com"
        self.store.manage("create", rule=mail())
        self._create()
        self._check()
        self._insert(_msg(1))
        self._check(10)
        qq_row = self.store.manage("get", 2)["rule"]
        self.assertEqual(len(qq_row["pending_events"]), 1)
        email_row = self.store.manage("get", 1)["rule"]
        self.assertEqual(len(email_row["pending_events"]), 0)


if __name__ == "__main__":
    unittest.main()