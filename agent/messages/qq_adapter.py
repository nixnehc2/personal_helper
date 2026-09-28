"""Extract readable QQ text without expanding media, cards or reply targets."""
from copy import deepcopy
import hashlib
import json
import re
from datetime import datetime, timezone

from .models import Message


_CQ_SEGMENT = re.compile(r"\[CQ:([A-Za-z0-9_]+)(?:,([^\[\]]*))?\]")


def _unescape(text):
    # Decode once, after recognizing CQ syntax. Escaped CQ-looking text is literal.
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def _segment_id(data, key):
    value = data.get(key)
    if type(value) not in (str, int) or not str(value).strip():
        raise ValueError(f"QQ segment 必须包含有效 {key}")
    return str(value)


def segment_to_text(segment):
    if (not isinstance(segment, dict) or not isinstance(segment.get("type"), str)
            or not segment["type"] or not isinstance(segment.get("data"), dict)):
        raise ValueError("QQ segment 必须包含字符串 type 和对象 data")
    kind, data = segment["type"], segment["data"]
    if kind == "text":
        text = data.get("text")
        if not isinstance(text, str):
            raise ValueError("QQ text segment 必须包含字符串 text")
        return text
    if kind == "at":
        target = _segment_id(data, "qq")
        return "@全体成员" if target == "all" else "@" + target
    if kind == "reply":
        return f"[回复:{_segment_id(data, 'id')}]"
    # Unknown but structurally valid segments are also ignored, not errors.
    return ""


def _cq_to_text(message):
    parts, end = [], 0
    for match in _CQ_SEGMENT.finditer(message):
        parts.append(_unescape(message[end:match.start()]))
        kind = match[1]
        if kind in ("at", "reply"):
            data = {}
            for field in (match[2] or "").split(","):
                key, separator, value = field.partition("=")
                if separator:
                    data[key] = _unescape(value.replace("&#44;", ","))
            parts.append(segment_to_text(dict(type=kind, data=data)))
        end = match.end()
    parts.append(_unescape(message[end:]))
    return "".join(parts)


def qq_to_message(raw, conversation, account_id):
    if not isinstance(raw, dict):
        raise ValueError("QQ 消息必须是对象")
    segments = raw.get("message")
    if isinstance(segments, str):
        text = _cq_to_text(segments)
    elif isinstance(segments, list):
        text = "".join(segment_to_text(segment) for segment in segments)
    else:
        raise ValueError("QQ message 必须是 segment 数组或 CQ 字符串")
    if not text:
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
        "text": text, "segments": deepcopy(segments), "sender": dict(sender),
        "conversation": {"type": kind, "id": peer, "name": conversation.get("name", "")},
        "account_id": str(account_id), "message_id": original_id,
    })
