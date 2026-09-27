"""Real OS-lock/process tests plus offline event lifecycle fault injection."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent.tools import FileTools
from agent.scheduler import OSLock, RUNTIME
from agent.event_runtime import (run_event, tick, events, management, BackgroundConsumer,
                                 claim_lock, launch)
from agent.automation_consumer import change_event
from agent.automation_checker import check_once
import test_automations as fixtures


def call(name, **args):
    return dict(content=[dict(type="tool_use", id="tool", name=name, input=args)], stop_reason="tool_use")


def reply(text):
    return dict(content=[dict(type="text", text=text)], stop_reason="end_turn")


class LockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        (self.root / "projects").mkdir()
        (self.root / "projects/a.md").write_text("old", encoding="utf-8")
        self.files = FileTools(self.root, lambda _: {}, lambda *args: "yes")
        self.addCleanup(self.files.policy.close)

    def child(self, code, *args):
        process = subprocess.Popen([sys.executable, "-u", "-c", code, str(self.root), *map(str,args)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8")
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        return process

    def test_real_process_turns_are_serial(self):
        code = '''import sys,time
from agent.tools import FileTools
f=FileTools(sys.argv[1])
with f.policy.scheduler.turn(lambda _:None):
 print('entered',flush=True)
 input()
'''
        first = self.child(code)
        self.assertEqual(first.stdout.readline().strip(), "entered")
        marker = self.root / "entered"
        second = self.child('''import sys
from pathlib import Path
from agent.tools import FileTools
f=FileTools(sys.argv[1])
with f.policy.scheduler.turn(lambda _:None):
 Path(sys.argv[2]).touch()
''', marker)
        time.sleep(.3)
        self.assertFalse(marker.exists())
        first.communicate("\n", timeout=10)
        second.communicate(timeout=10)
        self.assertEqual(second.returncode, 0)
        self.assertTrue(marker.exists())

    def test_crashed_owner_releases_and_never_merges(self):
        owner = self.child('''import sys
from agent.tools import FileTools
f=FileTools(sys.argv[1])
f.write_memory('pending/lost.md','unapproved')
print('ready',flush=True)
input()
''')
        self.assertEqual(owner.stdout.readline().strip(), "ready")
        other = FileTools(self.root)
        self.assertEqual(other.policy.discard(explicit=True)["status"], "no_changes")
        self.assertNotIn("content", other.execute("read_memory", dict(path="pending/lost.md")))
        owner.kill()
        owner.communicate(timeout=10)
        with other.policy.scheduler.turn(lambda _: None):
            self.assertFalse(other.policy._state()["active"])
            self.assertFalse((self.root / ".memory-temporary").exists())
        self.assertFalse((self.root / "pending/lost.md").exists())

    def test_no_keeps_lease_cancel_releases_and_other_reads_formal(self):
        self.files.policy.confirm_transaction = lambda *args: "no"
        self.files.replace_text("projects/a.md", "old", "candidate")
        other = FileTools(self.root)
        self.assertEqual(other.read_memory("projects/a.md")["content"], "old")
        self.assertEqual(self.files.commit_memory_changes()["status"], "not_approved")
        probe = OSLock(self.root / ".memory-owner.lock")
        self.assertFalse(probe.acquire())
        self.files.policy.discard(explicit=True)
        self.assertTrue(probe.acquire())
        probe.release()

    def test_commit_failure_rolls_back_and_retains_transaction(self):
        self.files.replace_text("projects/a.md", "old", "new")
        self.files.write_memory("projects/b.md", "new file")
        original = self.files.policy._sync_tree
        def fail(source, target, preserve_runtime):
            if source == self.root / ".memory-temporary" and target == self.root:
                (self.root / "projects/a.md").write_text("partial", encoding="utf-8")
                raise OSError("disk full")
            return original(source, target, preserve_runtime)
        with patch.object(self.files.policy, "_sync_tree", side_effect=fail):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.files.commit_memory_changes()
        self.assertEqual((self.root / "projects/a.md").read_text(), "old")
        self.assertFalse((self.root / "projects/b.md").exists())
        self.assertTrue(self.files.policy._active())
        self.assertEqual(self.files.read_memory("projects/a.md")["content"], "new")
        self.assertEqual(self.files.commit_memory_changes()["status"], "committed")

    def test_crash_mid_commit_recovers_before_read(self):
        child = self.child('''import os,sys
from pathlib import Path
from agent.tools import FileTools
f=FileTools(sys.argv[1],lambda _: {},lambda *args:'yes')
f.replace_text('projects/a.md','old','new')
original=f.policy._sync_tree
def crash(source,target,preserve_runtime):
 if source == f.root / '.memory-temporary' and target == f.root:
  (f.root/'projects/a.md').write_text('partial')
  os._exit(23)
 return original(source,target,preserve_runtime)
f.policy._sync_tree=crash
f.commit_memory_changes()
''')
        child.communicate(timeout=10)
        self.assertEqual(child.returncode, 23)
        reopened = FileTools(self.root)
        self.assertEqual(reopened.read_memory("projects/a.md")["content"], "old")
        self.assertFalse((self.root / ".memory-commit.json").exists())

    def test_submitted_user_has_priority_over_automatic_turn(self):
        ticket = self.files.policy.scheduler.ticket()
        self.addCleanup(lambda: self.files.policy.scheduler.unticket(ticket))
        marker = self.root / "automatic-entered"
        child = self.child('''import sys
from pathlib import Path
from agent.tools import FileTools
f=FileTools(sys.argv[1]); f.policy.scheduler.automatic=True
with f.policy.scheduler.turn(lambda _:None):
 Path(sys.argv[2]).touch()
''', marker)
        time.sleep(.3)
        self.assertFalse(marker.exists())
        self.files.policy.scheduler.unticket(ticket)
        child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0)
        self.assertTrue(marker.exists())

    def test_resumed_chat_refreshes_previous_memory_retrieval(self):
        from agent.main import run_turn
        messages = []
        client = Mock()
        client.complete.side_effect = [call("read_memory", path="projects/a.md"), reply("old")]
        run_turn(client, self.files, messages, "read", emit=lambda _:None)
        other = FileTools(self.root, lambda _: {}, lambda *args:"yes")
        other.replace_text("projects/a.md", "old", "new")
        other.commit_memory_changes()
        client.complete.side_effect = [reply("new")]
        run_turn(client, self.files, messages, "continue", emit=lambda _:None)
        evidence = json.loads(messages[2]["content"][0]["content"])
        self.assertTrue(evidence["runtime_refreshed"])
        self.assertEqual(evidence["content"], "new")


class EventTests(unittest.TestCase):
    def setUp(self):
        fixtures.AutomationsTests.setUp(self)
        self.root = self.path.parent / "memory"
        self.root.mkdir(parents=True)
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        self.files = FileTools(self.root, lambda _: {}, lambda *args: "no")
        self.addCleanup(self.files.policy.close)
        self.store.manage("create", rule=fixtures.timed("once", at=fixtures.NOW.isoformat()))
        check_once(self.store, fixtures.NOW)
        self.event_id = events(self.store)[0][1]["event_id"]
        self.results = self.path.parent / "results"
        patcher = patch("agent.event_runtime.RESULTS", self.results)
        patcher.start()
        self.addCleanup(patcher.stop)
        notice = patch("agent.notifications.notify")
        self.notice = notice.start()
        self.addCleanup(notice.stop)

    def event(self):
        return events(self.store)[0][1]

    def run_event(self, responses, read=lambda _: "/exit", automatic=True):
        client = Mock()
        client.complete.side_effect = responses
        run_event(client, self.files, self.store, 1, self.event_id, None,
                  emit=lambda _: None, read=read, automatic=automatic)
        return client

    def test_complete_saves_full_reply_notifies_removes(self):
        self.run_event([call("complete_event", reply="完整结果\n第二行")])
        self.assertEqual(events(self.store), [])
        saved = json.loads(next(self.results.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["reply"], "完整结果\n第二行")
        self.assertEqual(saved["event_id"], self.event_id)
        self.notice.assert_called_once()
        self.assertEqual(self.store.manage("get", 1)["rule"]["status"], "completed")

    def test_notify_failure_retries_without_model_or_duplicate_file(self):
        self.notice.side_effect = OSError("notification unavailable")
        client = self.run_event([call("complete_event", reply="done")])
        self.assertEqual(self.event()["reply"], "done")
        self.assertGreater(self.event()["retry_at"], time.time())
        tick(self.store, self.files)
        self.assertEqual(self.notice.call_count, 1)
        self.notice.side_effect = None
        change_event(self.store, 1, self.event_id, lambda r,e:e.update(retry_at=0))
        tick(self.store, self.files)
        self.assertEqual(events(self.store), [])
        self.assertEqual(len(list(self.results.glob("*.json"))), 1)
        client.complete.assert_called_once()

    def test_result_file_failure_retains_reply_and_retries_delivery_only(self):
        with patch("agent.event_runtime.atomic_json", side_effect=OSError("disk full")):
            client = self.run_event([call("complete_event", reply="saved final")])
        self.assertEqual(self.event()["reply"], "saved final")
        self.notice.assert_not_called()
        change_event(self.store, 1, self.event_id, lambda r,e:e.update(retry_at=0))
        tick(self.store, self.files)
        self.assertEqual(events(self.store), [])
        client.complete.assert_called_once()
        self.notice.assert_called_once()

    def test_feedback_is_not_final_and_exit_suspends_until_resume(self):
        def read(_):
            current = self.event()
            self.assertIsNone(current["reply"])
            self.assertTrue(current["active_session"])
            with patch("agent.event_runtime.launch") as start:
                tick(self.store, self.files)
                start.assert_not_called()
            return "/exit"
        self.run_event([reply("请提供地址")], read)
        self.assertTrue(self.event()["suspended"])
        self.assertIsNone(self.event()["active_session"])
        self.assertTrue(self.event()["messages"])
        with patch("agent.event_runtime.launch") as start:
            tick(self.store, self.files)
            start.assert_not_called()
        management("/automation event-resume " + self.event_id, self.files, self.store, lambda _:None)
        self.assertFalse(self.event()["suspended"])

    def test_confirmation_keeps_execution_lock_and_no_keeps_memory(self):
        def confirm(*args):
            execution = OSLock(RUNTIME / "execution.lock")
            self.assertFalse(execution.acquire())
            return "no"
        self.files.policy.confirm_transaction = confirm
        def read(_):
            execution = OSLock(RUNTIME / "execution.lock")
            self.assertTrue(execution.acquire())
            execution.release()
            memory = OSLock(self.root / ".memory-owner.lock")
            self.assertFalse(memory.acquire())
            # Rule checking remains independent of Agent/Memory locks.
            self.assertEqual(check_once(self.store, fixtures.NOW)["failed"], [])
            return "/exit"
        self.run_event([call("write_memory", path="pending/a.md", content="candidate"),
                        call("commit_memory_changes"), reply("请反馈")], read)
        self.assertTrue(self.event()["suspended"])
        self.assertFalse((self.root / "pending/a.md").exists())
        self.assertFalse(self.files.policy._active())

    def test_feedback_without_memory_releases_execution(self):
        def read(_):
            other = FileTools(self.root)
            with other.policy.scheduler.turn(lambda _: None):
                self.assertFalse(other.policy._active())
            return "地址如下"
        self.run_event([reply("请提供地址"), call("complete_event", reply="完成")], read)
        self.assertEqual(events(self.store), [])

    def test_feedback_without_memory_allows_another_event(self):
        self.store.manage("create", rule=fixtures.timed("once", at=fixtures.NOW.isoformat()))
        check_once(self.store, fixtures.NOW)
        def read(_):
            with patch("agent.event_runtime.launch", return_value=Mock(pid=77)) as start:
                tick(self.store, self.files)
                self.assertEqual(start.call_count, 1)
                self.assertNotEqual(start.call_args.args[3]["event_id"], self.event_id)
            return "/exit"
        self.run_event([reply("请补充")], read)

    def test_completion_tool_not_available_to_nested_email_agent(self):
        self.files.event_session = True
        self.assertIn("complete_event", [t["name"] for t in self.files.tool_specs])
        self.files.processing_eml = True
        self.assertNotIn("complete_event", [t["name"] for t in self.files.tool_specs])
        self.assertIn("error", self.files.execute("complete_event", dict(reply="premature")))

    def test_manual_commit_error_keeps_event_and_transaction_for_cancel(self):
        reads = iter(["/commit", "/cancel", "/exit"])
        count = []
        def read(_):
            count.append(1)
            if len(count) == 2:
                self.assertTrue(self.files.policy._active())
            return next(reads)
        with patch.object(self.files.policy, "request_commit", side_effect=OSError("disk failure")):
            self.run_event([call("write_memory", path="pending/a.md", content="candidate"), reply("请提交")], read)
        self.assertTrue(self.event()["suspended"])
        self.assertEqual(len(count), 3)
        self.assertFalse(self.files.policy._active())

    def test_start_failure_backoff_and_active_claim_prevents_duplicate(self):
        with patch("agent.event_runtime.launch", side_effect=OSError("cannot open")) as start:
            tick(self.store, self.files)
            tick(self.store, self.files)
            start.assert_called_once()
        self.assertIsNone(self.event()["active_session"])
        self.assertIn("cannot open", self.event()["last_error"])

    def test_background_consumes_while_main_thread_is_idle(self):
        import threading
        started = threading.Event()
        def start(*args):
            started.set()
            change_event(self.store, 1, self.event_id, lambda r,e:e.update(active_session="launch", launch_at=time.time()))
            return Mock(poll=lambda: None)
        background = BackgroundConsumer(self.files, self.store)
        with patch("agent.event_runtime.launch", side_effect=start) as launch_mock:
            background.start()
            try:
                self.assertTrue(started.wait(5))
                time.sleep(1.2)
                launch_mock.assert_called_once()
            finally:
                background.close()

    def test_pausing_does_not_block_saved_reply_delivery(self):
        change_event(self.store, 1, self.event_id, lambda r,e:e.update(reply="saved", completed_at=time.time()))
        management("/automation auto pause", self.files, self.store, lambda _:None)
        try:
            tick(self.store, self.files)
            self.assertEqual(events(self.store), [])
        finally:
            management("/automation auto resume", self.files, self.store, lambda _:None)

    def test_launch_is_visible_and_has_independent_entrypoint(self):
        rule, event = events(self.store)[0]
        with patch("agent.event_runtime.subprocess.Popen", return_value=Mock(pid=1234)) as popen:
            launch(self.store, self.root, rule, event)
        args, options = popen.call_args
        self.assertIn("--event", args[0])
        self.assertIn(self.event_id, args[0])
        self.assertEqual(options["creationflags"], subprocess.CREATE_NEW_CONSOLE)
        self.assertEqual(self.event()["pid"], 1234)


if __name__ == "__main__":
    unittest.main()
