"""Persistent deadlines, offline network boundaries and checker integration."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent import automation_checker as checker, qq_sync_schedule as schedule
from agent.automations import AutomationStore
from agent.qq_sync import QQStore, update_qq
from agent.qq_sync_selector import save_whitelist
from test_qq_whitelist import Chats

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / "qq/messages.sqlite3"
        self.whitelist = self.base / "qq/sync_conversations.json"
        self.store = AutomationStore(self.base / "automations.sqlite3", {}, lambda: NOW)
        self.client = Chats()
        save_whitelist([dict(type="group", id="200", name="old B"),
                        dict(type="private", id="400", name="old D")], self.whitelist)
        for target, value in (("agent.qq_sync.DB_PATH", self.db),
                              ("agent.qq_sync_selector.WHITELIST_PATH", self.whitelist)):
            guard = patch(target, value)
            guard.start()
            self.addCleanup(guard.stop)

    def due(self, seconds=0, config=None):
        schedule.check_qq_sync_due(config or {}, NOW + timedelta(seconds=seconds), self.db, self.whitelist)

    def deadline(self):
        return schedule.get_next_sync_at(self.db)

    def test_first_due_ticks_and_new_process_restart(self):
        with patch("agent.qq_sync.QQClient", return_value=self.client) as client, patch("builtins.input", side_effect=AssertionError("stdin forbidden")):
            for second in range(0, 601, 10):
                self.due(second)
                if second == 0:
                    self.assertEqual(self.deadline(), NOW + timedelta(seconds=600))
            self.assertEqual(client.call_count, 2)
        self.assertEqual(self.client.histories, [("group", "200"), ("private", "400")] * 2)
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=1200))
        code = """from agent.qq_sync_schedule import check_qq_sync_due
from unittest.mock import patch
from datetime import datetime
import sys
with patch('agent.qq_sync.QQClient', side_effect=AssertionError('unexpected connection')) as client:
 check_qq_sync_due({}, datetime.fromisoformat(sys.argv[3]), sys.argv[1], sys.argv[2])
 assert client.call_count == 0
