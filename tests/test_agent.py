import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.main import confirm_transaction
from agent.main import HISTORY_NAME, RunHistory, run_turn
from agent.main import parse_email_command
from agent.tools import FileTools
from agent.llm import Client


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / HISTORY_NAME

    def test_appends_multiple_sessions_to_one_file(self):
        first = RunHistory(self.path, session_id="first")
        first.append("session_start", model="test-model", root=str(self.root))
        first.append("turn_complete", user="你好", messages=[
            dict(role="user", content="你好"),
            dict(role="assistant", content=[dict(type="text", text="我在")]),
        ])
        second = RunHistory(self.path, session_id="second")
        second.append("session_start", model="test-model", root=str(self.root))

        records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["event"] for record in records],
                         ["session_start", "turn_complete", "session_start"])
        self.assertEqual([record["session_id"] for record in records],
                         ["first", "first", "second"])
        self.assertEqual(records[1]["messages"][1]["content"][0]["text"], "我在")

    def test_history_is_private_from_memory_tools(self):
        files = FileTools(self.root, lambda changes: {}, lambda action, changes: "yes")
        RunHistory(self.path).append("session_start")
        result = files.execute("read_memory", dict(path=HISTORY_NAME))
        self.assertIn("error", result)
        self.assertNotIn(HISTORY_NAME, files.policy.changes)


