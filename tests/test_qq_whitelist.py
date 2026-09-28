"""Synthetic-only coverage of human selection and checker startup sync."""
from contextlib import closing, nullcontext, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import automation_checker as checker, qq_sync_selector as selector
from agent.qq_conversations import build_conversation_list
from agent.qq_sync import QQStore, update_qq
from test_qq_sync import Client, raw


class Chats(Client):
    def __init__(self):
        super().__init__([raw(1)])
        self.groups = [dict(group_id=100, group_name="A"), dict(group_id=200, group_name="B")]
        self.friends = [dict(user_id=300, nickname="C"), dict(user_id=400, nickname="D")]
        self.histories = []

    def list_group_chats(self): return self.groups
    def list_private_chats(self): return self.friends
    def get_history_page(self, kind, peer, count, cursor):
        self.histories.append((kind, str(peer)))
        return super().get_history_page(kind, peer, count, cursor)


class WhitelistTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "sync_conversations.json"
        self.db = Path(temp.name) / "qq.sqlite3"
        self.client = Chats()

    def configure(self, raw_selection):
        selected = selector.select_conversations(raw_selection, build_conversation_list(self.client))
        selector.save_whitelist(selected, self.path)
        return selector.load_whitelist(self.path)

    def sync(self, allowed=None):
        return update_qq(client=self.client, db_path=self.db, allowed_conversations=allowed)

    def snapshot(self):
        with closing(QQStore(self.db).connect()) as db:
            return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in ("messages", "checkpoints", "conversation_states")}

    def test_indices_saved_as_identities_and_replaced(self):
        self.assertEqual(self.configure("1,3"), {("group", "100"), ("private", "300")})
        saved = json.loads(self.path.read_text(encoding="utf-8"))["conversations"]
        self.assertEqual(saved, [dict(type="group", id="100", name="A"), dict(type="private", id="300", name="C")])
        self.assertEqual(self.configure("2 4 2"), {("group", "200"), ("private", "400")})

    def test_rename_and_insert_do_not_change_selection(self):
        allowed = self.configure("3")
        original = self.path.read_bytes()
        self.client.groups.insert(0, dict(group_id=50, group_name="0-new"))
        self.client.friends[0]["nickname"] = "renamed"
        self.assertEqual(self.sync(allowed)["added"], 1)
        self.assertEqual(self.client.histories, [("private", "300")])
        self.assertEqual(QQStore(self.db).list()[0].content["conversation"]["name"], "renamed")
        self.assertEqual(self.path.read_bytes(), original)

    def test_only_selected_history_and_unselected_state_preserved(self):
        self.sync()
        before = self.snapshot()
        self.client.histories.clear()
        self.client.messages = [raw(2)]
        self.sync(self.configure("2,4"))
        self.assertEqual(self.client.histories, [("group", "200"), ("private", "400")])
        after = self.snapshot()
        self.assertTrue(set(before["messages"]).issubset(set(after["messages"])))
        for scope, checkpoint in before["checkpoints"]:
            if json.loads(scope)[2] in ("100", "300"):
                self.assertIn((scope, checkpoint), after["checkpoints"])

    def test_persistent_skip_wins_and_default_remains_all(self):
        self.sync(set())
        with closing(QQStore(self.db).connect()) as db:
            db.execute("INSERT INTO conversation_states VALUES (?, 'skip')", (json.dumps(["10", "group", "100"]),))
            db.commit()
        self.sync(self.configure("1,3"))
        self.assertEqual(self.client.histories, [("private", "300")])
        self.client.histories.clear()
        self.sync()
        self.assertEqual(set(self.client.histories), {("group", "200"), ("private", "300"), ("private", "400")})

    def test_missing_conversation_warns_and_continues(self):
        allowed = self.configure("1,2,3")
        original = self.path.read_bytes()
        self.client.groups.pop()
        with self.assertLogs(level="WARNING") as logs:
            result = self.sync(allowed)
        self.assertIn("[group] 200", " ".join(logs.output))
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self.client.histories, [("group", "100"), ("private", "300")])
        self.assertEqual(self.path.read_bytes(), original)

    def test_cli_retry_no_and_yes(self):
        self.configure("1,2,3")
        original = self.path.read_bytes()
        for answers in (["1,999", "2,4", "no"], ["2,4", "yes"]):
            output = StringIO()
            with patch.object(selector, "WHITELIST_PATH", self.path), patch.object(selector, "_build_client", return_value=self.client), patch("builtins.input", side_effect=answers), redirect_stdout(output):
                self.assertEqual(selector.main(), 0)
            if answers[-1] == "no":
                self.assertEqual(self.path.read_bytes(), original)
                self.assertIn("编号 999 不存在", output.getvalue())
            else:
                self.assertEqual(selector.load_whitelist(self.path), {("group", "200"), ("private", "400")})

    def test_atomic_failures_preserve_old_file(self):
        self.configure("1")
        original = self.path.read_bytes()
        for target in ("os.replace", "json.dump"):
            with patch("agent.qq_sync_selector." + target, side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    selector.save_whitelist([], self.path)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_missing_empty_and_invalid_never_sync(self):
        for content in (None, '{"conversations": []}', '{', '{}', '{"conversations": [1]}', '{"conversations": [{"type":"group","id":true}]}', '{"conversations": [{"type":"private","id":""}]}'):
            if content is not None:
                self.path.write_text(content, encoding="utf-8")
            with patch("agent.qq_sync.update_qq") as sync, self.assertLogs(level="INFO"):
                checker.qq_startup_sync(whitelist_path=self.path)
            sync.assert_not_called()

    def test_startup_connection_failure_contained(self):
        self.configure("1")
        for failure in (RuntimeError("offline"), dict(failed=1, errors=["offline"])):
            kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with patch("agent.qq_sync.update_qq", **kwargs) as sync, self.assertLogs(level="WARNING"):
                checker.qq_startup_sync(whitelist_path=self.path)
            self.assertEqual(sync.call_args.kwargs["allowed_conversations"], {("group", "100")})

    def test_loop_once_after_lock_and_once_mode_never_syncs(self):
        events = []
        from contextlib import contextmanager
        @contextmanager
        def lock(store):
            events.append("lock")
            yield
        for once in (False, True):
            events.clear()
            with patch.object(checker, "AutomationStore", return_value=SimpleNamespace(settings={})), patch.object(checker, "loop_lock", side_effect=lock), patch.object(checker, "qq_startup_sync", side_effect=lambda _: events.append("qq")) as sync, patch.object(checker, "check_once", side_effect=lambda _: events.append("check") or dict(failed=[], display="ok")) as check, patch.object(checker.time, "sleep", side_effect=[None, KeyboardInterrupt]), patch("sys.argv", ["checker"] + (["--once"] if once else [])):
                self.assertEqual(checker.main(), 0)
            self.assertEqual(events, ["check"] if once else ["lock", "qq", "check", "check"])

    def test_lock_failure_prevents_startup(self):
        with patch.object(checker, "AutomationStore", return_value=SimpleNamespace(settings={})), patch.object(checker, "loop_lock", side_effect=RuntimeError("locked")), patch.object(checker, "qq_startup_sync") as sync, patch("sys.argv", ["checker"]):
            self.assertEqual(checker.main(), 1)
        sync.assert_not_called()

    def test_failed_or_unconfigured_startup_still_runs_loop_without_retry(self):
        for mode in ("missing", "empty", "invalid", "offline"):
            if mode == "empty":
                selector.save_whitelist([], self.path)
            elif mode == "invalid":
                self.path.write_text("{", encoding="utf-8")
            elif mode == "offline":
                self.configure("1")
            with patch.object(selector, "WHITELIST_PATH", self.path), patch.object(checker, "AutomationStore", return_value=SimpleNamespace(settings={})), patch.object(checker, "loop_lock", return_value=nullcontext()), patch("agent.qq_sync.update_qq", side_effect=RuntimeError("offline")) as sync, patch.object(checker, "check_once", return_value=dict(failed=[], display="ok")) as check, patch.object(checker.time, "sleep", side_effect=[None, KeyboardInterrupt]), patch("sys.argv", ["checker"]):
                self.assertEqual(checker.main(), 0)
            self.assertEqual(check.call_count, 2)
            self.assertEqual(sync.call_count, 1 if mode == "offline" else 0)

    def test_shared_discovery_preserves_partial_sync_failure_reporting(self):
        from agent.qq_client import QQClientError
        with patch.object(self.client, "list_group_chats", side_effect=QQClientError("unavailable")):
            with self.assertRaises(QQClientError):
                build_conversation_list(self.client)
            result = self.sync()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.client.histories, [("private", "300"), ("private", "400")])
