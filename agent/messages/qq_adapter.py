"""Pure NapCat text adapter; unsupported segments are skipped as a whole."""
import hashlib
import json
from datetime import datetime, timezone

from .models import Message


def qq_to_message(raw, conversation, account_id):
    segments = raw.get("message")
    if isinstance(segments, str):
        # OneBot string format uses CQ escaping, unlike array text segments.
        if "[CQ:" in segments or not segments:
            return None
        text = segments.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")
    elif isinstance(segments, list) and segments:
        if any(not isinstance(s, dict) or s.get("type") != "text" for s in segments):
            return None
        parts = [s["data"]["text"] for s in segments]
        if any(not isinstance(p, str) for p in parts):
            raise ValueError("QQ text segment 必须包含字符串 text")
        text = "".join(parts)
        if not text:
            return None
    else:
        return None
    kind, peer = conversation["type"], str(conversation["id"])
    if kind not in ("private", "group"):
        raise ValueError("未知 QQ 会话类型")
    original_id = raw.get("message_id")
    if original_id is None or str(original_id) == "":
        raise ValueError("QQ 消息缺少稳定 message_id")
    if raw.get("message_type", kind) != kind or str(raw.get("self_id", account_id)) != str(account_id):
        raise ValueError("QQ 消息账号或会话类型不匹配")
    if kind == "group" and str(raw.get("group_id", peer)) != peer:
        raise ValueError("QQ 消息群号不匹配")
    sender = raw["sender"]
    if not isinstance(sender, dict) or not sender.get("user_id"):
        raise ValueError("QQ 消息缺少发送者")
    identity = json.dumps(["qq", str(account_id), kind, peer, str(original_id)], separators=(",", ":"))
    # Keep the established integer ID contract. Store as decimal TEXT in SQLite.
    stable_id = int.from_bytes(hashlib.sha256(identity.encode()).digest(), "big")
    timestamp = datetime.fromtimestamp(float(raw["time"]), timezone.utc).isoformat()
    return Message(stable_id, "qq", timestamp, False, {
        "text": text, "sender": dict(sender),
        "conversation": {"type": kind, "id": peer, "name": conversation.get("name", "")},
        "account_id": str(account_id), "message_id": original_id,
    })
