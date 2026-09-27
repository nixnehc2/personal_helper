"""Tests for the Agent Debug Logger system."""
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

from agent.debug_logger import AgentRun, DebugLogger, current_run, _LOGS_DIR


class AgentRunBasicsTests(unittest.TestCase):
    def test_run_records_llm_input_and_response(self):
        run = AgentRun("rid", "sid", "user", datetime.now().isoformat())
        run.record_llm_input("system-prompt", [{"role": "user", "content": "hi"}], [{"name": "tool1"}], model="test-model")
        run.record_llm_response({"content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn"})
        self.assertEqual(len(run.steps), 2)
        self.assertEqual(run.steps[0]["type"], "llm_request")
        self.assertEqual(run.steps[0]["system"], "system-prompt")
        self.assertEqual(run.steps[0]["model"], "test-model")
        self.assertEqual(run.steps[1]["type"], "llm_response")
        self.assertEqual(run.steps[1]["response"]["stop_reason"], "end_turn")

    def test_run_records_tool_calls_and_results(self):
        run = AgentRun("rid", "sid", "user", datetime.now().isoformat())
        run.record_tool_start("read_memory", "tc1", {"path": "self/info.md"})
        run.record_tool_result("tc1", {"content": "file contents here", "path": "self/info.md"})
        run.record_tool_start("get_current_time", "tc2", {})
        run.record_tool_result("tc2", {"time": "2026-09-28T00:39:31+08:00"})
        self.assertEqual(len(run.steps), 4)
        self.assertEqual(run.steps[0]["tool_name"], "read_memory")
        self.assertEqual(run.steps[0]["arguments"], {"path": "self/info.md"})
        self.assertEqual(run.steps[1]["result"]["content"], "file contents here")
        self.assertEqual(run.steps[2]["tool_name"], "get_current_time")
        self.assertEqual(run.steps[3]["result"]["time"], "2026-09-28T00:39:31+08:00")

    def test_run_records_tool_error(self):
        run = AgentRun("rid", "sid", "user", datetime.now().isoformat())
        run.record_tool_start("bad_tool", "tc1", {"x": 1})
        run.record_tool_result("tc1", {"error": "something broke"}, error="something broke")
        self.assertEqual(run.steps[1]["error"], "something broke")

    def test_finish_sets_status_and_time(self):
        run = AgentRun("rid", "sid", "user", datetime.now().isoformat())
        run.finish(status="success", final_response="done")
        self.assertEqual(run.status, "success")
        self.assertEqual(run.final_response, "done")
        self.assertIsNotNone(run.end_time)

    def test_finish_error_status(self):
        run = AgentRun("rid", "sid", "user", datetime.now().isoformat())
        run.finish(status="error", error="RuntimeError: boom")
        self.assertEqual(run.status, "error")
        self.assertEqual(run.error, "RuntimeError: boom")


class DebugLoggerContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs_dir = Path(self.temp.name) / "logs" / "agent_runs"

    def _patch_logs_dir(self):
        return patch("agent.debug_logger._LOGS_DIR", self.logs_dir)

    def test_run_context_manager_sets_current_run(self):
        logger = DebugLogger()
        with self._patch_logs_dir():
            with logger.run("session1", "user") as run:
                self.assertIsNotNone(current_run.get())
                self.assertEqual(run.trigger_type, "user")
                self.assertEqual(run.session_id, "session1")
            self.assertIsNone(current_run.get())

    def test_run_saves_json_file(self):
        logger = DebugLogger()
        with self._patch_logs_dir():
            with logger.run("session1", "user") as run:
                run.record_llm_input("sys", [], [])
                run.record_llm_response({"content": []})
            files = list(self.logs_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["session_id"], "session1")
            self.assertEqual(data["trigger_type"], "user")
            self.assertEqual(data["status"], "success")
            self.assertEqual(len(data["steps"]), 2)

    def test_run_with_automation_meta(self):
        logger = DebugLogger()
        meta = {"rule_id": 5, "event_id": "evt-123"}
        with self._patch_logs_dir():
            with logger.run("session-auto", "automation", automation_meta=meta) as run:
                run.record_llm_input("sys", [], [])
            files = list(self.logs_dir.glob("*.json"))
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["trigger_type"], "automation")
            self.assertEqual(data["automation_meta"]["rule_id"], 5)
            self.assertEqual(data["automation_meta"]["event_id"], "evt-123")

    def test_run_captures_error_on_exception(self):
        logger = DebugLogger()
        with self._patch_logs_dir():
            with self.assertRaises(ValueError):
                with logger.run("session1", "user") as run:
                    run.record_llm_input("sys", [], [])
                    raise ValueError("test error")
            files = list(self.logs_dir.glob("*.json"))
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "error")
            self.assertIn("ValueError", data["error"])

    def test_initial_context_recorded(self):
        logger = DebugLogger()
        with self._patch_logs_dir():
            with logger.run("s1", "user", initial_context={"user_input": "hello"}) as run:
                pass
            files = list(self.logs_dir.glob("*.json"))
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["initial_context"]["user_input"], "hello")


