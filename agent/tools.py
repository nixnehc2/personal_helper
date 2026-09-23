"""Generic UTF-8 file tools; no knowledge-base taxonomy lives here."""
import difflib
import os
from pathlib import Path, PureWindowsPath
import stat

LIMIT = 100_000


def schema(name, description, properties, required):
    return dict(name=name, description=description, input_schema=dict(
        type="object", properties={k: {"type": v} for k, v in properties.items()},
        required=required, additionalProperties=False))


TOOLS = [
    schema("list_directory", "List immediate children, not recursively.", {"path": "string"}, ["path"]),
    schema("read_file", "Read UTF-8 text with optional pagination; lines are 1-based.",
           {"path": "string", "start_line": "integer", "max_lines": "integer"}, ["path"]),
    schema("search_files", "Literal case-insensitive text search in .md/.txt files; bounded results.",
           {"query": "string", "path": "string"}, ["query"]),
    schema("create_file", "Propose creating a new UTF-8 file. Human approval is mandatory; never overwrites.",
           {"path": "string", "content": "string"}, ["path", "content"]),
    schema("replace_text", "Propose one exact unique replacement. Human approval is mandatory.",
           {"path": "string", "old_text": "string", "new_text": "string"},
           ["path", "old_text", "new_text"]),
]


class FileTools:
    def __init__(self, root, confirm):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("root must be a directory")
        self.confirm = confirm
        self.writes = []
        self.denied = False

    def path(self, value):
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("invalid path")
        parts = PureWindowsPath(value)
        if parts.drive or parts.root or ".." in parts.parts or ":" in value:
            raise ValueError("path outside root or invalid path")
        candidate = self.root
        for part in value.replace("\\", "/").split("/"):
            if part in ("", "."):
                continue
            if PureWindowsPath(part).is_reserved() or part.endswith((" ", ".")):
                raise ValueError("reserved or ambiguous path component")
            candidate = candidate / part
            if candidate.is_symlink() or candidate.is_junction():
                raise ValueError("links and junctions are not allowed")
            if candidate.exists():
                info = candidate.stat()
                if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError("reparse points are not allowed")
                if candidate.is_file() and info.st_nlink > 1:
                    raise ValueError("hard links are not allowed")
        if not candidate.resolve().is_relative_to(self.root):
            raise ValueError("path outside root")
        return candidate

    def text(self, path):
        if not path.is_file():
            raise ValueError("file not found or not a regular file")
        with path.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("file exceeds 100000-byte V1 limit")
        return raw.decode("utf-8")

    def list_directory(self, path):
        directory = self.path(path)
        if not directory.is_dir():
            raise ValueError("directory not found")
        entries = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if len(entries) == 200:
                    return dict(entries=entries, truncated=True)
                entries.append(dict(name=entry.name, is_directory=entry.is_dir(follow_symlinks=False)))
        return dict(entries=entries, truncated=False)

    def read_file(self, path, start_line=1, max_lines=200):
        if type(start_line) is not int or type(max_lines) is not int or start_line < 1 or not 1 <= max_lines <= 500:
            raise ValueError("invalid pagination")
        lines = self.text(self.path(path)).splitlines(keepends=True)
        selected = "".join(lines[start_line - 1:start_line - 1 + max_lines])
        return dict(path=path, start_line=start_line, total_lines=len(lines),
                    content=selected[:20000], truncated=(start_line - 1 + max_lines < len(lines) or len(selected) > 20000))

    def search_files(self, query, path="."):
        if not query:
            raise ValueError("empty search query")
        base = self.path(path)
        if not base.exists():
            raise ValueError("path not found")
        pending, matches, skipped, scanned = [base], [], [], 0
        while pending:
            item = pending.pop()
            scanned += 1
            if scanned > 2000:
                return dict(matches=matches, skipped=skipped, truncated=True)
            try:
                item = self.path(item.relative_to(self.root).as_posix())
                if item.is_dir():
                    with os.scandir(item) as entries:
                        for entry in entries:
                            if len(pending) >= 2000:
                                return dict(matches=matches, skipped=skipped, truncated=True)
                            pending.append(Path(entry.path))
                    continue
                if item.suffix.lower() not in (".md", ".txt"):
                    continue
                lines = self.text(item).splitlines()
                for number, line in enumerate(lines, 1):
                    index = line.casefold().find(query.casefold())
                    if index >= 0:
                        matches.append(dict(path=item.relative_to(self.root).as_posix(), line=number,
                                            snippet=line[max(0, index - 80):index + 240]))
                        if len(matches) >= 50:
                            return dict(matches=matches, skipped=skipped, truncated=True)
            except (OSError, ValueError) as error:
                if len(skipped) < 20:
                    skipped.append(dict(path=item.relative_to(self.root).as_posix(), error=str(error)))
        return dict(matches=matches, skipped=skipped, truncated=False)

    def approve(self, path, before, after):
        if self.denied:
            raise ValueError("writes disabled for this turn after user declined; do not retry")
        changes = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                      fromfile=path + " (before)", tofile=path + " (after)")
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                       for line in changes)
        if not self.confirm(path, diff):
            self.denied = True
            raise ValueError("user declined; no changes made")
        return diff

    def create_file(self, path, content):
        target = self.path(path)
        if target.exists():
            raise ValueError("file already exists")
        if len(content.encode("utf-8")) > LIMIT:
            raise ValueError("content too large")
        diff = self.approve(path, "", content)
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="") as stream:
            stream.write(content)
        self.writes.append(path)
        return dict(success=True, path=path, diff=diff)

    def replace_text(self, path, old_text, new_text):
        target = self.path(path)
        before = self.text(target)
        if not old_text:
            raise ValueError("old_text must not be empty")
        count = sum(before.startswith(old_text, n) for n in range(len(before)))
        if count != 1:
            raise ValueError(f"expected one match, found {count}")
        after = before.replace(old_text, new_text, 1)
        if after == before:
            raise ValueError("no change")
        if len(after.encode("utf-8")) > LIMIT:
            raise ValueError("content too large")
        diff = self.approve(path, before, after)
        target = self.path(path)
        if self.text(target) != before:
            raise ValueError("file changed during approval; read and propose again")
        with target.open("r+", encoding="utf-8", newline="") as stream:
            stream.write(after)
            stream.truncate()
        self.writes.append(path)
        return dict(success=True, path=path, diff=diff)

    def execute(self, name, arguments):
        try:
            spec = next((x for x in TOOLS if x["name"] == name), None)
            if spec is None or not isinstance(arguments, dict):
                raise ValueError("invalid tool call")
            props = spec["input_schema"]["properties"]
            if set(arguments) - set(props) or set(spec["input_schema"]["required"]) - set(arguments):
                raise ValueError("invalid tool arguments")
            for key, value in arguments.items():
                expected = str if props[key]["type"] == "string" else int
                if type(value) is not expected:
                    raise ValueError("invalid argument type")
            return getattr(self, name)(**arguments)
        except (OSError, ValueError, TypeError) as error:
            return dict(error=str(error))
