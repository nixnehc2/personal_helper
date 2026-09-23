"""Memory transaction backed by a complete Temporary working copy."""
from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

PROTECTED_MEMORY_PATHS = ("self",)
WRITABLE_FOLDERS = {"self", "entities", "projects", "areas", "knowledge", "history", "inbox", "archive", "pending"}
TEMPORARY_NAME = ".memory-temporary"
STATE_NAME = ".memory-transaction.json"
LOCK_NAME = ".memory-transaction.lock"


@dataclass
class MemoryChange:
    target_path: str
    action: str
    content: str = ""
    old_text: str = ""
    reason: str = ""
    sources: list[str] | None = None


@dataclass
class TransactionChange:
    path: str
    before: str | None
    after: str | None

    @property
    def action(self):
        return "create" if self.before is None else "delete" if self.after is None else "edit"

    @property
    def diff(self):
        lines = difflib.unified_diff((self.before or "").splitlines(True), (self.after or "").splitlines(True),
                                     fromfile=self.path + " (Formal)", tofile=self.path + " (Temporary)")
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


class MemoryPolicy:
    def __init__(self, files, confirm_batch, confirm_transaction):
        self.files = files
        self.confirm_batch = confirm_batch
        self.confirm_transaction = confirm_transaction
        self.session = uuid.uuid4().hex
        self.snapshot = None
        with self._lock():
            self._sync_tree(self.files.root, self.files.workspace_root, preserve_runtime=False)
            self._save(False)

    def canonical(self, path):
        target = self.files.workspace_path(path)
        value = target.resolve().relative_to(self.files.workspace_root).as_posix()
        if os.name != "nt":
            return value
        components = value.split("/")
        current = self.files.workspace_root
        for index, part in enumerate(components):
            if not current.is_dir():
                break
            matches = [entry.name for entry in os.scandir(current) if entry.name.casefold() == part.casefold()]
            if matches:
                components[index] = matches[0]
            current = current / components[index]
        return "/".join(components)

    def _target(self, path):
        target = self.files.workspace_path(path)
        parts = path.split("/")
        if len(parts) < 2 or parts[0].casefold() not in WRITABLE_FOLDERS:
            raise ValueError("target is not a writable memory folder")
        if path.casefold().startswith("inbox/email/") or target.suffix.lower() not in (".md", ".txt"):
            raise ValueError("raw email archive is immutable; use ingestion")
        return target

    @contextmanager
    def _lock(self):
        path = self.files.formal_path(LOCK_NAME, internal=True)
        with path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _state_bytes(self):
        path = self.files.formal_path(STATE_NAME, internal=True)
        return path.read_bytes() if path.exists() else None

    def _save(self, active, baseline=None):
        path = self.files.formal_path(STATE_NAME, internal=True)
        raw = json.dumps(dict(version=2, active=active, session=self.session,
                              baseline=baseline or {}), ensure_ascii=False).encode("utf-8")
        self._atomic_write(path, raw)
        self.snapshot = raw

    def _state(self):
        raw = self._state_bytes()
        if raw is None:
            return None
        data = json.loads(raw)
        if data.get("version") != 2 or not isinstance(data.get("session"), str):
            raise ValueError("invalid memory transaction state")
        return data

    def _check_snapshot(self):
        if self._state_bytes() != self.snapshot:
            raise ValueError("transaction changed in another session; reopen this memory root")

    def _active(self):
        state = self._state()
        return bool(state and state["active"] and state["session"] == self.session)

    @staticmethod
    def _atomic_write(target, content):
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".memory-tmp-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _safe_unlink(path):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    @staticmethod
    def _copy_file(source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".memory-copy-", dir=target.parent)
        try:
            with open(source, "rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _same_file(left, right):
        if not left.is_file() or not right.is_file():
            return False
        left_stat, right_stat = left.stat(), right.stat()
        if left_stat.st_size != right_stat.st_size:
            return False
        with left.open("rb") as first, right.open("rb") as second:
            while True:
                first_block, second_block = first.read(1024 * 1024), second.read(1024 * 1024)
                if first_block != second_block:
                    return False
                if not first_block:
                    return True

    def _sync_tree(self, source, target, preserve_runtime):
        target.mkdir(parents=True, exist_ok=True)
        source_entries = {entry.name.casefold(): entry.name for entry in os.scandir(source)}
        for entry in os.scandir(target):
            if preserve_runtime and target == self.files.root and entry.name.startswith(".memory-"):
                continue
            if entry.name.casefold() not in source_entries:
                self._safe_unlink(Path(target / entry.name))
        for folded, name in source_entries.items():
            source_item, target_item = Path(source / name), Path(target / name)
            if source == self.files.root and target == self.files.workspace_root and name.startswith(".memory-"):
                continue
            if preserve_runtime and target == self.files.root and name.startswith(".memory-"):
                continue
            if source_item.is_symlink() or source_item.is_junction():
                raise ValueError("links and junctions are not allowed")
            if target_item.exists() or target_item.is_symlink():
                if target_item.is_symlink() or target_item.is_junction():
                    raise ValueError("links and junctions are not allowed")
                if source_item.is_dir() != target_item.is_dir():
                    self._safe_unlink(target_item)
            if source_item.is_dir():
                target_item.mkdir(exist_ok=True)
                self._sync_tree(source_item, target_item, False)
            else:
                if source_item.stat().st_nlink > 1:
                    raise ValueError("hard links are not allowed")
                self._copy_file(source_item, target_item)

    def _tree_files(self, root):
        files = {}
        for directory, directory_names, names in os.walk(root):
            if Path(directory) == self.files.root:
                directory_names[:] = [name for name in directory_names if not name.startswith(".memory-")]
                names = [name for name in names if not name.startswith(".memory-")]
            for name in names:
                path = Path(directory) / name
                if path.is_symlink() or path.is_junction():
                    raise ValueError("links and junctions are not allowed")
                files[path.relative_to(root).as_posix()] = path
        return files

    def _changes(self):
        formal, temporary = self._tree_files(self.files.root), self._tree_files(self.files.workspace_root)
        changes = {}
        for path in sorted(set(formal) | set(temporary)):
            before, after = formal.get(path), temporary.get(path)
            if before and after and self._same_file(before, after):
                continue
            before_text = self._display_text(before)
            after_text = self._display_text(after)
            changes[path] = TransactionChange(path, before_text, after_text)
        return changes

    @staticmethod
    def _display_text(path):
        if path is None:
            return None
        raw = path.read_bytes()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary file: {len(raw)} bytes, sha256 {hashlib.sha256(raw).hexdigest()}>"

    def current(self, path):
        self._check_snapshot()
        path = self.canonical(path)
        target = self.files.workspace_path(path)
        return self.files.text(target) if target.exists() else None

    @property
    def changes(self):
        self._check_snapshot()
        return self._changes()

    def _ensure_memory_transaction(self):
        self._check_snapshot()
        if self._active():
            return
        self._sync_tree(self.files.root, self.files.workspace_root, preserve_runtime=False)
        self._save(True, self._baseline())

    def _baseline(self):
        return {path: hashlib.sha256(path_item.read_bytes()).hexdigest()
                for path, path_item in self._tree_files(self.files.root).items()}

    def ensure_memory_transaction(self):
        with self._lock():
            self._ensure_memory_transaction()

    def ensure_no_transaction(self):
        with self._lock():
            self._check_snapshot()
            if not self._active():
                self._sync_tree(self.files.root, self.files.workspace_root, preserve_runtime=False)
                self._save(False)

    def apply_memory_changes(self, changes):
        results = []
        for change in changes:
            try:
                with self._lock():
                    self._ensure_memory_transaction()
                    path = self.canonical(change.target_path)
                    target = self._target(path)
                    before = self.current(path)
                    if change.action == "create":
                        if before is not None:
                            raise ValueError("file already exists")
                        after = change.content
                    elif change.action == "replace":
                        if before is None:
                            raise ValueError("file not found")
                        if not change.old_text:
                            raise ValueError("old_text must not be empty")
                        count = sum(before.startswith(change.old_text, offset) for offset in range(len(before)))
                        if count != 1:
                            raise ValueError(f"expected one match, found {count}")
                        after = before.replace(change.old_text, change.content, 1)
                    elif change.action == "delete":
                        if before is None:
                            raise ValueError("file not found")
                        after = None
                    else:
                        raise ValueError("unsupported action")
                    if after == before:
                        raise ValueError("no change")
                    if after is not None and len(after.encode("utf-8")) > 100000:
                        raise ValueError("content too large")
                    if after is None:
                        target.unlink(missing_ok=True)
                    else:
                        self._atomic_write(target, after.encode("utf-8"))
                    formal = self.files.formal_path(path)
                    item = TransactionChange(path, self._display_text(formal) if formal.exists() else None, after)
                    results.append(dict(path=path, status="temporary", diff=item.diff))
            except (OSError, ValueError, TypeError) as error:
                results.append(dict(path=change.target_path, error=str(error)))
        return results

    def show(self):
        self.ensure_no_transaction()
        self._check_snapshot()
        changes = self._changes()
        state = self._state()
        baseline = state.get("baseline", {}) if state else {}
        formal = self._tree_files(self.files.root)
        result = []
        for path, change in changes.items():
            digest = hashlib.sha256(formal[path].read_bytes()).hexdigest() if path in formal else None
            raw_email_archive = path.casefold().startswith("inbox/email/")
            result.append(dict(path=path, action=change.action,
                               diff=None if raw_email_archive else change.diff,
                               diff_omitted=raw_email_archive,
                               conflict=baseline.get(path) != digest))
        return dict(status="temporary" if result else "no_changes", changes=result)

    def _validate(self, changes):
        if isinstance(changes, dict):
            changes = list(changes.values())
        formal = self._tree_files(self.files.root)
        temporary = self._tree_files(self.files.workspace_root)
        state = self._state()
        baseline = state.get("baseline", {}) if state else {}
        if set(formal) != set(baseline):
            raise ValueError("Formal Memory changed since transaction start; no write performed")
        for path, digest in baseline.items():
            if hashlib.sha256(formal[path].read_bytes()).hexdigest() != digest:
                raise ValueError(f"{path}: Formal Memory changed since transaction start")
        for change in changes:
            path = change.path
            target = self.files.workspace_path(path)
            raw_archive = path.casefold().startswith("inbox/email/") and target.suffix.lower() == ".eml"
            if path.split("/", 1)[0].casefold() in WRITABLE_FOLDERS and not raw_archive:
                self._target(path)
            if target.is_file() and target.stat().st_size > 100000 and target.suffix.lower() in (".md", ".txt"):
                raise ValueError(f"{path}: content too large")
            if path.casefold().endswith("/_index.md") and path in temporary:
                content = temporary[path].read_text(encoding="utf-8")
                for link in re.findall(r"\]\(([^)]+)\)", content):
                    if "://" in link or link.startswith("#"):
                        continue
                    linked = (target.parent / link.split("#")[0]).resolve()
                    if not linked.is_relative_to(self.files.workspace_root) or linked not in temporary.values():
                        raise ValueError(f"{path}: index references a missing file")

    def sync_formal_to_temporary(self):
        with self._lock():
            self._check_snapshot()
            self._sync_tree(self.files.root, self.files.workspace_root, preserve_runtime=False)
            self._save(False)

    def sync_temporary_to_formal(self):
        with self._lock():
            self._check_snapshot()
            self._sync_tree(self.files.workspace_root, self.files.root, preserve_runtime=True)
            self._save(False)

    def discard(self, explicit=False):
        with self._lock():
            self._check_snapshot()
            changes = self.changes
            if changes and not explicit and self.confirm_transaction("discard", list(changes.values())) != "yes":
                return dict(status="not_approved", temporary_retained=True)
            self._sync_tree(self.files.root, self.files.workspace_root, preserve_runtime=False)
            self._save(False)
            return dict(status="discarded" if changes else "no_changes")

    def request_commit(self):
        with self._lock():
            self._check_snapshot()
            changes = self._changes()
            if not changes:
                self._sync_tree(self.files.root, self.files.workspace_root, preserve_runtime=False)
                self._save(False)
                return dict(status="no_changes")
            self._validate(changes)
            if self.confirm_transaction("commit", list(changes.values())) != "yes":
                return dict(status="not_approved", temporary_retained=True)
            protected = [change for change in changes.values() if change.path.split("/")[0].casefold() in PROTECTED_MEMORY_PATHS]
            if protected:
                accepted = self.confirm_batch(protected)
                if not isinstance(accepted, dict) or set(accepted) - set(range(len(protected))):
                    raise ValueError("invalid self review response")
                for text in accepted.values():
                    if text is not None and (not isinstance(text, str) or len(text.encode("utf-8")) > 100000):
                        raise ValueError("invalid self review content")
                if any(text != protected[index].after for index, text in accepted.items()):
                    for index, text in accepted.items():
                        if text is not None:
                            self._atomic_write(self.files.workspace_path(protected[index].path), text.encode("utf-8"))
                    return dict(status="self_review_edited", temporary_retained=True,
                                note="Request commit again to approve the revised diff")
                if set(accepted) != set(range(len(protected))):
                    return dict(status="self_review_not_approved", temporary_retained=True)
            changes = self._changes()
            self._validate(changes)
            self._sync_tree(self.files.workspace_root, self.files.root, preserve_runtime=True)
            self._save(False)
            self.files.writes.extend(change.path for change in changes.values())
            return dict(status="committed", paths=[change.path for change in changes.values()])

    def archive_email(self, raw):
        digest = hashlib.sha256(raw).hexdigest()
        path = f"inbox/email/{digest}.eml"
        formal = self.files.formal_path(path)
        with self._lock():
            self._ensure_memory_transaction()
            target = self.files.workspace_path(path)
            if formal.exists() or target.exists():
                if (formal.exists() and formal.read_bytes() != raw) or (target.exists() and target.read_bytes() != raw):
                    raise ValueError("raw archive hash collision or corruption")
                return dict(path=path, duplicate=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(raw)
            self.files.writes.append(path)
            return dict(path=path, duplicate=False)


def apply_memory_changes(policy, changes):
    return policy.apply_memory_changes(changes)
