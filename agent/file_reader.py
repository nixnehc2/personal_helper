"""Read explicit local paths through a deliberately small backend interface."""
import asyncio
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat

LOG = logging.getLogger(__name__)
PROJECT = Path(__file__).resolve().parent.parent
MAX_BYTES = 20 * 1024 * 1024
MAX_CHARS = 80_000


class FileReadError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FilesystemMCP:
    """Private stdio client. Never exports the server's tool catalog to the Agent."""
    def __init__(self, roots):
        self.roots = roots

    async def _read(self, path):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server = PROJECT / "node_modules/@modelcontextprotocol/server-filesystem/dist/index.js"
        node = shutil.which("node")
        if not node or not server.is_file():
            raise RuntimeError("请安装 Node.js 并在项目目录运行 npm ci")
        params = StdioServerParameters(command=node, args=[str(server), *map(str, self.roots)])
        async with asyncio.timeout(30):
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    result = await session.call_tool("read_text_file", {"path": str(path)})
                    if result.isError:
                        raise RuntimeError("; ".join(b.text for b in result.content if b.type == "text"))
                    return "\n".join(b.text for b in result.content if b.type == "text")

    def read_text(self, path):
        try:
            return asyncio.run(self._read(path))
        except Exception as error:
            LOG.exception("Filesystem MCP failed for %s", path)
            raise FileReadError("filesystem_mcp_failed", "Filesystem MCP 读取失败；检查依赖、文件权限及日志") from error


class FileReader:
    def __init__(self, allowed_roots=None, *, denied_roots=(), filesystem=None):
        if allowed_roots is None:
            from .llm import load_config
            raw = load_config().get("FILE_READER_ALLOWED_ROOTS", os.environ.get("FILE_READER_ALLOWED_ROOTS"))
            try:
                allowed_roots = json.loads(raw) if raw else [str(Path.home())]
            except (ValueError, TypeError) as error:
                raise FileReadError("invalid_configuration", "FILE_READER_ALLOWED_ROOTS 必须是绝对目录路径的 JSON 数组") from error
        if not isinstance(allowed_roots, (list, tuple)) or not allowed_roots:
            raise FileReadError("invalid_configuration", "至少需要一个允许读取的目录")
        self.roots = []
        for root in allowed_roots:
            if not isinstance(root, (str, Path)) or not Path(root).is_absolute() or not Path(root).is_dir():
                raise FileReadError("invalid_configuration", "允许读取的目录必须是存在的绝对目录路径")
            self.roots.append(Path(root).resolve(strict=True))
        self.denied_roots = [Path(p).resolve() for p in denied_roots]
        self.filesystem = filesystem or FilesystemMCP(self.roots)

    def _path(self, value):
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise FileReadError("invalid_path", "路径无效：请提供明确的本地绝对文件路径")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or value.startswith(("\\\\", "//")):
            raise FileReadError("invalid_path", "只接受本地绝对文件路径，不接受 URL、网络路径或 ..")
        if os.name == "nt" and any(":" in p or PureWindowsPath(p).is_reserved() or p.endswith((" ", ".")) for p in path.parts[1:]):
            raise FileReadError("invalid_path", "路径包含保留名称或无效组件")
        resolved = path.resolve()
        if not any(resolved.is_relative_to(r) for r in self.roots) or any(resolved.is_relative_to(r) for r in self.denied_roots):
            raise FileReadError("access_denied", "访问被拒绝：路径不在允许的文件范围内；Memory 请使用 read_memory")
        if any(p.casefold().startswith(".memory-") for p in path.parts):
            raise FileReadError("access_denied", "不允许读取运行时私有文件")
        # Reject links, junctions and other reparse points in every component.
        for component in (path, *path.parents):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise FileReadError("access_denied", "不允许通过链接或 junction 读取文件")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise FileReadError("invalid_path", "路径必须指向普通文件")
        if info.st_nlink > 1:
            raise FileReadError("access_denied", "不允许读取硬链接文件")
        if path.suffix.lower() not in (".txt", ".md", ".pdf", ".docx"):
            raise FileReadError("unsupported_type", "不支持的文件类型；仅支持 txt、md、pdf、docx")
        if info.st_size > MAX_BYTES:
            raise FileReadError("file_too_large", "文件超过 V1 的 20 MiB 限制")
        if info.st_size == 0:
            raise FileReadError("empty_file", "文件为空")
        return resolved

    def _convert(self, path):
        try:
            from markitdown import MarkItDown
            from markitdown.converters import DocxConverter, PdfConverter
            # Use the stock format converter, without generic text/ZIP fallback:
            # otherwise malformed documents can be reported as successful text.
            converter = MarkItDown(enable_builtins=False, enable_plugins=False)
            converter.register_converter(PdfConverter() if path.suffix.lower() == ".pdf" else DocxConverter())
            return converter.convert(str(path)).text_content
        except Exception as error:
            LOG.exception("MarkItDown failed for %s", path)
            raise FileReadError("conversion_failed", f"{path.suffix[1:].upper()} 转换失败：文件可能损坏、加密或缺少 MarkItDown 依赖") from error

    def read_file(self, path):
        try:
            target = self._path(path)
            # stat can succeed even when the file's read ACL denies access.
            # Check that separately so all backends report the same permission error.
            with target.open("rb"):
                pass
            content = (self.filesystem.read_text(target) if target.suffix.lower() in (".txt", ".md")
                       else self._convert(target))
            if not isinstance(content, str):
                raise FileReadError("invalid_content", "读取后端未返回文本")
            if not content.strip():
                raise FileReadError("empty_content", "文件没有可提取的文字；V1 不支持 OCR 或扫描件识别")
            if len(content) > MAX_CHARS:
                raise FileReadError("content_too_large", "正文超过 V1 的 80000 字符限制，请提供较小文件")
            return dict(path=str(target), file_type=target.suffix[1:].lower(), content=content)
        except FileReadError as error:
            LOG.warning("read_file %s: %s (%s)", error.code, path, error)
            return dict(error=str(error), code=error.code)
        except FileNotFoundError:
            return dict(error="文件不存在", code="not_found")
        except PermissionError:
            LOG.exception("Permission denied for %s", path)
            return dict(error="文件访问权限不足", code="access_denied")
        except (OSError, ValueError) as error:
            LOG.exception("Invalid file path %s", path)
            return dict(error=f"路径无效或无法访问：{error}", code="invalid_path")
        except Exception:
            LOG.exception("Unexpected read_file failure for %s", path)
            return dict(error="文件读取失败，请检查日志", code="read_failed")