class LogFailureSafetyTests(unittest.TestCase):
    """Debug logging failures must never crash the Agent."""

    def test_save_failure_swallowed(self):
        run = AgentRun("rid", "sid", "user", datetime.now().isoformat())
        run.record_llm_input("sys", [], [])
        with patch("agent.debug_logger._LOGS_DIR", Path("Z:\\nonexistent\\path\\that\\cant\\exist")):
            run.save()

    def test_record_failure_swallowed(self):
        """Non-serializable data should be silently skipped, not crash."""
        run = AgentRun("rid", "sid", "user", datetime.now().isoformat())
        # JSON can't serialize bare objects; record methods catch exceptions
        run.record_llm_input("sys", [{"bad": object()}], [])
        # The list containing a non-serializable object will fail json.dumps
        # inside save(), but save() catches all exceptions
        run.save()  # Should not raise

    def test_context_manager_save_failure_does_not_crash(self):
        logger = DebugLogger()
        with patch("agent.debug_logger._LOGS_DIR", Path("Z:\\nonexistent")):
            with logger.run("s1", "user") as run:
                run.record_llm_input("sys", [], [])


def _make_debug_client(responses, model="test-model"):
    """Create a client that uses the real Client.complete() debug logging
    but returns fake LLM responses."""
    class DebugClient:
        def __init__(self):
            self.model = model
            self.token = "fake"
            self._responses = list(responses)
            self._idx = 0
        def complete(inner, system, messages, tools):
            from agent.debug_logger import current_run as cr
            run = cr.get(None)
            if run is not None:
                try:
                    run.record_llm_input(system, messages, tools, model=inner.model)
                except Exception:
                    pass
            result = inner._responses[inner._idx]
            inner._idx += 1
            if run is not None:
                try:
                    run.record_llm_response(result)
                except Exception:
                    pass
            return result
    return DebugClient()


