"""Create new exports in one fixed directory; no overwrites or Memory writes."""
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger(__name__)
MAX_CHARS = 80_000
MARKDOWN = ("markdown-raw_tex-raw_html-raw_attribute-yaml_metadata_block-pandoc_title_block"
            "-tex_math_dollars-tex_math_single_backslash-tex_math_double_backslash")
# XeCJK sub-blocks preserve the original symbol codepoints, including in code
# blocks, while keeping Chinese in YaHei. This is trusted configuration only;
# user-supplied TeX and metadata remain disabled.
PDF_SYMBOLS = re.compile("[\u2600-\u27bf\U0001f300-\U0001f6ff]")
PDF_SYMBOL_HEADER = r'''\xeCJKDeclareSubCJKBlock{ExportSymbols}{"2600 -> "27BF, "1F300 -> "1F6FF}
\setCJKmainfont[ExportSymbols={Segoe UI Symbol}]{Microsoft YaHei}
\setCJKsansfont[ExportSymbols={Segoe UI Symbol}]{Microsoft YaHei}
\setCJKmonofont[ExportSymbols={Segoe UI Symbol}]{Microsoft YaHei}'''


def pdf_symbol_text(content):
    # The PDF uses monochrome glyphs. Drop only emoji/text presentation selectors
    # immediately after a supported symbol; never drop the symbol or arbitrary text.
    return re.sub(f"({PDF_SYMBOLS.pattern})[\ufe0e\ufe0f]", r"\1", content)


