"""Local metadata index. No EML download, model call, or Memory writes."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesHeaderParser
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import time

from .llm import load_config

INDEX_PATH = Path(__file__).resolve().parent.parent / "data/email/index.json"


class EmailIndex:
    def __init__(self, path=INDEX_PATH):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return {"version": 1, "next_id": 1, "emails": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            assert data["version"] == 1
            ids = set()
            for row in data["emails"]:
                assert type(row["id"]) is int and row["id"] > 0 and row["id"] not in ids
                ids.add(row["id"])
                assert type(row["imported"]) is bool
                assert row["imported_at"] is None or isinstance(row["imported_at"], str)
                for key in ("imap_uid", "message_id", "subject", "from", "date", "account", "host", "folder", "uidvalidity"):
                    assert isinstance(row[key], str)
            assert type(data["next_id"]) is int and data["next_id"] > max(ids, default=0)
            return data
        except (ValueError, KeyError, TypeError, AssertionError):
            raise ValueError("邮件索引格式损坏；已保留原文件，请修复后重试") from None

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError("邮件索引正在同步；若进程异常退出，请确认没有同步进程后删除 index.lock") from None
        try:
            os.close(fd)
            yield
        finally:
            lock.unlink(missing_ok=True)

    def write(self, data):
        """Atomic replacement; callers must hold locked()."""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".index-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def get(self, id):
        if type(id) is not int or id <= 0:
            raise ValueError("邮件 ID 必须是正整数")
        row = next((r for r in self.read()["emails"] if r["id"] == id), None)
        if row is None:
            raise ValueError(f"邮件 ID {id} 不存在，请先 update_email")
        return row

    def mark_imported(self, expected):
        with self.locked():
            data = self.read()
            row = next((r for r in data["emails"] if r["id"] == expected["id"]), None)
            keys = ("host", "account", "folder", "uidvalidity", "imap_uid", "message_id")
            if row is None or any(row[k] != expected[k] for k in keys):
                raise ValueError("导入期间邮件索引发生变化，未标记已导入，请重新同步后重试")
            if not row["imported"]:
                row.update(imported=True, imported_at=datetime.now(timezone.utc).isoformat())
                self.write(data)
            return row

    @staticmethod
    def merge_rows(data, source, metadata):
        """Linear index construction plus constant-time identity lookups."""
        scoped = [r for r in data["emails"] if all(r[k] == source[k] for k in ("host", "account", "folder"))]
        by_uid, by_message = {}, {}
        for row in scoped:
            by_uid.setdefault((row["uidvalidity"], row["imap_uid"]), row)
            if row["message_id"]:
                by_message.setdefault(row["message_id"], row)
        added = 0
        for item in metadata:
            existing = by_uid.get((source["uidvalidity"], item["imap_uid"]))
            if existing is None and item["message_id"]:
                existing = by_message.get(item["message_id"])
            if existing is None:
                existing = dict(source, **item, id=data["next_id"], imported=False, imported_at=None)
                data["emails"].append(existing)
                data["next_id"] += 1
                added += 1
            else:
                old_uid = (existing["uidvalidity"], existing["imap_uid"])
                old_message = existing["message_id"]
                if by_uid.get(old_uid) is existing:
                    del by_uid[old_uid]
                if by_message.get(old_message) is existing:
                    del by_message[old_message]
                existing.update(source)
                existing.update(item)
            by_uid[(existing["uidvalidity"], existing["imap_uid"])] = existing
            if existing["message_id"]:
                by_message.setdefault(existing["message_id"], existing)
        return added

    def merge(self, source, metadata):
        with self.locked():
            data = self.read()
            added = self.merge_rows(data, source, metadata)
            self.write(data)
            return data["emails"], added


def scope_key(settings):
    return json.dumps([settings.get("EMAIL_IMAP_HOST", "imap.qq.com").lower(),
                       settings.get("EMAIL_ACCOUNT", "").lower(), settings.get("EMAIL_FOLDER", "INBOX")])


def trusted_progress(value):
    if not isinstance(value, dict):
        return None
    if (not isinstance(value.get("uidvalidity"), str) or not value["uidvalidity"].isdigit()
            or type(value.get("max_uid")) is not int or not 0 <= value["max_uid"] <= 4294967295):
        return None
    try:
        completed = datetime.fromisoformat(value["completed_at"])
        if completed.tzinfo is None:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return value


def fetch_metadata(settings, progress=None):
    account = settings.get("EMAIL_ACCOUNT", "")
    password = settings.get("EMAIL_AUTH_CODE", "")
    if not account or not password:
        raise ValueError("请在 config.local.json 配置 EMAIL_ACCOUNT 和 EMAIL_AUTH_CODE")
    host = settings.get("EMAIL_IMAP_HOST", "imap.qq.com")
    folder = settings.get("EMAIL_FOLDER", "INBOX")
    connection = None
    stage = "连接"
    try:
        port = int(settings.get("EMAIL_IMAP_PORT", "993"))
        connection = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
        stage = "登录（请检查邮箱授权码和 IMAP 服务是否开启）"
        status, _ = connection.login(account, password)
        if status != "OK":
            raise ValueError("login failed")
        stage = "选择文件夹"
        status, _ = connection.select(folder, readonly=True)
        if status != "OK":
            raise ValueError("select failed")
        _, validity = connection.response("UIDVALIDITY")
        if not validity or not validity[0] or not validity[0].isdigit():
            raise ValueError("missing UIDVALIDITY")
        source = dict(host=host.lower(), account=account.lower(), folder=folder, uidvalidity=validity[0].decode("ascii"))
        progress = trusted_progress(progress)
        incremental = progress is not None and progress["uidvalidity"] == source["uidvalidity"]
        previous = progress["max_uid"] if incremental else 0
        stage = "获取 UID 列表"
        criteria = ("UID", f"{min(previous + 1, 4294967295)}:*") if incremental else ("ALL",)
        status, values = connection.uid("search", None, *criteria)
        if status != "OK" or not values or not isinstance(values[0], bytes):
            raise ValueError("search failed")
        queried = values[0].split()
        if any(not uid.isdigit() or not 1 <= int(uid) <= 4294967295 for uid in queried):
            raise ValueError("invalid UID search response")
        # Some IMAP servers return the current highest UID for an empty N:* range.
        uids = [str(uid).encode("ascii") for uid in sorted({int(uid) for uid in queried if int(uid) > previous})]
        rows, failures = [], []
        fetched = 0

        def fail(uid, kind):
            failures.append(dict(source, imap_uid=uid.decode("ascii"),
                                 failed_at=datetime.now(timezone.utc).isoformat(), error_type=kind))

        for start in range(0, len(uids), 100):
            batch = uids[start:start + 100]
            try:
                status, parts = connection.uid("fetch", b",".join(batch), "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE IN-REPLY-TO REFERENCES)])")
                if status != "OK":
                    raise ValueError("fetch status failed")
            except (OSError, imaplib.IMAP4.error, ValueError):
                for uid in batch:
                    fail(uid, "fetch_failed")
                continue
            headers = {}
            for part in parts or []:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[0], bytes):
                    match = re.search(rb"\bUID\s+(\d+)\b", part[0])
                    if match and match[1] in batch and isinstance(part[1], bytes):
                        headers[match[1]] = part[1]
            fetched += len(headers)
            for uid in batch:
                if uid not in headers:
                    fail(uid, "missing_header")
                    continue
                try:
                    message = BytesHeaderParser(policy=policy.default).parsebytes(headers[uid])
                    if message.defects:
                        raise ValueError("malformed header")
                    rows.append(dict(imap_uid=uid.decode("ascii"), message_id=str(message.get("Message-ID", "")).strip(),
                                     subject=str(message.get("Subject", "")), **{"from": str(message.get("From", ""))}, date=str(message.get("Date", "")),
                                     in_reply_to=str(message.get("In-Reply-To", "")), references=str(message.get("References", ""))))
                except (ValueError, TypeError, LookupError):
                    fail(uid, "parse_error")
        return dict(source=source, metadata=rows, failures=failures,
                    progress=dict(uidvalidity=source["uidvalidity"], max_uid=max([previous] + [int(uid) for uid in uids]),
                                  completed_at=datetime.now(timezone.utc).isoformat()),
                    mode="incremental" if incremental else "full", queried_uid_count=len(queried),
                    eligible_uid_count=len(uids), fetched_header_count=fetched, success_count=len(rows))
    except (OSError, imaplib.IMAP4.error, ValueError):
        # Server errors may echo authentication data. Never include them in user/model output.
        raise ValueError(f"IMAP {stage}失败；原索引未修改") from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


def format_table(rows):
    def cell(value):
        return "".join(c if c.isprintable() and c != "|" else " " for c in str(value))
    lines = ["ID | Subject | From | Date | Imported"]
    for row in rows:
        lines.append(" | ".join(cell(v) for v in (row["id"], row["subject"] or "（无主题）", row["from"], row["date"], "已导入" if row["imported"] else "未导入")))
    return "\n".join(lines)


def update_email_index(config=None, index_path=INDEX_PATH):
    """Single sync core used by the CLI and registered Agent tool.

    Only this layer fetches metadata and persists index state. Entry points may
    format or limit the returned rows, but never assign IDs or import status.
    """
    started = time.perf_counter()
    settings = dict(os.environ)
    settings.update(load_config() if config is None else config)
    index = EmailIndex(index_path)
    # Serialize snapshot -> network -> atomic commit so concurrent entrypoints
    # cannot regress the mailbox watermark or overwrite imported flags.
    with index.locked():
        data = index.read()
        progress_by_scope = data.setdefault("sync_progress", {})
        failures = data.setdefault("sync_failures", [])
        if not isinstance(progress_by_scope, dict) or not isinstance(failures, list):
            raise ValueError("邮件同步记录格式损坏；原索引未修改")
        key = scope_key(settings)
        fetched = fetch_metadata(settings, progress_by_scope.get(key))
        added = index.merge_rows(data, fetched["source"], fetched["metadata"])
        failures.extend(fetched["failures"])
        progress_by_scope[key] = fetched["progress"]
        index.write(data)
    rows = data["emails"]
    result = {k: fetched[k] for k in ("source", "mode", "queried_uid_count", "eligible_uid_count", "fetched_header_count", "success_count")}
    result.update(added=added, total=len(rows), skipped_uids=[f["imap_uid"] for f in fetched["failures"]],
                  skipped_count=len(fetched["failures"]), failure_records_location=str(index.path.resolve()) + "#sync_failures",
                  emails=rows, table=format_table(rows), elapsed_seconds=round(time.perf_counter() - started, 3))
    result["sync_summary"] = (f"共 {result['total']} 封，新增 {result['added']} 封；{result['mode']} 同步：查询 UID {result['queried_uid_count']}，实际获取邮件头 {result['fetched_header_count']}，"
                              f"成功 {result['success_count']}，跳过 {result['skipped_count']}；失败记录：{result['failure_records_location']}")
    return result


def update_email(config=None, index_path=INDEX_PATH):
    """Compatibility for existing Python callers; delegates to the same core."""
    return update_email_index(config=config, index_path=index_path)


def main():
    try:
        print("同步邮箱……")
        result = update_email_index()
        print(result["table"])
        print(result["sync_summary"])
        return 0
    except (OSError, ValueError) as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
