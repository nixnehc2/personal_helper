"""Tests for Tool Context Visibility (conversation vs run_only)."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from agent.tools import FileTools, TOOLS, schema
from agent.main import _filter_run_only_results, _TOOL_VISIBILITY


def _make_client(responses, model="test-model"):
    """Create a client that records debug info and returns canned responses."""
    class Client:
        def __init__(self):
            self.model = model
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
    return Client()


class ToolVisibilityConfigTests(unittest.TestCase):
    def test_default_visibility_is_conversation(self):
        s = schema("test_tool", "desc", {"x": "string"}, ["x"])
        self.assertEqual(s["context_visibility"], "conversation")

    def test_explicit_run_only(self):
        s = schema("test_tool", "desc", {}, [], context_visibility="run_only")
        self.assertEqual(s["context_visibility"], "run_only")

    def test_show_memory_changes_is_run_only(self):
        spec = next(s for s in TOOLS if s["name"] == "show_memory_changes")
        self.assertEqual(spec["context_visibility"], "run_only")

    def test_read_memory_is_conversation(self):
        spec = next(s for s in TOOLS if s["name"] == "read_memory")
        self.assertEqual(spec["context_visibility"], "conversation")

    def test_all_tools_have_visibility(self):
        for spec in TOOLS:
            self.assertIn("context_visibility", spec, f'{spec["name"]} missing context_visibility')
            self.assertIn(spec["context_visibility"], ("conversation", "run_only"))


class FilterFunctionTests(unittest.TestCase):
    """Direct tests for _filter_run_only_results."""

    def test_no_filtering_when_no_run_only(self):
        messages = [
            dict(role="user", content="hi"),
            dict(role="assistant", content=[dict(type="tool_use", id="t1", name="read_memory", input={})]),
            dict(role="user", content=[dict(type="tool_result", tool_use_id="t1", content="{}")]),
            dict(role="assistant", content=[dict(type="text", text="result")]),
            dict(role="user", content="next question"),
        ]
        filtered = _filter_run_only_results(messages, 4)
        self.assertEqual(len(filtered), 5)

    def test_run_only_result_filtered_from_previous_run(self):
        messages = [
            dict(role="user", content="show changes"),
            dict(role="assistant", content=[
                dict(type="tool_use", id="t1", name="show_memory_changes", input={}),
            ]),
            dict(role="user", content=[
                dict(type="tool_result", tool_use_id="t1", content='{"changes": []}'),
            ]),
            dict(role="assistant", content=[dict(type="text", text="no changes")]),
            dict(role="user", content="next question"),  # index 4 = current run start
        ]
        filtered = _filter_run_only_results(messages, 4)
        # The tool_result message at index 2 is dropped (empty content after filtering)
        self.assertEqual(len(filtered), 4)
        # Check the tool result message (index 2) — should have empty content
        # Since content becomes empty, the message is dropped
        result_msgs = [m for m in filtered if m.get("role") == "user" and isinstance(m.get("content"), list)]
        self.assertEqual(len(result_msgs), 0)

    def test_conversation_result_preserved_in_next_run(self):
        messages = [
            dict(role="user", content="read file"),
            dict(role="assistant", content=[
                dict(type="tool_use", id="t1", name="read_memory", input={"path": "self/test.md"}),
            ]),
            dict(role="user", content=[
                dict(type="tool_result", tool_use_id="t1", content='{"content": "file data"}'),
            ]),
            dict(role="assistant", content=[dict(type="text", text="here is the file")]),
            dict(role="user", content="next question"),
        ]
        filtered = _filter_run_only_results(messages, 4)
        # read_memory is conversation — result should persist
        result_msgs = [m for m in filtered if m.get("role") == "user" and isinstance(m.get("content"), list)]
        self.assertEqual(len(result_msgs), 1)
        self.assertEqual(result_msgs[0]["content"][0]["tool_use_id"], "t1")

    def test_mixed_tools_selective_filtering(self):
        messages = [
            dict(role="user", content="check"),
            dict(role="assistant", content=[
                dict(type="tool_use", id="t1", name="show_memory_changes", input={}),
                dict(type="tool_use", id="t2", name="read_memory", input={"path": "self/x.md"}),
            ]),
            dict(role="user", content=[
                dict(type="tool_result", tool_use_id="t1", content='{"changes": []}'),
                dict(type="tool_result", tool_use_id="t2", content='{"content": "data"}'),
            ]),
            dict(role="assistant", content=[dict(type="text", text="done")]),
            dict(role="user", content="next"),
        ]
        filtered = _filter_run_only_results(messages, 4)
        # t1 (run_only) should be filtered; t2 (conversation) should remain
        all_results = [b for m in filtered if isinstance(m.get("content"), list)
                       for b in m["content"] if b.get("type") == "tool_result"]
        self.assertEqual(len(all_results), 1)
        self.assertEqual(all_results[0]["tool_use_id"], "t2")

    def test_current_run_results_not_filtered(self):
        """run_only results within the current run must remain visible."""
        messages = [
            dict(role="user", content="show changes"),  # prev run
            dict(role="assistant", content=[
                dict(type="tool_use", id="t_old", name="show_memory_changes", input={}),
            ]),
            dict(role="user", content=[
                dict(type="tool_result", tool_use_id="t_old", content='{"changes": ["a"]}'),
            ]),
            dict(role="assistant", content=[dict(type="text", text="had changes")]),
            dict(role="user", content="check again"),  # current run start = 4
            dict(role="assistant", content=[
                dict(type="tool_use", id="t_new", name="show_memory_changes", input={}),
            ]),
            dict(role="user", content=[
                dict(type="tool_result", tool_use_id="t_new", content='{"changes": ["b"]}'),
            ]),
        ]
        filtered = _filter_run_only_results(messages, 4)
        # t_old should be filtered, t_new should remain
        all_results = [b for m in filtered if isinstance(m.get("content"), list)
                       for b in m["content"] if b.get("type") == "tool_result"]
        self.assertEqual(len(all_results), 1)
        self.assertEqual(all_results[0]["tool_use_id"], "t_new")

    def test_first_run_no_filtering(self):
        """current_run_start=0 means no previous messages to filter."""
        messages = [
            dict(role="user", content="first ever"),
        ]
        filtered = _filter_run_only_results(messages, 0)
        self.assertEqual(len(filtered), 1)


class IntegrationContextVisibilityTests(unittest.TestCase):
    """End-to-end tests using run_turn with context visibility."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs_dir = self.root / "logs" / "agent_runs"
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        self.files = FileTools(
            self.root,
            lambda changes: {i: c.after for i, c in enumerate(changes)},
            lambda action, changes: "yes",
        )
        self.addCleanup(self.files.policy.close)

    def test_conversation_tool_persists_across_turns(self):
        """read_memory result should be visible in next turn's LLM context."""
        from agent.main import run_turn

        (self.root / "self").mkdir(exist_ok=True)
        (self.root / "self" / "data.md").write_text("important data", encoding="utf-8")

        # Turn 1: call read_memory
        turn1 = _make_client([
            dict(content=[dict(type="tool_use", id="t1", name="read_memory",
                               input={"path": "self/data.md"})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="已读取")], stop_reason="end_turn"),
        ])
        messages = []
        run_turn(turn1, self.files, messages, "read self/data.md",
                 emit=lambda _: None)

        # Turn 2: LLM should see the old tool result in context
        captured_messages = []
        class CaptureClient:
            model = "test"
            def complete(inner, system, msgs, tools):
                captured_messages.extend(msgs)
                return dict(content=[dict(type="text", text="ok")], stop_reason="end_turn")
        run_turn(CaptureClient(), self.files, messages, "what did you read?",
                 emit=lambda _: None)

        # The old read_memory tool_result should be in captured messages
        all_results = [b for m in captured_messages if isinstance(m.get("content"), list)
                       for b in m["content"] if b.get("type") == "tool_result"]
        tool_ids = [r["tool_use_id"] for r in all_results]
        self.assertIn("t1", tool_ids)

    def test_run_only_tool_filtered_from_next_turn(self):
        """show_memory_changes result should NOT be visible in next turn."""
        from agent.main import run_turn

        # Turn 1: call show_memory_changes
        turn1 = _make_client([
            dict(content=[dict(type="tool_use", id="t1", name="show_memory_changes",
                               input={})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="no changes")], stop_reason="end_turn"),
        ])
        messages = []
        run_turn(turn1, self.files, messages, "show changes",
                 emit=lambda _: None)

        # Turn 2: LLM should NOT see the old show_memory_changes result
        captured_messages = []
        class CaptureClient:
            model = "test"
            def complete(inner, system, msgs, tools):
                captured_messages.extend(msgs)
                return dict(content=[dict(type="text", text="ok")], stop_reason="end_turn")
        run_turn(CaptureClient(), self.files, messages, "next question",
                 emit=lambda _: None)

        all_results = [b for m in captured_messages if isinstance(m.get("content"), list)
                       for b in m["content"] if b.get("type") == "tool_result"]
        tool_ids = [r["tool_use_id"] for r in all_results]
        self.assertNotIn("t1", tool_ids)

    def test_history_not_deleted_by_filtering(self):
        """The messages list itself retains run_only results; only LLM input is filtered."""
        from agent.main import run_turn, _filter_run_only_results

        turn1 = _make_client([
            dict(content=[dict(type="tool_use", id="t1", name="show_memory_changes",
                               input={})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="done")], stop_reason="end_turn"),
        ])
        messages = []
        run_turn(turn1, self.files, messages, "show changes",
                 emit=lambda _: None)

        # The raw messages list should still contain the tool result
        all_results = [b for m in messages if isinstance(m.get("content"), list)
                       for b in m["content"] if b.get("type") == "tool_result"]
        self.assertEqual(len(all_results), 1)
        self.assertEqual(all_results[0]["tool_use_id"], "t1")

    def test_current_run_uses_run_only_result(self):
        """Within a single run, run_only tool results are fully usable."""
        from agent.main import run_turn

        # Multi-step: show_memory_changes -> LLM uses result -> text
        chain = _make_client([
            dict(content=[dict(type="tool_use", id="t1", name="show_memory_changes",
                               input={})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="没有待提交的修改")], stop_reason="end_turn"),
        ])
        messages = []
        result = run_turn(chain, self.files, messages, "show changes",
                          emit=lambda _: None)
        # Should complete successfully with the tool result used
        self.assertEqual(result.get("changes"), [])

    def test_no_visibility_defaults_to_conversation(self):
        """Tools without explicit context_visibility default to conversation."""
        self.assertEqual(_TOOL_VISIBILITY.get("read_memory"), "conversation")
        self.assertEqual(_TOOL_VISIBILITY.get("write_memory"), "conversation")
        self.assertEqual(_TOOL_VISIBILITY.get("automation"), "conversation")

    def test_debug_log_records_context_visibility(self):
        """Debug logs should include context_visibility for each tool call."""
        from agent.main import run_turn

        turn1 = _make_client([
            dict(content=[dict(type="tool_use", id="t1", name="show_memory_changes",
                               input={})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="done")], stop_reason="end_turn"),
        ])
        messages = []
        with patch("agent.debug_logger._LOGS_DIR", self.logs_dir):
            run_turn(turn1, self.files, messages, "show changes",
                     emit=lambda _: None)

        files = list(self.logs_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        tool_calls = [s for s in data["steps"] if s["type"] == "tool_call"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["context_visibility"], "run_only")


if __name__ == "__main__":
    unittest.main()
