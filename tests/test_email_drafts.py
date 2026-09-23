import copy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent.email_drafts import DraftStore
from agent.main import main, parse_tool_command, run_turn
from agent.tools import FileTools, TOOLS


def answer(body="张老师您好，周五下午可以参加讨论。", to=None):
    return dict(content=[dict(type="text", text=json.dumps(dict(
        to=to, subject="关于周五讨论", body=body), ensure_ascii=False))], stop_reason="end_turn")


def call(name, arguments):
    return dict(content=[dict(type="tool_use", id="call1", name=name, input=arguments)], stop_reason="tool_use")


class DraftTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "memory"
        self.root.mkdir()
        (self.root / "AGENT.md").write_text("Read _INDEX.md first.", encoding="utf-8")
        (self.root / "_INDEX.md").write_text("称呼张老师，邮件简洁。", encoding="utf-8")
        self.files = FileTools(self.root, lambda _: {}, lambda *_: "no")
        self.path = Path(tmp.name) / "drafts"
        patcher = patch("agent.email_drafts.DRAFTS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def edit(self, client=None, **arguments):
        with self.files.email_context(client or Mock(complete=Mock(return_value=answer()))):
            return self.files.execute("edit_email", dict(instruction="给张老师写邮件", **arguments))

    def test_create_edit_and_explicit_old_draft(self):
        first = self.edit()
        second = self.edit()
        self.assertEqual((first["id"], second["id"]), (1, 2))
        before = (self.path / "2.json").read_bytes()
        changed = self.edit(Mock(complete=Mock(return_value=answer("简短正文"))), draft_id=1)
        self.assertEqual(changed["id"], 1)
        self.assertEqual(changed["created_at"], first["created_at"])
        self.assertNotEqual(changed["updated_at"], first["updated_at"])
        self.assertEqual(DraftStore().read(1)["body"], "简短正文")
        self.assertEqual((self.path / "2.json").read_bytes(), before)
        self.assertEqual(self.files.active_email_draft_id, 1)
        self.assertIsNone(changed["to"])
        self.assertEqual(changed["status"], "draft")
        self.assertIn("简短正文", changed["display"])

    def test_read_shared_temporary_memory_and_forbid_writes_or_send(self):
        self.files.create_file("projects/context.md", "项目 A 候选信息")
        before = copy.deepcopy(self.files.show_memory_changes())
        client = Mock(complete=Mock(side_effect=[
            call("read_file", {"path": "projects/context.md"}),
            call("create_file", {"path": "bad.txt", "content": "bad"}),
            call("send_email", {"draft_id": 1}), answer()]))
        result = self.edit(client)
        self.assertNotIn("error", result)
        transcript = client.complete.call_args.args[1]
        outcomes = [json.loads(m["content"][0]["content"]) for m in transcript
                    if m["role"] == "user" and isinstance(m["content"], list)]
        self.assertIn("项目 A", outcomes[0]["content"])
        self.assertTrue(all("error" in r for r in outcomes[1:]))
        self.assertEqual(self.files.show_memory_changes(), before)
        self.assertNotIn("send_email", {s["name"] for s in TOOLS})

    def test_failures_preserve_draft_and_active_id(self):
        self.edit()
        before = (self.path / "1.json").read_bytes()
        for response in (dict(content=[dict(type="text", text="invalid")], stop_reason="end_turn"),
                         dict(answer(), stop_reason="max_tokens"), answer("")):
            self.assertIn("error", self.edit(Mock(complete=Mock(return_value=response)), draft_id=1))
            self.assertEqual((self.path / "1.json").read_bytes(), before)
        client = Mock()
        for draft_id in (0, -1, True, 99):
            self.assertIn("error", self.edit(client, draft_id=draft_id))
        client.complete.assert_not_called()
        self.assertEqual(self.files.active_email_draft_id, 1)

    def test_conflicting_edit_does_not_overwrite(self):
        self.edit()
        original = DraftStore().read(1)
        DraftStore().save(dict(to=None, subject="new", body="other edit"), original)
        with self.assertRaisesRegex(ValueError, "已被修改"):
            DraftStore().save(dict(to=None, subject="old", body="lost update"), original)
        self.assertEqual(DraftStore().read(1)["body"], "other edit")

    def test_command_and_agent_followup_share_tool_and_context(self):
        client = Mock(model="test")
        client.complete.side_effect = [answer(),
            call("edit_email", {"instruction": "再简短一点", "draft_id": 1}),
            answer("周五下午可以讨论。"),
            dict(content=[dict(type="text", text="已修改 Draft #1")], stop_reason="end_turn")]
        with patch("sys.argv", ["agent.main"]), patch("agent.main.FileTools", return_value=self.files), \
                patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), \
                patch("agent.main.Client", return_value=client), \
                patch("builtins.input", side_effect=["/edit_email 给张老师写邮件", "再简短一点", "/exit"]), \
                patch.object(self.files, "execute", wraps=self.files.execute) as execute, redirect_stdout(StringIO()) as output:
            self.assertEqual(main(), 0)
        self.assertEqual(execute.call_count, 2)
        self.assertIn("active_email_draft_id=1", client.complete.call_args_list[1].args[0])
        self.assertIn("给张老师写邮件", json.dumps(client.complete.call_args_list[2].args[1], ensure_ascii=False))
        self.assertEqual(len(list(self.path.glob("*.json"))), 1)
        self.assertIn("周五下午可以讨论。", output.getvalue())

    def test_agent_creation_and_import_guard(self):
        client = Mock(complete=Mock(side_effect=[call("edit_email", {"instruction": "写邮件"}), answer(),
                      dict(content=[], stop_reason="end_turn")]))
        run_turn(client, self.files, [], "帮我写邮件", emit=lambda _: None)
        self.assertEqual(self.files.active_email_draft_id, 1)
        self.files.processing_eml = True
        self.assertIn("error", self.edit())

    def test_clear_resets_pointer_but_keeps_saved_draft(self):
        self.edit()
        with patch("sys.argv", ["agent.main"]), patch("agent.main.FileTools", return_value=self.files), \
                patch("agent.main.load_config", return_value={"ANTHROPIC_AUTH_TOKEN": "test"}), \
                patch("agent.main.Client", return_value=Mock(model="test")), \
                patch("builtins.input", side_effect=["/clear", "/exit"]), redirect_stdout(StringIO()):
            self.assertEqual(main(), 0)
        self.assertIsNone(self.files.active_email_draft_id)
        self.assertEqual(DraftStore().read(1)["status"], "draft")

    def test_parser(self):
        self.assertEqual(parse_tool_command("/edit_email 12 第二段\n短一点"),
                         ("edit_email", {"draft_id": 12, "instruction": "第二段\n短一点"}))
        for command in ("/edit_email", "/edit_email 12", "/edit_email 0 修改"):
            with self.assertRaises(ValueError):
                parse_tool_command(command)


if __name__ == "__main__":
    unittest.main()