"""
        subprocess.run([sys.executable, "-c", code, str(self.db), str(self.whitelist),
                        (NOW + timedelta(seconds=780)).isoformat()], check=True, capture_output=True)
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=1200))

    def test_not_due_does_not_load_whitelist_or_log(self):
        schedule.set_next_sync_at(NOW + timedelta(seconds=600), self.db)
        with patch("agent.qq_sync_selector.load_whitelist") as load, patch("agent.qq_sync.update_qq") as sync, self.assertNoLogs(level="INFO"):
            self.due(300)
        load.assert_not_called()
        sync.assert_not_called()

    def test_retry_exception_and_failed_result_once_per_minute(self):
        for failure in (RuntimeError("offline"), dict(failed=1, errors=["partial failure"])):
            schedule.set_next_sync_at(NOW, self.db)
            options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with patch("agent.qq_sync.update_qq", **options) as sync:
                for second in (0, 10, 20, 59):
                    self.due(second)
                self.assertEqual(sync.call_count, 1)
                self.assertEqual(self.deadline(), NOW + timedelta(seconds=60))
                self.due(60)
                self.assertEqual(sync.call_count, 2)
            self.assertEqual(self.deadline(), NOW + timedelta(seconds=120))

    def test_missing_empty_and_invalid_whitelist_backoff(self):
        for content, delay in ((None, 600), ('{"conversations": []}', 600), ('{', 60),
                               ('{"conversations": [1]}', 60)):
            schedule.set_next_sync_at(NOW, self.db)
            if content is None:
                self.whitelist.unlink(missing_ok=True)
            else:
                self.whitelist.write_text(content, encoding="utf-8")
            with patch("agent.qq_sync.update_qq") as sync, self.assertLogs(level="INFO") as logs:
                self.due()
            sync.assert_not_called()
            self.assertIn("[QQ]", " ".join(logs.output))
            self.assertEqual(self.deadline(), NOW + timedelta(seconds=delay))

    def test_configured_intervals_and_invalid_configuration(self):
        with patch("agent.qq_sync.update_qq", return_value=dict(failed=0)):
            self.due(config=dict(QQ_SYNC_INTERVAL_SECONDS="900", QQ_SYNC_RETRY_SECONDS="30"))
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=900))
        with patch("agent.qq_sync.update_qq", side_effect=RuntimeError("offline")):
            self.due(900, dict(QQ_SYNC_INTERVAL_SECONDS=900, QQ_SYNC_RETRY_SECONDS=30))
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=930))
        for value in (0, -1, "nan", "inf", "invalid"):
            with patch("agent.qq_sync.update_qq") as sync, self.assertLogs(level="WARNING"):
                self.due(930, dict(QQ_SYNC_INTERVAL_SECONDS=value))
            sync.assert_not_called()

    def test_local_config_and_manual_environment_use_same_interval(self):
        from agent.llm import load_config
        config_path = self.base / "config.json"
        config_path.write_text(json.dumps(dict(QQ_SYNC_INTERVAL_SECONDS="900", QQ_SYNC_RETRY_SECONDS="30")), encoding="utf-8")
        config = load_config(config_path)
        with patch("agent.qq_sync.update_qq", return_value=dict(failed=0)):
            self.due(config=config)
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=900))
        with patch("agent.llm.load_config", return_value=config), patch.dict("os.environ", {"QQ_SYNC_INTERVAL_SECONDS": "1200"}), patch("agent.qq_sync.QQClient", return_value=self.client), patch.object(schedule, "utc_now", return_value=NOW + timedelta(seconds=300)):
            self.assertEqual(update_qq(db_path=self.db)["failed"], 0)
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=1200))
        with patch("agent.llm.load_config", return_value={}), patch.dict("os.environ", {"QQ_SYNC_INTERVAL_SECONDS": "1200"}), patch("agent.qq_sync.QQClient", return_value=self.client), patch.object(schedule, "utc_now", return_value=NOW + timedelta(seconds=300)):
            self.assertEqual(update_qq(db_path=self.db)["failed"], 0)
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=1500))

    def test_persistent_skip_and_missing_peer_keep_whitelist(self):
        original = self.whitelist.read_bytes()
        with closing(QQStore(self.db).connect()) as db, db:
            db.execute("INSERT INTO conversation_states VALUES (?, 'skip')", (json.dumps(["10", "private", "400"]),))
        with patch("agent.qq_sync.QQClient", return_value=self.client):
            self.due()
        self.assertEqual(self.client.histories, [("group", "200")])
        self.client.groups.pop()
        with patch("agent.qq_sync.QQClient", return_value=self.client), self.assertLogs(level="WARNING") as logs:
            self.due(600)
        self.assertIn("[group] 200", " ".join(logs.output))
        self.assertEqual(self.whitelist.read_bytes(), original)
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=1200))

    def test_manual_success_postpones_but_failure_does_not(self):
        schedule.set_next_sync_at(NOW + timedelta(seconds=600), self.db)
        with patch.object(schedule, "utc_now", return_value=NOW + timedelta(seconds=300)):
            result = update_qq(client=self.client, db_path=self.db, config={})
        self.assertEqual(result["failed"], 0)
        self.assertEqual(set(self.client.histories), {("group", "100"), ("group", "200"), ("private", "300"), ("private", "400")})
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=900))
        from agent.qq_client import QQClientError
        for target in ("get_login_info", "get_history_page"):
            with patch.object(self.client, target, side_effect=QQClientError("failed")):
                self.assertGreater(update_qq(client=self.client, db_path=self.db)["failed"], 0)
            self.assertEqual(self.deadline(), NOW + timedelta(seconds=900))

    def test_concurrent_due_claim_and_manual_deadline_not_overwritten(self):
        # Initialize schema before racing callers; SQLite serializes claims.
        self.deadline()
        with patch("agent.qq_sync.update_qq", return_value=dict(failed=0)) as sync:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: self.due(), range(2)))
        sync.assert_called_once()
        schedule.set_next_sync_at(NOW, self.db)
        def manual_during_automatic(**kwargs):
            with patch.object(schedule, "utc_now", return_value=NOW + timedelta(seconds=300)):
                schedule.record_manual_success({}, self.db)
            return dict(failed=0)
        with patch("agent.qq_sync.update_qq", side_effect=manual_during_automatic):
            self.due()
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=900))

    def test_timezone_and_invalid_persisted_deadline(self):
        schedule.set_next_sync_at(NOW.astimezone(timezone(timedelta(hours=8))) + timedelta(seconds=600), self.db)
        with patch("agent.qq_sync.update_qq") as sync:
            self.due(599)
        sync.assert_not_called()
        with closing(QQStore(self.db).connect()) as db, db:
            db.execute("UPDATE sync_state SET value='broken'")
        with patch("agent.qq_sync.update_qq", return_value=dict(failed=0)) as sync, self.assertLogs(level="WARNING"):
            self.due()
        sync.assert_called_once()
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=600))

    def test_check_once_and_main_share_due_check_under_loop_lock(self):
        with patch("agent.qq_sync.QQClient", return_value=self.client), patch("builtins.input", side_effect=AssertionError("stdin")):
            checker.check_once(self.store, NOW)
            checker.check_once(self.store, NOW + timedelta(seconds=10))
        self.assertEqual(len(self.client.histories), 2)
        events = []
        @contextmanager
        def lock(store):
            events.append("lock")
            yield
        for once in (False, True):
            events.clear()
            with patch.object(checker, "AutomationStore", return_value=self.store), patch.object(checker, "loop_lock", side_effect=lock), patch.object(checker, "check_qq_sync_due", side_effect=lambda *args: events.append("qq_due")), patch.object(checker.time, "sleep", side_effect=[None, KeyboardInterrupt]), patch("sys.argv", ["checker"] + (["--once"] if once else [])):
                self.assertEqual(checker.main(), 0)
            self.assertEqual(events, ["qq_due"] if once else ["lock", "qq_due", "qq_due"])
        with patch.object(checker, "AutomationStore", return_value=self.store), patch.object(checker, "loop_lock", side_effect=RuntimeError("locked")), patch.object(checker, "check_qq_sync_due") as due, patch("sys.argv", ["checker"]):
            self.assertEqual(checker.main(), 1)
        due.assert_not_called()

    def test_qq_network_and_storage_failure_does_not_stop_timer_or_loop(self):
        import test_automations as fixtures
        self.store.manage("create", rule=fixtures.timed("once", at=NOW.isoformat()))
        with patch("agent.qq_sync.update_qq", side_effect=RuntimeError("offline")) as sync, patch.object(checker, "AutomationStore", return_value=self.store), patch.object(checker.time, "sleep", side_effect=[None, KeyboardInterrupt]), patch("sys.argv", ["checker"]):
            self.assertEqual(checker.main(), 0)
        sync.assert_called_once()
        self.assertEqual(len(checker.pending(self.store)["events"]), 1)
        self.assertEqual(self.deadline(), NOW + timedelta(seconds=60))
        with patch.object(schedule, "_claim_due", side_effect=OSError("disk")), self.assertLogs(level="WARNING"):
            self.assertEqual(checker.check_once(self.store, NOW)["failed"], [])