class IntegrationWithRunTurnTests(unittest.TestCase):
    """Test that run_turn produces debug logs with the correct structure."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs_dir = self.root / "logs" / "agent_runs"
        (self.root / "AGENT.md").write_text("test protocol", encoding="utf-8")
        from agent.tools import FileTools
        self.files = FileTools(
            self.root,
            lambda changes: {i: c.after for i, c in enumerate(changes)},
            lambda action, changes: "yes",
        )
        self.addCleanup(self.files.policy.close)

    def test_user_turn_produces_debug_log(self):
        from agent.main import run_turn

        client = _make_debug_client([
            dict(content=[dict(type="text", text="这是回复")], stop_reason="end_turn"),
        ])

        with patch("agent.debug_logger._LOGS_DIR", self.logs_dir):
            run_turn(client, self.files, [], "你好",
                     emit=lambda _: None, trigger_type="user",
                     session_id="test-session-1")

        files = list(self.logs_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(data["trigger_type"], "user")
        self.assertEqual(data["session_id"], "test-session-1")
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["initial_context"]["user_input"], "你好")

        llm_requests = [s for s in data["steps"] if s["type"] == "llm_request"]
        self.assertEqual(len(llm_requests), 1)
        self.assertIn("test protocol", llm_requests[0]["system"])
        self.assertIn("Runtime current local time", llm_requests[0]["system"])
        self.assertEqual(llm_requests[0]["model"], "test-model")

        llm_responses = [s for s in data["steps"] if s["type"] == "llm_response"]
        self.assertEqual(len(llm_responses), 1)
        self.assertEqual(llm_responses[0]["response"]["stop_reason"], "end_turn")

    def test_tool_call_captured_with_full_result(self):
        from agent.main import run_turn

        (self.root / "self").mkdir(exist_ok=True)
        (self.root / "self" / "test.md").write_text("secret memory content", encoding="utf-8")

        client = _make_debug_client([
            dict(content=[dict(type="tool_use", id="t1", name="read_memory",
                               input={"path": "self/test.md"})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="读取完毕")], stop_reason="end_turn"),
        ])

        with patch("agent.debug_logger._LOGS_DIR", self.logs_dir):
            run_turn(client, self.files, [], "read self/test.md",
                     emit=lambda _: None)

        files = list(self.logs_dir.glob("*.json"))
        data = json.loads(files[0].read_text(encoding="utf-8"))

        tool_calls = [s for s in data["steps"] if s["type"] == "tool_call"]
        tool_results = [s for s in data["steps"] if s["type"] == "tool_result"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_calls[0]["tool_name"], "read_memory")
        self.assertEqual(tool_calls[0]["arguments"]["path"], "self/test.md")
        result_data = tool_results[0]["result"]
        self.assertIn("secret memory content", json.dumps(result_data))

    def test_multi_step_tool_chain_logged(self):
        """Test LLM -> tool -> LLM -> tool -> LLM chain is fully logged."""
        from agent.main import run_turn

        (self.root / "self").mkdir(exist_ok=True)
        (self.root / "self" / "info.md").write_text("user info", encoding="utf-8")

        client = _make_debug_client([
            dict(content=[dict(type="tool_use", id="t1", name="read_memory",
                               input={"path": "self/info.md"})], stop_reason="tool_use"),
            dict(content=[dict(type="tool_use", id="t2", name="write_memory",
                               input={"path": "self/notes.md", "content": "new note"})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="已完成记录")], stop_reason="end_turn"),
        ])

        with patch("agent.debug_logger._LOGS_DIR", self.logs_dir):
            run_turn(client, self.files, [], "记一下笔记",
                     emit=lambda _: None)

        files = list(self.logs_dir.glob("*.json"))
        data = json.loads(files[0].read_text(encoding="utf-8"))

        llm_req = [s for s in data["steps"] if s["type"] == "llm_request"]
        llm_resp = [s for s in data["steps"] if s["type"] == "llm_response"]
        tc = [s for s in data["steps"] if s["type"] == "tool_call"]
        tr = [s for s in data["steps"] if s["type"] == "tool_result"]
        self.assertEqual(len(llm_req), 3)
        self.assertEqual(len(llm_resp), 3)
        self.assertEqual(len(tc), 2)
        self.assertEqual(len(tr), 2)

        types = [s["type"] for s in data["steps"]]
        self.assertEqual(types, [
            "llm_request", "llm_response", "tool_call", "tool_result",
            "llm_request", "llm_response", "tool_call", "tool_result",
            "llm_request", "llm_response",
        ])


class HiddenToolResultTests(unittest.TestCase):
    """Verify debug logs capture full tool results that UI would not show."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs_dir = self.root / "logs" / "agent_runs"
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        from agent.tools import FileTools
        self.files = FileTools(
            self.root,
            lambda changes: {i: c.after for i, c in enumerate(changes)},
            lambda action, changes: "yes",
        )
        self.addCleanup(self.files.policy.close)

    def test_full_tool_result_in_log_not_in_ui(self):
        """The UI shows '[result] success' but the log has the full result."""
        from agent.main import run_turn

        secret_data = "SENSITIVE_FULL_RESULT: " + "x" * 500

        (self.root / "self").mkdir(exist_ok=True)
        (self.root / "self" / "data.md").write_text(secret_data, encoding="utf-8")

        client = _make_debug_client([
            dict(content=[dict(type="tool_use", id="t1", name="read_memory",
                               input={"path": "self/data.md"})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="已读取")], stop_reason="end_turn"),
        ])

        ui_output = []
        with patch("agent.debug_logger._LOGS_DIR", self.logs_dir):
            run_turn(client, self.files, [], "read data",
                     emit=lambda t: ui_output.append(t))

        ui_text = "\n".join(ui_output)
        self.assertNotIn(secret_data, ui_text)

        files = list(self.logs_dir.glob("*.json"))
        data = json.loads(files[0].read_text(encoding="utf-8"))
        tool_results = [s for s in data["steps"] if s["type"] == "tool_result"]
        self.assertEqual(len(tool_results), 1)
        full_result_json = json.dumps(tool_results[0]["result"], ensure_ascii=False)
        self.assertIn(secret_data, full_result_json)


if __name__ == "__main__":
    unittest.main()
