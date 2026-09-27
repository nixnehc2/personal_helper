"""One-shot QQ sync. Message insertion and separate checkpoints commit together."""
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .messages.models import Message
from .messages.qq_adapter import qq_to_message
from .qq_client import QQClient, QQClientError

DB_PATH = Path(__file__).resolve().parent.parent / "data/qq/messages.sqlite3"


class QQStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DB_PATH

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS checkpoints (scope TEXT PRIMARY KEY, message_id TEXT NOT NULL)")
        return db

    def list(self, imported=None):
        if not self.path.exists():
            return []
        db = self.connect()
        try:
            messages = [Message(**json.loads(row[0])) for row in db.execute("SELECT payload FROM messages ORDER BY rowid")]
            return [m for m in messages if imported is None or m.imported == imported]
        finally:
            db.close()

    def get(self, id):
        message = next((m for m in self.list() if m.id == id), None)
        if message is None:
            raise ValueError(f"QQ Message {id} 不存在")
        return message


def update_qq(config=None, db_path=None, client=None, page_size=100):
    if not 2 <= page_size <= 1000:
        raise ValueError("QQ page_size 必须在 2~1000 之间")
    if client is None:
        from .llm import load_config
        config = load_config() if config is None else config
        client = QQClient(config.get("QQ_API_URL", "http://127.0.0.1:3000"), config.get("QQ_ACCESS_TOKEN", ""))
    result = dict(scanned=0, text=0, added=0, duplicates=0, skipped=0, failed=0, errors=[])
    db = QQStore(db_path).connect()
    try:
        account = str(client.get_login_info()["user_id"])
        conversations = []
        for kind, method, key, name in (
            ("group", client.list_group_chats, "group_id", "group_name"),
            ("private", client.list_private_chats, "user_id", "nickname"),
        ):
            try:
                conversations.extend(dict(type=kind, id=c[key], name=c.get(name, "")) for c in method())
            except (QQClientError, ValueError, TypeError, KeyError) as exc:
                result["failed"] += 1
                result["errors"].append(f"{kind}: {exc}")
        for conversation in conversations:
            scope = json.dumps([account, conversation["type"], str(conversation["id"])])
            counts = dict(scanned=0, text=0, added=0, duplicates=0, skipped=0, failed=0)
            try:
                db.execute("BEGIN IMMEDIATE")
                checkpoint = db.execute("SELECT message_id FROM checkpoints WHERE scope=?", (scope,)).fetchone()
                checkpoint = checkpoint[0] if checkpoint else None
                cursor, newest, seen = None, None, set()
                message_failed = False
                for _ in range(10000):
                    batch = client.get_history_page(conversation["type"], conversation["id"], page_size, cursor)
                    if not batch:
                        break
                    ids = [str(m["message_id"]) for m in batch]
                    newest = newest or ids[-1]
                    fresh = [(m, id) for m, id in zip(batch, ids) if id not in seen]
                    if not fresh:
                        # Inclusive boundary at the oldest available message is normal.
                        if len(batch) == 1 and ids[0] == cursor:
                            break
                        raise ValueError("QQ 历史分页未前进；保留 checkpoint 以便重试")
                    for raw, original_id in fresh:
                        if original_id in seen:
                            continue
                        seen.add(original_id)
                        counts["scanned"] += 1
                        try:
                            message = qq_to_message(raw, conversation, account)
                            if message is None:
                                counts["skipped"] += 1
                                continue
                            counts["text"] += 1
                            inserted = db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?)",
                                (str(message.id), json.dumps(asdict(message), ensure_ascii=False))).rowcount
                            counts["added" if inserted else "duplicates"] += 1
                        except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
                            counts["failed"] += 1
                            message_failed = True
                            result["errors"].append(f"{scope} / {original_id}: {exc}")
                    if checkpoint in ids or len(batch) < page_size:
                        break
                    cursor = ids[0]
                else:
                    raise ValueError("QQ 历史超过分页上限；保留 checkpoint")
                if newest is not None and not message_failed:
                    db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?)", (scope, newest))
                db.commit()
                for key, value in counts.items():
                    result[key] += value
            except (QQClientError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                db.rollback()
                result["failed"] += 1
                result["errors"].append(f"{scope}: {exc}")
    except (QQClientError, ValueError, TypeError, KeyError) as exc:
        result["failed"] += 1
        result["errors"].append(str(exc))
    finally:
        db.close()
    result["display"] = ("QQ 同步完成" if not result["failed"] else "QQ 同步完成（存在失败，请重试）") + "\n" + "\n".join(
        f"{label}：{result[key]}" for label, key in (("扫描", "scanned"), ("纯文字", "text"),
        ("新增", "added"), ("重复", "duplicates"), ("跳过非文字", "skipped"), ("失败", "failed")))
    return result
