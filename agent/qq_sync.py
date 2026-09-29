"""One-shot QQ sync. Message insertion and separate checkpoints commit together."""
import json
import logging
import os
from copy import deepcopy
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .messages.models import Message
from .messages.qq_adapter import qq_to_message
from .qq_client import QQClient, QQClientError
from .qq_conversations import build_conversation_list, conversation_identity

DB_PATH = Path(__file__).resolve().parent.parent / "data/qq/messages.sqlite3"


class QQStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DB_PATH

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS checkpoints (scope TEXT PRIMARY KEY, message_id TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS conversation_states (scope TEXT PRIMARY KEY, state TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
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
        if type(id) is not int or id <= 0:
            raise ValueError("Message ID 必须是正整数")
        message = next((m for m in self.list() if m.id == id), None)
        if message is None:
            raise ValueError(f"QQ Message {id} 不存在")
        return message

    def rowid_cursor(self):
        """Return the current maximum rowid, or 0 if the table is empty."""
        if not self.path.exists():
            return 0
        db = self.connect()
        try:
            row = db.execute("SELECT MAX(rowid) FROM messages").fetchone()
            return row[0] or 0
        finally:
            db.close()

    def messages_after_rowid(self, after_rowid):
        """Return (payload, rowid) tuples with rowid > after_rowid, in rowid order."""
        if not self.path.exists():
            return []
        db = self.connect()
        try:
            return [(row[0], row[1]) for row in db.execute(
                "SELECT payload, rowid FROM messages WHERE rowid > ? ORDER BY rowid",
                (after_rowid,)
            ).fetchall()]
        finally:
            db.close()
    def mark_imported(self, expected):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM messages WHERE id=?", (str(expected.id),)).fetchone()
            if row is None:
                raise ValueError("QQ Message 在导入期间已不存在")
            current = Message(**json.loads(row[0]))
            if (current.id, current.source, current.time, current.content) != (
                    expected.id, expected.source, expected.time, expected.content):
                raise ValueError("QQ Message 在导入期间发生变化；未标记已导入")
            current.imported = True
            db.execute("UPDATE messages SET payload=? WHERE id=?",
                       (json.dumps(asdict(current), ensure_ascii=False), str(current.id)))
            db.commit()
        finally:
            db.close()


def update_qq(config=None, db_path=None, client=None, page_size=100, progress=None, skip_event=None,
              allowed_conversations=None):
    """Sync all conversations when allowed_conversations is None, otherwise only (type, str(id)) identities.

    Persistent skip still wins. skip_event skips the current conversation.
    """
    if not 2 <= page_size <= 1000:
        raise ValueError("QQ page_size 必须在 2~1000 之间")
    if client is None:
        from .llm import load_config
        config = {**os.environ, **load_config()} if config is None else config
        client = QQClient(config.get("QQ_API_URL", "http://127.0.0.1:3000"), config.get("QQ_ACCESS_TOKEN", ""))
    result = dict(scanned=0, text=0, added=0, duplicates=0, skipped=0, failed=0, errors=[])
    # Observation only: snapshots never share mutable objects with sync state.
    total_conversations, current_conversation, completed_conversations = None, 0, 0

    def report(event, conversation=None, counts=None, page=0, *, provisional=False,
               error=None, rolled_back=False):
        if progress is None:
            return
        try:
            committed = {key: result[key] for key in ("scanned", "text", "added", "duplicates", "skipped", "failed")}
            current = dict(counts) if counts is not None else dict.fromkeys(committed, 0)
            if rolled_back and error is not None:
                current["failed"] += 1
            progress(deepcopy(dict(
                event=event, current_conversation=current_conversation,
                completed_conversations=completed_conversations, total_conversations=total_conversations,
                conversation=conversation, page=page, conversation_counts=current,
                committed_counts=committed,
                total_counts={key: value + (current[key] if provisional else 0) for key, value in committed.items()},
                provisional=provisional, rolled_back=rolled_back, error=error,
            )))
        except Exception:
            # A broken display/observer must not change transactions or the result.
            pass

    db = QQStore(db_path).connect()
    try:
        report("connecting")
        account = str(client.get_login_info()["user_id"])
        def discovery_error(kind, exc):
            result["failed"] += 1
            result["errors"].append(f"{kind}: {exc}")
            report("error", {"type": kind}, error=result["errors"][-1])

        conversations = build_conversation_list(client, on_error=discovery_error)
        if allowed_conversations is not None:
            allowed = set(allowed_conversations)
            found = {conversation_identity(c) for c in conversations}
            for kind, peer in sorted(allowed - found):
                logging.warning("configured QQ conversation not found: [%s] %s", kind, peer)
            conversations = [c for c in conversations if conversation_identity(c) in allowed]
        total_conversations = len(conversations)
        report("start")
        for conversation in conversations:
            scope = json.dumps([account, conversation["type"], str(conversation["id"])])
            counts = dict(scanned=0, text=0, added=0, duplicates=0, skipped=0, failed=0)
            current_conversation += 1
            page = 0
            rolled_back = False

            # Check persistent skip state before starting.
            row = db.execute("SELECT state FROM conversation_states WHERE scope=?", (scope,)).fetchone()
            if row and row[0] == "skip":
                result["skipped"] += 1
                report("conversation_skipped", conversation, counts)
                completed_conversations += 1
                continue

            # Clear any leftover skip request from a previous conversation.
            if skip_event is not None:
                skip_event.clear()

            report("conversation_start", conversation, counts)
            try:
                db.execute("BEGIN IMMEDIATE")
                checkpoint = db.execute("SELECT message_id FROM checkpoints WHERE scope=?", (scope,)).fetchone()
                checkpoint = checkpoint[0] if checkpoint else None
                cursor, newest, seen = None, None, set()
                message_failed = False
                for _ in range(10000):
                    # Check skip request before each page.
                    if skip_event is not None and skip_event.is_set():
                        break

                    batch = client.get_history_page(conversation["type"], conversation["id"], page_size, cursor)
                    page += 1

                    # Check skip request after network call returns.
                    if skip_event is not None and skip_event.is_set():
                        break

                    if not batch:
                        report("page", conversation, counts, page, provisional=True)
                        break
                    ids = [str(m["message_id"]) for m in batch]
                    newest = newest or ids[-1]
                    fresh = [(m, id) for m, id in zip(batch, ids) if id not in seen]
                    if not fresh:
                        # Inclusive boundary at the oldest available message is normal.
                        if len(batch) == 1 and ids[0] == cursor:
                            report("page", conversation, counts, page, provisional=True)
                            break
                        raise ValueError("QQ 历史分页未前进；保留 checkpoint 以便重试")
                    for raw, original_id in fresh:
                        # Check skip request during message processing.
                        if skip_event is not None and skip_event.is_set():
                            break

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
                            report("message_error", conversation, counts, page,
                                   provisional=True, error=result["errors"][-1])

                    # Break out of outer loop if skip requested.
                    if skip_event is not None and skip_event.is_set():
                        break

                    report("page", conversation, counts, page, provisional=True)
                    if checkpoint in ids or len(batch) < page_size:
                        break
                    cursor = ids[0]
                else:
                    raise ValueError("QQ 历史超过分页上限；保留 checkpoint")

                # Handle skip: rollback partial writes and persist skip state.
                if skip_event is not None and skip_event.is_set():
                    db.rollback()
                    rolled_back = True
                    # Save skip state in a separate transaction.
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("INSERT OR REPLACE INTO conversation_states VALUES (?, ?)", (scope, "skip"))
                    db.commit()
                    report("conversation_skipped_user", conversation, counts, page, rolled_back=True)
                else:
                    if newest is not None and not message_failed:
                        db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?)", (scope, newest))
                    db.commit()
                    for key, value in counts.items():
                        result[key] += value
            except (QQClientError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                db.rollback()
                result["failed"] += 1
                result["errors"].append(f"{scope}: {exc}")
                rolled_back = True
                report("error", conversation, counts, page, error=result["errors"][-1], rolled_back=True)
            completed_conversations += 1
            report("conversation_end", conversation, counts, page, rolled_back=rolled_back)
    except (QQClientError, ValueError, TypeError, KeyError) as exc:
        result["failed"] += 1
        result["errors"].append(str(exc))
        report("error", error=result["errors"][-1])
    finally:
        db.close()
    if allowed_conversations is None and not result["failed"]:
        from .qq_sync_schedule import record_manual_success
        try:
            record_manual_success(config, db_path)
        except Exception as exc:
            result["failed"] += 1
            result["errors"].append(f"QQ 同步时间保存失败: {exc}")
    result["display"] = ("QQ 同步完成" if not result["failed"] else "QQ 同步完成（存在失败，请重试）") + "\n" + "\n".join(
        f"{label}：{result[key]}" for label, key in (("扫描", "scanned"), ("可读消息", "text"),
        ("新增", "added"), ("重复", "duplicates"), ("无文字内容跳过", "skipped"), ("失败", "failed")))
    report("finish")
    return result