class DisplayTests(unittest.TestCase):
    def test_commit_confirmation_omits_raw_email_diff(self):
        raw_diff = "BASE64_DIFF" * 10000
        output = StringIO()
        with patch("builtins.input", return_value="no"), redirect_stdout(output):
            result = confirm_transaction("commit", [
                SimpleNamespace(path="inbox/email/archive.eml", action="create", diff=raw_diff),
                SimpleNamespace(path="pending/candidate.md", action="create", diff="+candidate"),
            ])
        self.assertEqual(result, "no")
        self.assertIn("[原始邮件归档] diff 已省略", output.getvalue())
        self.assertIn("+candidate", output.getvalue())
        self.assertNotIn("BASE64_DIFF", output.getvalue())


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = FileTools(self.root, lambda changes: {i: c.after for i, c in enumerate(changes)}, lambda action, changes: "yes")
        (self.root / "projects").mkdir(exist_ok=True)
        (self.root / "projects/note.md").write_text("alpha\nbeta\n", encoding="utf-8", newline="")

    def test_approval_and_unique_edit(self):
        result = self.files.replace_text("projects/note.md", "alpha", "gamma")
        self.assertEqual((self.root / "projects/note.md").read_text(), "alpha\nbeta\n")
        self.files.commit_memory_changes()
        self.assertEqual((self.root / "projects/note.md").read_text(), "gamma\nbeta\n")
        self.assertIn("-alpha", result["diff"])
        self.assertIn("+gamma", result["diff"])

    def test_denial_retains_every_folder(self):
        self.files.policy.confirm_transaction = lambda action, changes: "no"
        self.files.write_memory("self/one.md", "new")
        self.files.write_memory("self/two.md", "new")
        self.files.write_memory("projects/note2.md", "temporary")
        result = self.files.commit_memory_changes()
        self.assertEqual(result["status"], "not_approved")
        self.assertEqual(len(self.files.policy.changes), 3)
        self.assertFalse((self.root / "self/one.md").exists())
        self.assertFalse((self.root / "projects/note2.md").exists())

    def test_create_never_overwrites(self):
        self.assertIn("exists", self.files.execute("write_memory", dict(path="projects/note.md", content="oops"))["error"])

    def test_missing_multiple_empty_and_overlapping_matches(self):
        (self.root / "projects/note.md").write_text("aaa", encoding="utf-8")
        for old in ("", "missing", "a", "aa"):
            self.assertIn("error", self.files.execute("replace_text", dict(path="projects/note.md", old_text=old, new_text="x")))
        self.assertEqual((self.root / "projects/note.md").read_text(), "aaa")

    def test_paths(self):
        for path in ("../outside.md", "a/../../outside", "C:\\outside", "/etc/passwd", "\\\\server\\share", "note.md:stream", "NUL", "folder. /note.md"):
            for name, extra in (("read_memory", {}), ("list_directory", {}), ("search_files", {"query": "x"}),
                                ("write_memory", {"content": "x"}), ("replace_text", {"old_text": "a", "new_text": "x"})):
                self.assertIn("error", self.files.execute(name, dict(path=path, **extra)), (name, path))

    def test_email_command_preserves_windows_paths_and_flags(self):
        self.assertEqual(parse_email_command('/email C:\\mail\\a.eml'),
                         (r"C:\mail\a.eml", False, False))
        self.assertEqual(parse_email_command('/email "C:\\mail with spaces\\a.eml" --authored-by-user --reprocess'),
                         (r"C:\mail with spaces\a.eml", True, True))
        self.assertEqual(parse_email_command('/email C:\\mail\\a.eml --force'),
                         (r"C:\mail\a.eml", False, True))
        self.assertEqual(parse_email_command("not an email command"), None)

    def test_hardlink_is_rejected(self):
        link = self.root / "linked.md"
        link.hardlink_to(self.root / "projects/note.md")
        self.assertIn("hard links", self.files.execute("read_memory", {"path": "linked.md"})["error"])

    def test_changed_during_confirmation(self):
        (self.root / "self").mkdir()
        target = self.root / "self/note.md"
        target.write_text("alpha", encoding="utf-8")
        def confirm(changes):
            target.write_text("human edit", encoding="utf-8")
            return {0: changes[0].after}
        self.files.policy.confirm_batch = confirm
        self.files.replace_text("self/note.md", "alpha", "gamma")
        result = self.files.execute("commit_memory_changes", {})
        self.assertIn("changed", result["error"])
        self.assertEqual(target.read_text(), "human edit")

    def test_search_and_pagination(self):
        result = self.files.search_files("ALPHA")
        self.assertEqual(result["matches"][0]["line"], 1)
        self.assertEqual(self.files.read_memory("projects/note.md", 2, 1)["content"], "beta\n")
        self.assertTrue(self.files.read_memory("projects/note.md", 1, 1)["truncated"])

    def test_crlf_preserved(self):
        (self.root / "projects/note.md").write_bytes(b"alpha\r\nbeta\r\n")
        self.files.replace_text("projects/note.md", "alpha", "gamma")
        self.files.commit_memory_changes()
        self.assertEqual((self.root / "projects/note.md").read_bytes(), b"gamma\r\nbeta\r\n")

    def test_malformed_call(self):
        self.assertIn("error", self.files.execute("execute_shell", {}))
        self.assertIn("error", self.files.execute("read_memory", {"path": 12}))
        self.assertIn("error", self.files.execute("read_memory", {"path": "projects/note.md", "extra": True}))

    def test_loop_returns_tool_results_and_preserves_context(self):
        (self.root / "AGENT.md").write_text("Read indexes first", encoding="utf-8")
        responses = [
            dict(content=[dict(type="tool_use", id="r1", name="read_memory", input={"path": "projects/note.md"})], stop_reason="tool_use"),
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

    def test_system_prompt_includes_root_index_and_retrieval_rules(self):
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        (self.root / "_INDEX.md").write_text("root navigation", encoding="utf-8")
        captured = {}
        class FakeClient:
            def complete(inner, system, messages, tools):
                captured["system"] = system
                captured["tools"] = tools
                return dict(content=[dict(type="text", text="ok")], stop_reason="end_turn")

        run_turn(FakeClient(), self.files, [], "What are my current projects?", emit=lambda _: None)

        self.assertIn("Before answering, asking a clarification question, or making a tool call", captured["system"])
        self.assertIn("Clarification is a fallback after retrieval fails", captured["system"])
        self.assertIn("Knowledge-base root index (_INDEX.md):\nroot navigation", captured["system"])
        self.assertIn("Root index entries are navigation, not sufficient evidence", captured["system"])
        descriptions = {spec["name"]: spec["description"] for spec in captured["tools"]}
        self.assertIn("Required before citing a Memory fact", descriptions["read_memory"])
        self.assertIn("Use when already loaded indexes do not locate relevant Memory", descriptions["search_files"])


class MemoryRetrievalPolicyTests(unittest.TestCase):
    @staticmethod
    def tool_names(messages):
        return [block["name"] for message in messages
                if isinstance(message.get("content"), list)
                for block in message["content"]
                if isinstance(block, dict) and block.get("type") == "tool_use"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = FileTools(self.root, lambda changes: {}, lambda action, changes: "yes")
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")

    def test_memory_value_is_used_for_generated_filename(self):
        (self.root / "_INDEX.md").write_text("self: self/_INDEX.md", encoding="utf-8")
        (self.root / "self").mkdir()
        (self.root / "self/_INDEX.md").write_text("profile: self/profile.md", encoding="utf-8")
        (self.root / "self/profile.md").write_text("姓名：陈新\n学号：241502026\n", encoding="utf-8")
        self.files = FileTools(self.root, lambda changes: {}, lambda action, changes: "yes")
        captured = {}
        responses = [
            dict(content=[dict(type="tool_use", id="read", name="read_memory",
                               input={"path": "self/profile.md"})], stop_reason="tool_use"),
            dict(content=[dict(type="tool_use", id="create", name="create_file",
                               input={"filename": "陈新-241502026.pdf", "content": "作业内容"})],
                 stop_reason="tool_use"),
            dict(content=[dict(type="text", text="已生成")], stop_reason="end_turn"),
        ]

        class FakeClient:
            def complete(inner, system, messages, tools):
                captured["system"] = system
                if len(messages) == 3:
                    result = json.loads(messages[-1]["content"][0]["content"])
                    self.assertIn("陈新", result["content"])
                    self.assertIn("241502026", result["content"])
                return responses.pop(0)

        with patch.object(self.files, "create_file", return_value=dict(success=True, path="generated_files/陈新-241502026.pdf")) as create_file:
            messages = []
            run_turn(FakeClient(), self.files, messages, "请按照姓名-学号.pdf生成文件", emit=lambda _: None)

        create_file.assert_called_once_with(filename="陈新-241502026.pdf", content="作业内容")
        self.assertIn("Knowledge-base self index (self/_INDEX.md):\nprofile: self/profile.md", captured["system"])
        self.assertEqual(self.tool_names(messages), ["read_memory", "create_file"])

    def test_conversation_value_is_used_without_memory_search(self):
        (self.root / "_INDEX.md").write_text("root navigation", encoding="utf-8")
        responses = [
            dict(content=[dict(type="tool_use", id="create", name="create_file",
                               input={"filename": "陈新-241502026.pdf", "content": "作业内容"})],
                 stop_reason="tool_use"),
            dict(content=[dict(type="text", text="已生成")], stop_reason="end_turn"),
        ]

        class FakeClient:
            def complete(inner, system, messages, tools):
                return responses.pop(0)

        with patch.object(self.files, "create_file", return_value=dict(success=True, path="generated_files/陈新-241502026.pdf")):
            messages = []
            run_turn(FakeClient(), self.files, messages, "我的姓名是陈新，学号是241502026；请生成姓名-学号.pdf", emit=lambda _: None)

        self.assertEqual(self.tool_names(messages), ["create_file"])

    def test_missing_memory_value_allows_user_question_after_search(self):
        (self.root / "_INDEX.md").write_text("root navigation", encoding="utf-8")
        responses = [
            dict(content=[dict(type="tool_use", id="search", name="search_files",
                               input={"query": "学号"})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="未找到学号，请提供学号")], stop_reason="end_turn"),
        ]

        class FakeClient:
            def complete(inner, system, messages, tools):
                if len(messages) >= 3:
                    result = json.loads(messages[-1]["content"][0]["content"])
                    self.assertEqual(result["matches"], [])
                return responses.pop(0)

        messages = []
        run_turn(FakeClient(), self.files, messages, "请按照姓名-学号.pdf生成文件", emit=lambda _: None)
        self.assertEqual(self.tool_names(messages), ["search_files"])
        self.assertIn("请提供学号", messages[-1]["content"][0]["text"])

    def test_conflicting_memory_values_require_user_confirmation(self):
        (self.root / "_INDEX.md").write_text("self: self/_INDEX.md", encoding="utf-8")
        (self.root / "self").mkdir()
        (self.root / "self/_INDEX.md").write_text("profile A: self/profile-a.md\nprofile B: self/profile-b.md", encoding="utf-8")
        (self.root / "self/profile-a.md").write_text("学号：241502026\n", encoding="utf-8")
        (self.root / "self/profile-b.md").write_text("学号：241502027\n", encoding="utf-8")
        self.files = FileTools(self.root, lambda changes: {}, lambda action, changes: "yes")
        responses = [
            dict(content=[
                dict(type="tool_use", id="a", name="read_memory", input={"path": "self/profile-a.md"}),
                dict(type="tool_use", id="b", name="read_memory", input={"path": "self/profile-b.md"}),
            ], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="Memory 中存在两个学号，请确认应使用哪一个")], stop_reason="end_turn"),
        ]

        class FakeClient:
            def complete(inner, system, messages, tools):
                if len(messages) >= 3:
                    values = [json.loads(block["content"])["content"] for block in messages[-1]["content"]]
                    self.assertIn("241502026", "".join(values))
                    self.assertIn("241502027", "".join(values))
                return responses.pop(0)

        messages = []
        run_turn(FakeClient(), self.files, messages, "请按照姓名-学号.pdf生成文件", emit=lambda _: None)
        self.assertEqual(self.tool_names(messages), ["read_memory", "read_memory"])
        self.assertIn("请确认", messages[-1]["content"][0]["text"])

class ToolLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = FileTools(self.root, lambda changes: {i: c.after for i, c in enumerate(changes)}, lambda action, changes: "yes")
        (self.root / "projects").mkdir(exist_ok=True)
        (self.root / "projects/note.md").write_text("alpha\nbeta\n", encoding="utf-8", newline="")

    def test_useful_candidate_memory_is_staged_and_committed(self):
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        responses = [
            dict(content=[
                dict(type="tool_use", id="p1", name="write_memory",
                     input={"path": "self/preference.md", "content": "Candidate long-term preference\n"}),
                dict(type="tool_use", id="p2", name="write_memory",
                     input={"path": "projects/status.md", "content": "Candidate project status\n"}),
                dict(type="tool_use", id="p3", name="write_memory",
                     input={"path": "areas/focus.md", "content": "Candidate ongoing focus topic\n"}),
                dict(type="tool_use", id="p4", name="write_memory",
                     input={"path": "pending/preference.md", "content": "Candidate preference; not yet specific enough\n"}),
            ], stop_reason="tool_use"),
            dict(content=[dict(type="tool_use", id="c1", name="commit_memory_changes", input={})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="已提交候选修改")], stop_reason="end_turn"),
        ]
        captured = {}
        class CandidateClient:
            def complete(inner, system, messages, tools):
                captured["system"] = system
                captured["tools"] = tools
                return responses.pop(0)
        result = run_turn(CandidateClient(), self.files, [], "remember these candidate facts", emit=lambda _: None)
        self.assertEqual(result["status"], "no_changes")
        self.assertTrue((self.root / "self/preference.md").exists())
        self.assertTrue((self.root / "projects/status.md").exists())
        self.assertTrue((self.root / "areas/focus.md").exists())
        self.assertTrue((self.root / "pending/preference.md").exists())
        self.assertIn("Proactively stage potentially useful durable information", captured["system"])
        self.assertIn("write or update pending/ first", captured["system"])
        self.assertIn("casual chat, or unsupported speculation", captured["system"])
        descriptions = {spec["name"]: spec["description"] for spec in captured["tools"]}
        self.assertIn("只修改 Temporary Memory，不会直接修改 Formal Memory。", descriptions["write_memory"])
        self.assertIn("只修改 Temporary Memory，不会直接修改 Formal Memory。", descriptions["edit_memory"])
        self.assertIn("只修改 Temporary Memory，不会直接修改 Formal Memory。", descriptions["delete_memory"])
        self.assertIn("当前修改完成，请进入用户 review。", descriptions["commit_memory_changes"])

    def test_ordinary_knowledge_answer_does_not_create_memory(self):
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        class AnswerOnlyClient:
            def complete(inner, system, messages, tools):
                self.assertIn("do not mechanically store ordinary knowledge", system)
                return dict(content=[dict(type="text", text="这是普通知识回答")], stop_reason="end_turn")
        messages = []
        result = run_turn(AnswerOnlyClient(), self.files, messages, "What is a hash table?", emit=lambda _: None)
        self.assertEqual(result["changes"], [])
        self.assertFalse(any(block.get("type") == "tool_use"
                             for message in messages
                             if isinstance(message.get("content"), list)
                             for block in message["content"]))

    def test_user_no_retains_candidates_for_revision(self):
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        self.files.policy.confirm_transaction = lambda action, changes: "no"
        responses = [
            dict(content=[dict(type="tool_use", id="w1", name="write_memory",
                               input={"path": "projects/status.md", "content": "draft status\n"})], stop_reason="tool_use"),
            dict(content=[dict(type="tool_use", id="c1", name="commit_memory_changes", input={})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="已请求审阅")], stop_reason="end_turn"),
        ]
        run_turn(type("Client", (), {"complete": staticmethod(lambda *args: responses.pop(0))})(),
                 self.files, [], "project update", emit=lambda _: None)
        self.assertFalse((self.root / "projects/status.md").exists())
        self.assertIn("projects/status.md", self.files.policy.changes)
        self.files.policy.confirm_transaction = lambda action, changes: "yes"
        revision = [
            dict(content=[dict(type="tool_use", id="e1", name="replace_text",
                               input={"path": "projects/status.md", "old_text": "draft status", "new_text": "revised status"})],
                 stop_reason="tool_use"),
            dict(content=[dict(type="tool_use", id="c2", name="commit_memory_changes", input={})], stop_reason="tool_use"),
            dict(content=[dict(type="text", text="已提交修订")], stop_reason="end_turn"),
        ]
        result = run_turn(type("Client", (), {"complete": staticmethod(lambda *args: revision.pop(0))})(),
                          self.files, [], "correct the status", emit=lambda _: None)
        self.assertEqual(result["status"], "no_changes")
        self.assertEqual((self.root / "projects/status.md").read_text(encoding="utf-8"), "revised status\n")

    def test_truncated_response_does_not_execute_writes(self):
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        class FakeClient:
            def complete(inner, *args):
                return dict(content=[dict(type="tool_use", id="w1", name="write_memory",
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