class FileWriteError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class PandocBackend:
    def _run(self, command, cwd, content):
        try:
            result = subprocess.run(command, input=content, text=True, encoding="utf-8",
                                    errors="replace", capture_output=True, cwd=cwd, timeout=90,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            raise FileWriteError("conversion_timeout", "文档转换超时，请缩短内容后重试") from error
        # Diagnostics may include source excerpts; keep them out of Agent results.
        if result.stderr:
            LOG.warning("Pandoc diagnostics: %s", result.stderr[-6000:])
        if result.returncode:
            raise FileWriteError("conversion_failed", "Pandoc 转换失败；PDF 请检查 XeLaTeX、xeCJK、Microsoft YaHei 及符号字体 Segoe UI Symbol，详见日志")
        if "Missing character:" in result.stderr:
            codes = list(dict.fromkeys(re.findall(r"\(U\+([0-9A-Fa-f]{4,6})\)", result.stderr)))[:12]
            details = "、".join(f"{chr(int(code, 16))!r} (U+{code.upper()})"
                               for code in codes if int(code, 16) <= 0x10FFFF)
            raise FileWriteError("missing_glyph", "PDF 字体缺少字符：" + (details or "详见日志")
                                 + "。未保存文件；这不一定是中文字体故障。请针对所列字符调整字体或表示方式后重试 PDF，不要擅自改为其他格式。")
        return result.stdout

    def convert(self, content, output):
        pandoc = shutil.which("pandoc")
        if not pandoc:
            raise FileWriteError("pandoc_missing", "未安装 Pandoc 或不在 PATH 中，请安装后重启 Agent")
        engine = None
        if output.suffix == ".pdf":
            engine = shutil.which("xelatex")
            if not engine:
                raise FileWriteError("pdf_backend_missing", "PDF backend 缺失：需要 XeLaTeX、xeCJK 和 Microsoft YaHei 中文字体")
            content = pdf_symbol_text(content)
        # Parse with Pandoc itself. No custom Markdown, DOCX or PDF parser.
        source = output.parent / "source.md"
        source.write_text(content, encoding="utf-8")
        document = json.loads(self._run([pandoc, "--sandbox", "--from", MARKDOWN,
                                         "--to=json", str(source)], output.parent, None))
        document["meta"] = {}
        todo = [document]
        while todo:
            node = todo.pop()
            if isinstance(node, dict):
                if node.get("t") in ("Image", "RawBlock", "RawInline"):
                    raise FileWriteError("unsupported_content", "V1 不支持图片或原始排版指令，请仅提供 Markdown 正文")
                todo.extend(node.values())
            elif isinstance(node, list):
                todo.extend(node)
        command = [pandoc, "--sandbox", "--from=json", "--standalone", "--output", str(output)]
        if engine:
            command += ["--pdf-engine", engine, "--pdf-engine-opt=-no-shell-escape",
                        "--variable=mainfont:Microsoft YaHei", "--variable=CJKmainfont:Microsoft YaHei",
                        "--variable=monofont:Microsoft YaHei", "--variable=CJKmonofont:Microsoft YaHei",
                        "--variable=pagestyle:empty"]
            if PDF_SYMBOLS.search(content):
                command.append("--variable=header-includes:" + PDF_SYMBOL_HEADER)
        self._run(command, output.parent, json.dumps(document, ensure_ascii=False))
        if not output.is_file() or output.stat().st_size == 0:
            raise FileWriteError("empty_output", "转换未生成有效文件")


class FileWriter:
    def __init__(self):
        # Never derived from an Agent argument, cwd or Memory root.
        self.output_dir = PROJECT / "generated_files"
        self.pandoc = PandocBackend()

    def _filename(self, filename):
        if (not isinstance(filename, str) or not filename or len(filename) > 180
                or filename != filename.strip() or filename.endswith(".")
                or any(ord(c) < 32 or c in '/\\:<>"|?*' for c in filename)
                or PureWindowsPath(filename).is_reserved() or filename.startswith(".")):
            raise FileWriteError("invalid_filename", "文件名无效：只允许普通文件名，不能包含目录、绝对路径或保留名称")
        extension = Path(filename).suffix.lower()
        if extension not in (".txt", ".md", ".pdf", ".docx"):
            raise FileWriteError("unsupported_type", "不支持的文件类型；仅支持 txt、md、pdf、docx")
        return extension

    def _directory(self):
        # Check before mkdir and again before publishing; never follow a redirected output directory.
        for item in (self.output_dir, *self.output_dir.parents):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise FileWriteError("unsafe_directory", "generated_files 路径不能包含链接或 junction")
        self.output_dir.mkdir(exist_ok=True)
        if not self.output_dir.is_dir():
            raise FileWriteError("output_directory_failed", "generated_files 不是目录")

    def create_file(self, filename, content):
        try:
            extension = self._filename(filename)
            if not isinstance(content, str) or not content.strip():
                raise FileWriteError("empty_content", "正文不能为空")
            if len(content) > MAX_CHARS:
                raise FileWriteError("content_too_large", "正文超过 V1 的 80000 字符限制")
            self._directory()
            destination = self.output_dir / filename
            if os.path.lexists(destination):
                raise FileWriteError("file_exists", "文件已存在，不允许覆盖")
            with tempfile.TemporaryDirectory(prefix=".file-writer-", dir=self.output_dir) as temp:
                staged = Path(temp) / ("output" + extension)
                if extension in (".txt", ".md"):
                    staged.write_text(content, encoding="utf-8", newline="")
                else:
                    self.pandoc.convert(content, staged)
                if not staged.is_file() or not staged.stat().st_size:
                    raise FileWriteError("empty_output", "未生成有效文件")
                self._directory()
                # Same-volume atomic publication. link fails if ANY entry exists,
                # including a concurrent writer or dangling symlink. Never replace.
                os.link(staged, destination)
            return dict(success=True, filename=filename, path=str(destination))
        except FileWriteError as error:
            LOG.warning("create_file %s: %s", error.code, error)
            return dict(success=False, error=str(error), code=error.code)
        except FileExistsError:
            return dict(success=False, error="文件已存在，或 generated_files 被同名文件占用", code="file_exists")
        except PermissionError:
            LOG.exception("create_file permission denied")
            return dict(success=False, error="无法创建 generated_files 或写入文件：权限不足", code="access_denied")
        except OSError:
            LOG.exception("create_file filesystem failure")
            return dict(success=False, error="无法创建生成目录或写入文件，请检查磁盘和目录权限", code="filesystem_failed")
        except Exception:
            LOG.exception("create_file failed")
            return dict(success=False, error="文件生成失败，请检查日志", code="generation_failed")
