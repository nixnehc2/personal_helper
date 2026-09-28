"""Deterministic timer ingestion acceptance; no waiting or external services."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from datetime import timedelta
from io import StringIO
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_automations as fixtures
from agent.automations import AutomationStore
from agent.automation_checker import check_once, pending, loop_lock
from agent.automation_triggers import instant
from agent.main import main, parse_tool_command
import agent.automation_checker as checker


NOW, timed, mail = fixtures.NOW, fixtures.timed, fixtures.mail


class CheckerTests(unittest.TestCase):
    setUp = fixtures.AutomationsTests.setUp
    create = fixtures.AutomationsTests.create
    def row(self, id=1):
        return self.store.manage("get", id)["rule"]

    def check(self, seconds=0, **kwargs):
        return check_once(self.store, NOW + timedelta(seconds=seconds), **kwargs)

    def test_once_tolerance_restart_and_normalized_identity(self):
        self.create(timed("once", at=NOW.isoformat()))
        result = self.check(60)
        self.assertEqual(len(result["enqueued"]), 1)
        event = self.row()["pending_events"][0]
        self.assertEqual(event["event_id"], "schedule:1:2026-09-28T00:30:00Z")
        self.assertFalse(event["data"]["is_catch_up"])
        self.assertIsNone(self.row()["next_check_at"])
        self.assertEqual(self.row()["status"], "active")
        self.assertEqual(self.check(60)["enqueued"], [])
        code = "from agent.automations import AutomationStore; from agent.automation_checker import check_once; import sys,json; from pathlib import Path; base=Path(sys.argv[1]).parent; print(json.dumps(check_once(AutomationStore(sys.argv[1], {}), qq_db_path=base / 'qq/messages.sqlite3', qq_whitelist_path=base / 'qq/sync_conversations.json')))"
        result = json.loads(subprocess.check_output([sys.executable, "-c", code, str(self.path)], text=True))
        self.assertEqual(result["enqueued"], [])
        self.assertEqual(len(pending(self.store)["events"]), 1)

    def test_once_missed_policies(self):
        for policy in ("latest", "skip"):
            id = self.create(timed("once", at=NOW.isoformat(), missed_policy=policy))["rule"]["id"]
            result = self.check(61)
            row = self.row(id)
            self.assertEqual(len(row["pending_events"]), int(policy == "latest"))
            self.assertEqual(row["status"], "active" if policy == "latest" else "completed")
            self.assertIsNotNone(row["cursor"])
            if policy == "skip":
                self.assertIn("已错过并跳过", result["display"])

    def test_interval_no_backfill_no_drift_and_gap(self):
        self.create(timed(interval_seconds=120))
        self.check(1)  # Legacy empty progress starts at first future slot, not historical slots.
        self.assertEqual(self.row()["pending_events"], [])
        self.assertEqual(instant(self.row()["next_check_at"]), NOW + timedelta(seconds=120))
        self.check(123)
        self.assertEqual(instant(self.row()["next_check_at"]), NOW + timedelta(seconds=240))
        self.check(12001)
        events = self.row()["pending_events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(instant(events[-1]["occurred_at"]), NOW + timedelta(seconds=12000))
        self.assertEqual(instant(self.row()["next_check_at"]), NOW + timedelta(seconds=12120))

    def test_latest_and_skip_large_gap(self):
        for policy in ("latest", "skip"):
            self.create(timed(interval_seconds=120, missed_policy=policy))
        self.check(1)
        result = self.check(12090)
        self.assertEqual(len(result["enqueued"]), 1)
        self.assertEqual(len(result["skipped"]), 2)
        self.assertTrue(self.row(1)["pending_events"][0]["data"]["is_catch_up"])
        self.assertEqual(self.row(2)["pending_events"], [])
        for id in (1, 2):
            self.assertEqual(instant(self.row(id)["cursor"]), NOW + timedelta(seconds=12000))
            self.assertEqual(instant(self.row(id)["next_check_at"]), NOW + timedelta(seconds=12120))

    def test_cron_preview_matches_runtime(self):
        for tz in ("Asia/Shanghai", "America/New_York"):
            saved = self.create(timed("cron", expression="*/5 * * * *", timezone=tz))
            id = saved["rule"]["id"]
            self.check()
            self.assertEqual(instant(self.row(id)["next_check_at"]), instant(saved["preview"][0]))
            check_once(self.store, instant(saved["preview"][0]) + timedelta(seconds=3))
            self.assertEqual(instant(self.row(id)["pending_events"][0]["occurred_at"]), instant(saved["preview"][0]))
            self.assertEqual(instant(self.row(id)["next_check_at"]), instant(saved["preview"][1]))

    def test_pause_resume_cancel_and_snapshots(self):
        self.create(timed(interval_seconds=120))
        self.check()
        old_event = self.row()["pending_events"][0]
        old_progress = self.row()["next_check_at"]
        self.store.manage("update", 1, dict(name="new", content="new instruction"))
        self.assertEqual(self.row()["next_check_at"], old_progress)
        self.store.manage("pause", 1)
        self.assertEqual(self.check(1000)["enqueued"], [])
        self.assertEqual(self.row()["pending_events"], [old_event])
        self.store.manage("resume", 1)
        self.check(1000)
        self.assertEqual(self.row()["pending_events"][0], old_event)
        self.assertEqual(self.row()["pending_events"][1]["content"], "new instruction")
        self.store.manage("cancel", 1)
        self.assertEqual(self.row()["pending_events"], [])
        self.assertEqual(self.check(2000)["enqueued"], [])

    def test_time_edit_resets_from_edit_time_preserves_event(self):
        self.create(timed(interval_seconds=120))
        self.check()
        event = self.row()["pending_events"][0]
        self.store.clock = lambda: NOW + timedelta(seconds=181)
        self.store.manage("update", 1, dict(trigger_config=timed(interval_seconds=60)["trigger_config"]))
        self.assertEqual(instant(self.row()["next_check_at"]), NOW + timedelta(seconds=240))
        self.assertIsNone(self.row()["cursor"])
        self.assertEqual(self.row()["pending_events"], [event])
        self.check(241)
        self.assertEqual(len(self.row()["pending_events"]), 2)
        self.store.manage("update", 1, dict(trigger_config=timed("once", missed_policy="skip")["trigger_config"]))
        self.check(300)
        self.assertEqual(self.row()["status"], "active")  # Existing queued work prevents completion.
        self.assertEqual(len(self.row()["pending_events"]), 2)

    def test_atomic_rollback_error_isolation_and_recovery(self):
        for _ in range(2):
            self.create(timed("once", at=NOW.isoformat()))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER fail_progress BEFORE UPDATE OF cursor ON automations
                WHEN NEW.id=1 BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        result = self.check()
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(len(result["enqueued"]), 1)
        row = self.row()
        self.assertEqual(row["pending_events"], [])
        self.assertIsNone(row["cursor"])
        self.assertIsNone(row["last_checked_at"])
        self.assertEqual(row["last_error"], "injected failure")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER fail_progress")
        self.check()
        self.assertIsNone(self.row()["last_error"])
        self.assertEqual(len(self.row()["pending_events"]), 1)

    def test_concurrent_checks_and_loop_lock(self):
        self.create(timed("once", at=NOW.isoformat()))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.check(), range(8)))
        self.assertEqual(sum(len(r["enqueued"]) for r in results), 1)
        with loop_lock(self.store):
            with self.assertRaisesRegex(RuntimeError, "已有检查循环"):
                with loop_lock(self.store):
                    pass
        with loop_lock(self.store):
            pass

    def test_state_is_reread_inside_transaction(self):
        self.create(timed("once", at=NOW.isoformat()))
        real_connect = checker.connect
        calls = 0

        def changed_store(store):
            nonlocal calls
            calls += 1
            if calls == 2:
                store.manage("pause", 1)
            return real_connect(store)

        with patch.object(checker, "connect", side_effect=changed_store):
            self.assertEqual(self.check()["enqueued"], [])
        self.assertEqual(self.row()["status"], "paused")
        self.assertIsNone(self.row()["last_checked_at"])

    def test_cron_gap_and_pause_skip(self):
        self.create(timed("cron", expression="*/5 * * * *", missed_policy="skip"))
        self.check()
        self.store.manage("pause", 1)
        self.check(970)
        self.store.manage("resume", 1)
        result = self.check(970)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(self.row()["pending_events"], [])
        self.assertEqual(instant(self.row()["cursor"]), NOW + timedelta(seconds=900))
        self.check(1203)
        self.assertEqual(len(self.row()["pending_events"]), 1)

    def test_cron_dst_preview_is_authoritative(self):
        for start in ("2026-03-07T12:00:00Z", "2026-10-31T12:00:00Z"):
            self.store.clock = lambda: instant(start)
            saved = self.create(timed("cron", expression="30 2 * * *", timezone="America/New_York"))
            id = saved["rule"]["id"]
            check_once(self.store, instant(start))
            for expected in saved["preview"]:
                check_once(self.store, instant(expected) + timedelta(seconds=3))
                self.assertEqual(instant(self.row(id)["pending_events"][-1]["occurred_at"]), instant(expected))

    def test_tolerance_configuration_and_loop_exit(self):
        self.create(timed("once", at=NOW.isoformat(), missed_policy="skip"))
        self.store.settings["AUTOMATION_TOLERANCE_SECONDS"] = 5
        self.assertEqual(len(self.check(6)["skipped"]), 1)
        with self.assertRaises(ValueError):
            self.check(tolerance_seconds=-1)
        with patch.object(checker, "AutomationStore", return_value=self.store), \
             patch("sys.argv", ["checker", "--interval", "2"]), \
             patch.object(checker.time, "sleep", side_effect=KeyboardInterrupt) as sleep, \
             self.assertLogs(level="INFO"):
            self.assertEqual(checker.main(), 0)
        sleep.assert_called_once_with(2)
        with loop_lock(self.store):
            pass

    def test_paused_mail_skipped_no_model_and_diagnostic_chat(self):
        self.create(mail())
        self.store.manage("pause", 1)
        before = self.row()
        with patch("agent.llm.Client.complete", side_effect=AssertionError("model called")):
            self.check()
        self.assertEqual(self.row(), before)
        self.assertEqual(parse_tool_command("/automation pending 1")[1], dict(action="pending", id=1))
        for command in ("/automation pending 0", "/automation check 1", "/automation pending 1 2"):
            with self.assertRaises(ValueError):
                parse_tool_command(command)
        root = self.path.parent / "memory"
        root.mkdir()
        (root / "AGENT.md").write_text("Test", encoding="utf-8")
        output = StringIO()
        with patch("agent.automation_checker.AutomationStore", return_value=self.store), \
             patch("sys.argv", ["agent.main", "--root", str(root)]), \
             patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), \
             patch("agent.main.Client", return_value=SimpleNamespace(model="test")), \
             patch("builtins.input", side_effect=["/automation check", "/automation pending", "/automation pending 1", "/exit"]), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertNotIn("本轮中止", output.getvalue())
        self.assertIn("入队 0", output.getvalue())
