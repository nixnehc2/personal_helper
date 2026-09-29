import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent.event_runtime import blocks_new_event, USER_WAIT_PHASES, tick, events
from agent.automation_consumer import change_event
from agent.automation_checker import check_once
from agent.scheduler import OSLock, RUNTIME
from agent.tools import FileTools
import test_automations as fixtures


class BlocksNewEventTests(unittest.TestCase):

    def test_running_blocks(self):
        self.assertTrue(blocks_new_event({"active_session": "abc", "phase": "running"}))

    def test_waiting_for_user_does_not_block(self):
        self.assertFalse(blocks_new_event({"active_session": "abc", "phase": "waiting_for_user"}))

    def test_waiting_feedback_does_not_block(self):
        self.assertFalse(blocks_new_event({"active_session": "abc", "phase": "waiting_feedback"}))

    def test_no_active_session_does_not_block(self):
        self.assertFalse(blocks_new_event({"active_session": None, "phase": "running"}))

    def test_delivery_blocks(self):
        self.assertTrue(blocks_new_event({"active_session": "abc", "phase": "delivery"}))


class TickWaitingForUserTests(unittest.TestCase):

    def setUp(self):
        fixtures.AutomationsTests.setUp(self)
        self.root = self.path.parent / "memory"
        self.root.mkdir(parents=True)
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        self.files = FileTools(self.root, lambda _: {}, lambda *args: "no")
        self.addCleanup(self.files.policy.close)
        patcher = patch("agent.event_runtime.RESULTS", self.path.parent / "results")
        patcher.start()
        self.addCleanup(patcher.stop)
        notice = patch("agent.notifications.notify")
        notice.start()
        self.addCleanup(notice.stop)

    def _create_event(self):
        self.store.manage("create", rule=fixtures.timed("once", at=fixtures.NOW.isoformat()))
        check_once(self.store, fixtures.NOW)
        return events(self.store)[0]

    def test_waiting_for_user_does_not_block_new_event(self):
        rule_a, event_a = self._create_event()
        change_event(self.store, rule_a["id"], event_a["event_id"],
                     lambda r, e: e.update(active_session="sa", phase="waiting_for_user"))
        self.store.manage("create", rule=fixtures.timed("once", at=fixtures.NOW.isoformat()))
        check_once(self.store, fixtures.NOW)
        self.assertEqual(len(events(self.store)), 2)
        with patch("agent.event_runtime.launch") as mock_launch:
            mock_launch.return_value = Mock(poll=Mock(return_value=0))
            tick(self.store, self.files)
            mock_launch.assert_called_once()

    def test_waiting_feedback_does_not_block(self):
        rule_a, event_a = self._create_event()
        change_event(self.store, rule_a["id"], event_a["event_id"],
                     lambda r, e: e.update(active_session="sa", phase="waiting_feedback"))
        self.store.manage("create", rule=fixtures.timed("once", at=fixtures.NOW.isoformat()))
        check_once(self.store, fixtures.NOW)
        with patch("agent.event_runtime.launch") as mock_launch:
            mock_launch.return_value = Mock(poll=Mock(return_value=0))
            tick(self.store, self.files)
            mock_launch.assert_called_once()

    def test_running_blocks_new_event(self):
        rule_a, event_a = self._create_event()
        change_event(self.store, rule_a["id"], event_a["event_id"],
                     lambda r, e: e.update(active_session="sa", phase="running"))
        self.store.manage("create", rule=fixtures.timed("once", at=fixtures.NOW.isoformat()))
        check_once(self.store, fixtures.NOW)
        with patch("agent.event_runtime.launch") as mock_launch:
            tick(self.store, self.files)
            mock_launch.assert_not_called()

    def test_waiting_for_user_with_memory_lock_blocks(self):
        rule_a, event_a = self._create_event()
        change_event(self.store, rule_a["id"], event_a["event_id"],
                     lambda r, e: e.update(active_session="sa", phase="waiting_for_user"))
        self.store.manage("create", rule=fixtures.timed("once", at=fixtures.NOW.isoformat()))
        check_once(self.store, fixtures.NOW)
        mem_lock = OSLock(self.root / ".memory-owner.lock")
        self.assertTrue(mem_lock.acquire())
        try:
            with patch("agent.event_runtime.launch") as mock_launch:
                tick(self.store, self.files)
                mock_launch.assert_not_called()
        finally:
            mem_lock.release()

    def test_memory_lock_released_allows_launch(self):
        rule_a, event_a = self._create_event()
        change_event(self.store, rule_a["id"], event_a["event_id"],
                     lambda r, e: e.update(active_session="sa", phase="waiting_for_user"))
        self.store.manage("create", rule=fixtures.timed("once", at=fixtures.NOW.isoformat()))
        check_once(self.store, fixtures.NOW)
        mem_lock = OSLock(self.root / ".memory-owner.lock")
        mem_lock.acquire()
        with patch("agent.event_runtime.launch") as mock_launch:
            tick(self.store, self.files)
            mock_launch.assert_not_called()
        mem_lock.release()
        with patch("agent.event_runtime.launch") as mock_launch:
            mock_launch.return_value = Mock(poll=Mock(return_value=0))
            tick(self.store, self.files)
            mock_launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
