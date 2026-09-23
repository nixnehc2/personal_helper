"""Local metadata index. No EML download, model call, or Memory writes."""
import copy
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

    def merge(self, source, metadata):
        with self.locked():
            data = copy.deepcopy(self.read())
            added = 0
            for item in metadata:
                scoped = [r for r in data["emails"] if all(r[k] == source[k] for k in ("host", "account", "folder"))]
                existing = next((r for r in scoped if r["uidvalidity"] == source["uidvalidity"] and r["imap_uid"] == item["imap_uid"]), None)
                if existing is None and item["message_id"]:
                    existing = next((r for r in scoped if r["message_id"] == item["message_id"]), None)
                if existing is not None:
                    existing.update(source)
                    existing.update(item)
                else:
                    data["emails"].append(dict(source, **item, id=data["next_id"], imported=False, imported_at=None))
                    data["next_id"] += 1
                    added += 1
            self.write(data)
            return data["emails"], added


def fetch_metadata(settings):
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
        connection.login(account, password)
        stage = "选择文件夹"
        status, _ = connection.select(folder, readonly=True)
        if status != "OK":
            raise ValueError("select failed")
        _, validity = connection.response("UIDVALIDITY")
        if not validity or not validity[0] or not validity[0].isdigit():
            raise ValueError("missing UIDVALIDITY")
        source = dict(host=host.lower(), account=account.lower(), folder=folder, uidvalidity=validity[0].decode("ascii"))
        stage = "获取 UID 列表"
        status, values = connection.uid("search", None, "ALL")
        if status != "OK" or not values or values[0] is None:
            raise ValueError("search failed")
        rows, skipped = [], []
        uids = values[0].split()
        for start in range(0, len(uids), 100):
            batch = uids[start:start + 100]
            stage = "获取邮件头"
            status, parts = connection.uid("fetch", b",".join(batch), "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])")
            if status != "OK":
                skipped.extend(uid.decode("ascii") for uid in batch)
                continue
            headers = {}
            for part in parts or []:
                if isinstance(part, tuple) and isinstance(part[0], bytes):
                    match = re.search(rb"\bUID\s+(\d+)\b", part[0])
                    if match:
                        headers[match[1]] = part[1]
            for uid in batch:
                try:
                    message = BytesHeaderParser(policy=policy.default).parsebytes(headers[uid])
                    rows.append(dict(imap_uid=uid.decode("ascii"), message_id=str(message.get("Message-ID", "")).strip(),
                                     subject=str(message.get("Subject", "")), **{"from": str(message.get("From", ""))}, date=str(message.get("Date", ""))))
                except (ValueError, TypeError, LookupError):
                    skipped.append(uid.decode("ascii"))
        return source, rows, skipped
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
    settings = dict(os.environ)
    settings.update(load_config() if config is None else config)
    source, metadata, skipped = fetch_metadata(settings)
    rows, added = EmailIndex(index_path).merge(source, metadata)
    return dict(added=added, total=len(rows), skipped_uids=skipped, emails=rows, table=format_table(rows))


def update_email(config=None, index_path=INDEX_PATH):
    """Compatibility for existing Python callers; delegates to the same core."""
    return update_email_index(config=config, index_path=index_path)


def main():
    try:
        print("同步邮箱……")
        result = update_email_index()
        print(result["table"])
        print(f"共 {result['total']} 封，新增 {result['added']} 封，跳过 {len(result['skipped_uids'])} 封")
        return 0
    except (OSError, ValueError) as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
