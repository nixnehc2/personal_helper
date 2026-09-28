"""Offline persistence, validation and shared-entrypoint acceptance tests."""
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent.automations import AutomationStore
from agent.main import main, parse_tool_command, run_turn
from agent.tools import FileTools

NOW = datetime(2026, 9, 28, 0, 30, tzinfo=timezone.utc)


def timed(kind="interval", **extra):
    configs = {
        "once": {"at": "2026-09-28T08:00:00+08:00"},
        "interval": {"start_at": "2026-09-28T08:00:00+08:00", "interval_seconds": 5400},
        "cron": {"expression": "0 20 * * 1,3,5", "timezone": "Asia/Shanghai"},
    }
    return dict(name="提醒", trigger_type="schedule", content="提醒用户提交申请", trigger_config={
        "schedule_type": kind, "missed_policy": "latest", **configs[kind], **extra})


def mail():
    return dict(name="监控申请回复", trigger_type="event", source="email", content="总结导师关于申请的回复", mode="once",
                trigger_config=dict(scope=dict(account_id="me@example.com", folder="INBOX"),
                                    match=dict(from_addresses=["teacher@example.com"], subject_contains="申请",
                                               reply_to_message_id="<original@example.com>"), check_interval_seconds=300))


class AutomationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "data/automations.sqlite3"
        self.store = AutomationStore(self.path, {"EMAIL_ACCOUNT": "me@example.com"}, lambda: NOW)
        # Checker now polls QQ too; all fixtures remain isolated from personal data.
        for target, value in (("agent.qq_sync.DB_PATH", self.path.parent / "qq/messages.sqlite3"),
                              ("agent.qq_sync_selector.WHITELIST_PATH", self.path.parent / "qq/sync_conversations.json")):
            guard = patch(target, value)
            guard.start()
            self.addCleanup(guard.stop)

    def create(self, rule=None):
        return self.store.manage("create", rule=rule or timed())

    def test_all_kinds_and_utc_reserved_fields(self):
        for rule in (timed("once", at="2026-09-29T08:00:00+08:00"), timed(), timed("cron"), mail()):
            saved = self.create(rule)["rule"]
            self.assertEqual(self.store.manage("get", saved["id"])["rule"], saved)
            self.assertTrue(saved["created_at"].endswith("+00:00"))
            for key in ("next_check_at", "cursor", "last_checked_at", "last_error"):
                self.assertIsNone(saved[key])
            self.assertEqual(saved["pending_events"], [])
        self.assertEqual(len(self.store.manage("list")["rules"]), 4)

    def test_preview_anchor_timezone_weekdays_and_no_mutation(self):
        result = self.create()
        self.assertEqual(result["preview"], ["2026-09-28T09:30:00+08:00", "2026-09-28T11:00:00+08:00", "2026-09-28T12:30:00+08:00"])
        before = self.path.read_bytes()
        self.store.manage("get", result["rule"]["id"])
        self.assertEqual(self.path.read_bytes(), before)
        cron = self.create(timed("cron"))
        self.assertEqual(cron["preview"], ["2026-09-28T20:00:00+08:00", "2026-09-30T20:00:00+08:00", "2026-10-02T20:00:00+08:00"])
        self.assertEqual(self.create(timed("once"))["preview"], [])
        future = self.create(timed("once", at="2026-09-29T08:00:00+08:00"))
        self.assertEqual(len(future["preview"]), 1)
        self.assertEqual(future["rule"]["trigger_config"]["at"], "2026-09-29T00:00:00+00:00")
        sunday = self.create(timed("cron", expression="0 20 * * 0"))
        self.assertEqual(sunday["preview"][0], "2026-10-04T20:00:00+08:00")
        self.assertEqual(sunday["preview"], self.create(timed("cron", expression="0 20 * * 7"))["preview"])
        ny = self.create(timed("cron", timezone="America/New_York"))
        self.assertEqual(ny["preview"][0], "2026-09-28T20:00:00-04:00")

    def test_restart_in_fresh_process_and_transitions(self):
        id = self.create()["rule"]["id"]
        self.store.manage("update", id, dict(name="更名", content="新的完整指令"))
        for action, status in (("pause", "paused"), ("resume", "active"), ("cancel", "cancelled")):
            first = self.store.manage(action, id)["rule"]
            self.assertEqual(first["status"], status)
            self.assertEqual(self.store.manage(action, id)["rule"], first)
            code = "from agent.automations import AutomationStore; import sys,json; print(json.dumps(AutomationStore(sys.argv[1], {}).manage('get', 1)['rule']))"
            row = json.loads(subprocess.check_output([sys.executable, "-c", code, str(self.path)], text=True))
            self.assertEqual(row["status"], status)
            self.assertEqual(row["name"], "更名")
        with self.assertRaisesRegex(ValueError, "不能恢复"):
            self.store.manage("resume", id)

    def test_invalid_configs_and_atomic_updates(self):
        id = self.create()["rule"]["id"]
        initial = self.store.manage("get", id)["rule"]
        bad = [timed("cron", expression="0 0 0 * * *"), timed("cron", expression="61 * * * *"),
               timed("cron", expression="0 0 30 2 *"), timed("cron", timezone="Mars/Olympus"),
               timed(interval_seconds=0), timed(interval_seconds=-1), timed(interval_seconds=True),
               timed(interval_seconds=float("inf")), timed("once", at="2026-09-28T08:00:00"),
               timed(missed_policy="all")]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.create(item)
            with self.assertRaises(ValueError):
                self.store.manage("update", id, dict(name="不应保存", trigger_config=item["trigger_config"]))
            self.assertEqual(self.store.manage("get", id)["rule"], initial)
        self.assertEqual(len(self.store.manage("list")["rules"]), 1)
        for changes in ({"mode": "once"}, {"status": "completed"}, {"cursor": "x"}, {"name": ""}):
            with self.assertRaises(ValueError):
                self.store.manage("update", id, changes)
        for action in ("get", "update", "pause", "resume", "cancel"):
            with self.assertRaisesRegex(ValueError, "不存在"):
                self.store.manage(action, 999, {"name": "x"} if action == "update" else None)
        changed = self.store.manage("update", id, {"trigger_config": timed("once")["trigger_config"]})
        self.assertEqual(changed["rule"]["mode"], "once")

    def test_email_validation_no_network(self):
        for key, value in (("match", {"from_addresses": ["bad"]}),
                           ("scope", {"account_id": "other@example.com", "folder": "INBOX"}),
                           ("check_interval_seconds", 0), ("match", {"unknown": "x"})):
            rule = mail()
            rule["trigger_config"][key] = value
            with self.assertRaises(ValueError):
                self.create(rule)
        rule = mail()
        rule["source"] = "qq"
        with self.assertRaises(ValueError):
            self.create(rule)

    def test_real_chat_command_loop(self):
        root = Path(self.tmp.name) / "memory"
        root.mkdir()
        (root / "AGENT.md").write_text("Test", encoding="utf-8")
        files = FileTools(root, lambda _: {}, lambda *args: "no")
        commands = ["/automation create " + json.dumps(timed()), "/clear", "/automation list", "/automation get 1", "/exit"]
        output = StringIO()
        with patch("agent.automations.AutomationStore", return_value=self.store), \
                patch("sys.argv", ["agent.main"]), \
                patch("agent.main.FileTools", return_value=files), \
                patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), \
                patch("agent.main.Client", return_value=SimpleNamespace(model="test")), \
                patch("builtins.input", side_effect=commands), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn("#1", output.getvalue())
        self.assertIn("Windows 通知", output.getvalue())
        self.assertNotIn("本轮中止", output.getvalue())

    def test_agent_and_chat_commands_share_store(self):
        root = Path(self.tmp.name) / "memory"
        root.mkdir()
        (root / "AGENT.md").write_text("Test", encoding="utf-8")
        files = FileTools(root, lambda _: {}, lambda *args: "no")
        with patch("agent.automations.AutomationStore", return_value=self.store):
            responses = [dict(content=[dict(type="tool_use", id="a", name="automation", input=dict(action="create", rule=timed()))], stop_reason="tool_use"),
                         dict(content=[dict(type="text", text="已保存")], stop_reason="end_turn")]
            class Client:
                def complete(self, system, messages, specs):
                    assert any(s["name"] == "automation" for s in specs)
                    return responses.pop(0)
            run_turn(Client(), files, [], "每隔90分钟提醒", emit=lambda _: None)
            name, args = parse_tool_command("/automation list")
            self.assertEqual(len(files.execute(name, args)["rules"]), 1)
            for command in ('/automation update 1 {"name":"改名"}', '/automation pause 1', '/automation resume 1', '/automation cancel 1', '/automation get 1'):
                name, args = parse_tool_command(command)
                self.assertNotIn("error", files.execute(name, args))
            self.assertIn("error", files.execute("automation", dict(action="resume", id=1)))
            self.assertEqual(files.policy.changes, {})
            files.processing_eml = True
            self.assertIn("error", files.execute("automation", dict(action="create", rule=timed())))


if __name__ == "__main__":
    unittest.main()
