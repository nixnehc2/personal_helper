import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.main import run_turn
from agent.tools import FileTools
from agent.llm import Client


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proposals = []
        self.files = FileTools(self.root, lambda path, diff: self.proposals.append(diff) or True)
        (self.root / "note.md").write_text("alpha\nbeta\n", encoding="utf-8", newline="")

    def test_approval_and_unique_edit(self):
        self.files.replace_text("note.md", "alpha", "gamma")
        self.assertEqual((self.root / "note.md").read_text(), "gamma\nbeta\n")
        self.assertIn("-alpha", self.proposals[0])
        self.assertIn("+gamma", self.proposals[0])

    def test_denial_blocks_subsequent_writes(self):
        self.files.confirm = lambda *args: False
        result = self.files.execute("create_file", dict(path="new/one.md", content="new"))
        self.assertIn("declined", result["error"])
        self.assertFalse((self.root / "new").exists())
        self.files.confirm = lambda *args: True
        self.assertIn("disabled", self.files.execute("create_file", dict(path="two.md", content="x"))["error"])

    def test_create_never_overwrites(self):
        self.assertIn("exists", self.files.execute("create_file", dict(path="note.md", content="oops"))["error"])
        self.assertEqual(self.proposals, [])

    def test_missing_multiple_empty_and_overlapping_matches(self):
        (self.root / "note.md").write_text("aaa", encoding="utf-8")
        for old in ("", "missing", "a", "aa"):
            self.assertIn("error", self.files.execute("replace_text", dict(path="note.md", old_text=old, new_text="x")))
        self.assertEqual((self.root / "note.md").read_text(), "aaa")

    def test_paths(self):
        for path in ("../outside.md", "a/../../outside", "C:\\outside", "/etc/passwd", "\\\\server\\share", "note.md:stream", "NUL", "folder. /note.md"):
            for name, extra in (("read_file", {}), ("list_directory", {}), ("search_files", {"query": "x"}),
                                ("create_file", {"content": "x"}), ("replace_text", {"old_text": "a", "new_text": "x"})):
                self.assertIn("error", self.files.execute(name, dict(path=path, **extra)), (name, path))

    def test_hardlink_is_rejected(self):
        link = self.root / "linked.md"
        link.hardlink_to(self.root / "note.md")
        self.assertIn("hard links", self.files.execute("read_file", {"path": "linked.md"})["error"])

    def test_changed_during_confirmation(self):
        def confirm(*args):
            (self.root / "note.md").write_text("human edit", encoding="utf-8")
            return True
        self.files.confirm = confirm
        result = self.files.execute("replace_text", dict(path="note.md", old_text="alpha", new_text="gamma"))
        self.assertIn("changed", result["error"])
        self.assertEqual((self.root / "note.md").read_text(), "human edit")

    def test_search_and_pagination(self):
        result = self.files.search_files("ALPHA")
        self.assertEqual(result["matches"][0]["line"], 1)
        self.assertEqual(self.files.read_file("note.md", 2, 1)["content"], "beta\n")
        self.assertTrue(self.files.read_file("note.md", 1, 1)["truncated"])

    def test_crlf_preserved(self):
        (self.root / "note.md").write_bytes(b"alpha\r\nbeta\r\n")
        self.files.replace_text("note.md", "alpha", "gamma")
        self.assertEqual((self.root / "note.md").read_bytes(), b"gamma\r\nbeta\r\n")

    def test_malformed_call(self):
        self.assertIn("error", self.files.execute("execute_shell", {}))
        self.assertIn("error", self.files.execute("read_file", {"path": 12}))
        self.assertIn("error", self.files.execute("read_file", {"path": "note.md", "extra": True}))

    def test_loop_returns_tool_results_and_preserves_context(self):
        (self.root / "AGENT.md").write_text("Read indexes first", encoding="utf-8")
        responses = [
            dict(content=[dict(type="tool_use", id="r1", name="read_file", input={"path": "note.md"})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="alpha [note.md]")], stop_reason="end_turn"),
        ]
        class FakeClient:
            def complete(inner, system, messages, tools):
                self.assertIn("Read indexes first", system)
                if len(responses) == 1:
                    result = messages[-1]["content"][0]
                    self.assertEqual(result["tool_use_id"], "r1")
                    self.assertIn("alpha", json.loads(result["content"])["content"])
                return responses.pop(0)
        messages = []
        run_turn(FakeClient(), self.files, messages, "What is in my note?", emit=lambda _: None)
        self.assertEqual(messages[-1]["role"], "assistant")

    def test_truncated_response_does_not_execute_writes(self):
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        class FakeClient:
            def complete(inner, *args):
                return dict(content=[dict(type="tool_use", id="w1", name="create_file",
                                          input=dict(path="bad.md", content="x"))], stop_reason="max_tokens")
        with self.assertRaises(RuntimeError):
            run_turn(FakeClient(), self.files, [], "record x")
        self.assertFalse((self.root / "bad.md").exists())


class ClientTests(unittest.TestCase):
    def test_payload_and_auth(self):
        with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "https://example.com/v1"}):
            client = Client("test-token")
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'{"content":[],"stop_reason":"end_turn"}'
        with patch.object(client.opener, "open", return_value=Response()) as mocked:
            client.complete("protocol", [{"role": "user", "content": "hi"}], [])
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/v1/messages")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(json.loads(request.data)["messages"][0]["content"], "hi")


if __name__ == "__main__":
    unittest.main()
