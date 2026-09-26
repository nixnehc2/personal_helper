"""Real MCP/MarkItDown acceptance fixtures plus boundary and turn-loop tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agent.file_reader import FileReader, FilesystemMCP, MAX_CHARS
from agent.main import run_turn
from agent.tools import FileTools, TOOLS

FIXTURES = Path(__file__).parent.resolve() / "fixtures/file_reader"


class FileReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader = FileReader([FIXTURES])

    def test_all_four_real_backends(self):
        contents = []
        for extension in ("txt", "md", "pdf", "docx"):
            with self.subTest(extension=extension):
                path = FIXTURES / f"example.{extension}"
                result = self.reader.read_file(str(path))
                self.assertNotIn("error", result, result)
                self.assertEqual(result["file_type"], extension)
                self.assertEqual(result["path"], str(path))
                self.assertIn("Project: File Reader V1", result["content"])
                self.assertIn("Key number: 314159", result["content"])
                contents.append(" ".join(result["content"].split()))
        self.assertEqual(len(set(contents)), 1)

    def test_invalid_missing_and_outside_paths(self):
        for path, code in [("example.txt", "invalid_path"), ("https://example.com/x.pdf", "invalid_path"),
                           ("\x00", "invalid_path"), (str(FIXTURES / "missing.md"), "not_found"),
                           (str(FIXTURES.parent / "private.txt"), "access_denied"),
                           (str(FIXTURES), "invalid_path")]:
            with self.subTest(path=path):
                self.assertEqual(self.reader.read_file(path)["code"], code)

    def test_unsupported_empty_and_corrupt_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = FileReader([tmp])
            for name, data, code in [("file.xlsx", b"unsupported", "unsupported_type"),
                                     ("empty.txt", b"", "empty_file"),
                                     ("broken.pdf", b"%PDF-1.7\nbroken", "conversion_failed"),
                                     ("broken.docx", b"PK\x03\x04broken", "conversion_failed")]:
                with self.subTest(name=name):
                    p = Path(tmp) / name
                    p.write_bytes(data)
                    self.assertEqual(reader.read_file(str(p))["code"], code)

    def test_permission_denied(self):
        for method in ("lstat", "open"):
            with patch.object(Path, method, side_effect=PermissionError("denied")):
                self.assertEqual(self.reader.read_file(str(FIXTURES / "example.pdf"))["code"], "access_denied")

    def test_mcp_server_enforces_its_own_roots(self):
        # Exercise the server's boundary without the FileReader preflight.
        from agent.file_reader import FileReadError
        with self.assertRaises(FileReadError):
            self.reader.filesystem.read_text(FIXTURES.parent.parent / "test_file_reader.py")

    def test_mcp_failure_is_tool_error(self):
        with patch.object(FilesystemMCP, "_read", new=AsyncMock(side_effect=RuntimeError("transport closed"))):
            self.assertEqual(self.reader.read_file(str(FIXTURES / "example.txt"))["code"], "filesystem_mcp_failed")

    def test_no_silent_truncation_and_no_extractable_text(self):
        for content, code in [(" " * 3, "empty_content"), ("x" * (MAX_CHARS + 1), "content_too_large")]:
            with patch.object(self.reader, "_convert", return_value=content):
                self.assertEqual(self.reader.read_file(str(FIXTURES / "example.pdf"))["code"], code)

    def test_routes_and_no_extra_mcp_tools(self):
        backend = Mock()
        backend.read_text.return_value = "text"
        reader = FileReader([FIXTURES], filesystem=backend)
        with patch.object(reader, "_convert", return_value="markdown") as converter:
            for ext in ("txt", "md", "pdf", "docx"):
                reader.read_file(str(FIXTURES / f"example.{ext}"))
            self.assertEqual(backend.read_text.call_count, 2)
            self.assertEqual(converter.call_count, 2)
        spec = next(s for s in TOOLS if s["name"] == "read_file")
        self.assertEqual(set(spec["input_schema"]["properties"]), {"path"})
        self.assertFalse({"read_text_file", "read_pdf", "read_docx", "markitdown_convert", "filesystem_read"}
                         & {s["name"] for s in TOOLS})

    def test_configuration_and_memory_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = FileTools(tmp)
            p = Path(tmp) / "secret.md"
            p.write_text("private", encoding="utf-8")
            files.file_reader = FileReader([tmp], denied_roots=[tmp])
            self.assertEqual(files.execute("read_file", {"path": str(p)})["code"], "access_denied")
            files.read_only = True
            self.assertEqual(files.execute("read_file", {"path": str(FIXTURES / "example.pdf")})["code"], "access_denied")
        with patch("agent.llm.load_config", return_value={"FILE_READER_ALLOWED_ROOTS": json.dumps([str(FIXTURES)])}):
            self.assertEqual(FileReader().roots, [FIXTURES])

    def test_hardlink_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "source.txt"
            p.write_text("private")
            link = Path(tmp) / "link.txt"
            link.hardlink_to(p)
            self.assertEqual(FileReader([tmp]).read_file(str(link))["code"], "access_denied")

    def test_two_sequential_reads_return_to_same_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "AGENT.md").write_text("Use evidence.", encoding="utf-8")
            files = FileTools(tmp)
            files.file_reader = self.reader
            messages = []
            test = self

            class Client:
                calls = 0

                def complete(self, system, history, specs):
                    self.calls += 1
                    if self.calls > 1:
                        result = history[-1]["content"][0]
                        test.assertFalse(result["is_error"])
                        test.assertIn("314159", json.loads(result["content"])["content"])
                    if self.calls <= 2:
                        ext = "pdf" if self.calls == 1 else "docx"
                        return dict(content=[dict(type="tool_use", id=str(self.calls), name="read_file",
                                                  input={"path": str(FIXTURES / f"example.{ext}")})], stop_reason="tool_use")
                    return dict(content=[dict(type="text", text="两份文件均为 File Reader V1，Key number 都是 314159。")], stop_reason="end_turn")

            client = Client()
            run_turn(client, files, messages, "比较 example.pdf 和 example.docx 的内容", emit=lambda _: None)
            self.assertEqual(client.calls, 3)
            self.assertFalse(files.policy.changes)

    def test_tool_error_does_not_abort_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "AGENT.md").write_text("Use evidence.", encoding="utf-8")
            files = FileTools(tmp)
            files.file_reader = self.reader
            responses = [dict(content=[dict(type="tool_use", id="bad", name="read_file",
                                           input={"path": str(FIXTURES / "missing.txt")})], stop_reason="tool_use"),
                         dict(content=[dict(type="text", text="文件不存在")], stop_reason="end_turn")]
            client = Mock()
            client.complete.side_effect = responses
            messages = []
            run_turn(client, files, messages, "读取 missing.txt", emit=lambda _: None)
            self.assertTrue(messages[2]["content"][0]["is_error"])


if __name__ == "__main__":
    unittest.main()
