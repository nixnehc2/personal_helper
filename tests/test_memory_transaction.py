"""Minimal tests for the complete Temporary Memory transaction lifecycle."""
import json
from pathlib import Path
import tempfile
import unittest

from agent.tools import FileTools
from test_email_memory import ScriptClient, create


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "memory"
        self.root.mkdir()
        (self.root / "AGENT.md").write_text("protocol", encoding="utf-8")
        (self.root / "projects").mkdir()
        (self.root / "projects/a.md").write_text("original", encoding="utf-8")
        self.answer = "yes"
        self.approvals = []
        self.files = self.open_files()

    def open_files(self):
        def approve(action, changes):
            self.approvals.append((action, [change.path for change in changes]))
            return self.answer

        return FileTools(self.root, lambda changes: {index: change.after for index, change in enumerate(changes)}, approve)

    def snapshot(self, root):
        files = {}
        for path in root.rglob("*"):
            if path.is_file():
                relative = path.relative_to(root)
                if not relative.parts[0].startswith(".memory-"):
                    files[relative.as_posix()] = path.read_bytes()
        return files

    def assert_synchronized(self):
        self.assertEqual(self.snapshot(self.root), self.snapshot(self.files.workspace_root))

    def state(self):
        return json.loads((self.root / ".memory-transaction.json").read_text(encoding="utf-8"))

    def test_startup_and_first_edit_enter_one_transaction(self):
        self.assert_synchronized()
        self.assertFalse(self.state()["active"])
        self.files.replace_text("projects/a.md", "original", "edited")
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.files.read_memory("projects/a.md")["content"], "edited")
        self.assertEqual((self.root / "projects/a.md").read_text(encoding="utf-8"), "original")

    def test_modify_accept_makes_formal_equal_temporary(self):
        self.files.replace_text("projects/a.md", "original", "edited")
        self.files.create_file("projects/new.md", "new")
        self.files.commit_memory_changes()
        self.assertEqual((self.root / "projects/a.md").read_text(encoding="utf-8"), "edited")
        self.assertEqual((self.root / "projects/new.md").read_text(encoding="utf-8"), "new")
        self.assertFalse(self.state()["active"])
        self.assert_synchronized()

    def test_no_retains_temporary_for_revision_then_yes_commits(self):
        self.answer = "no"
        self.files.create_file("projects/b.md", "first")
        self.assertEqual(self.files.commit_memory_changes()["status"], "not_approved")
        self.assertEqual(self.files.read_memory("projects/b.md")["content"], "first")
        self.assertFalse((self.root / "projects/b.md").exists())

        self.answer = "yes"
        self.files.replace_text("projects/b.md", "first", "revised")
        self.files.commit_memory_changes()
        self.assertEqual((self.root / "projects/b.md").read_text(encoding="utf-8"), "revised")
        self.assert_synchronized()
        self.assertEqual([action for action, _ in self.approvals], ["commit", "commit"])

    def test_explicit_discard_restores_formal_to_temporary(self):
        self.files.create_file("projects/b.md", "temporary")
        self.files.discard_memory_changes()
        self.assertIn("error", self.files.execute("read_file", {"path": "projects/b.md"}))
        self.assertFalse(self.state()["active"])
        self.assert_synchronized()

    def test_pending_uses_the_same_transaction(self):
        self.files.create_file("pending/preference.md", "candidate")
        self.assertEqual((self.files.workspace_root / "pending/preference.md").read_text(encoding="utf-8"), "candidate")
        self.assertFalse((self.root / "pending/preference.md").exists())
        self.files.commit_memory_changes()
        self.assertEqual((self.root / "pending/preference.md").read_text(encoding="utf-8"), "candidate")
        self.assert_synchronized()

    def test_show_omits_raw_email_archive_diff(self):
        archive = self.files.policy.archive_email(b"raw email")
        result = self.files.show_memory_changes()
        change = next(item for item in result["changes"] if item["path"] == archive["path"])
        self.assertIsNone(change["diff"])
        self.assertTrue(change["diff_omitted"])

        self.files.create_file("pending/candidate.md", "candidate")
        result = self.files.show_memory_changes()
        change = next(item for item in result["changes"] if item["path"] == "pending/candidate.md")
        self.assertIn("+candidate", change["diff"])
        self.assertFalse(change["diff_omitted"])

    def test_new_session_resets_stale_temporary_from_formal(self):
        self.files.create_file("projects/b.md", "temporary")
        self.assertTrue((self.files.workspace_root / "projects/b.md").exists())
        reopened = self.open_files()
        self.assertIn("error", reopened.execute("read_file", {"path": "projects/b.md"}))
        self.assertFalse((reopened.workspace_root / "projects/b.md").exists())
        self.assertFalse(self.state()["active"])
        self.assert_synchronized()

    def test_runtime_error_does_not_discard_active_transaction(self):
        self.files.create_file("projects/b.md", "temporary")
        with self.assertRaises(RuntimeError):
            raise RuntimeError("simulated interruption")
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.files.read_memory("projects/b.md")["content"], "temporary")

    def test_run_turn_no_and_same_session_revision(self):
        from agent.main import run_turn

        self.answer = "no"
        messages = []
        run_turn(ScriptClient([[create("projects/b.md", "first")], [("commit_memory_changes", {})]]),
                 self.files, messages, "remember", emit=lambda _: None)
        self.assertFalse((self.root / "projects/b.md").exists())
        self.answer = "yes"
        run_turn(ScriptClient([[("edit_memory", dict(path="projects/b.md", old_text="first", new_text="revised"))],
                               [("commit_memory_changes", {})]]),
                 self.files, messages, "revise", emit=lambda _: None)
        self.assertEqual((self.root / "projects/b.md").read_text(encoding="utf-8"), "revised")
        self.assert_synchronized()


if __name__ == "__main__":
    unittest.main()
