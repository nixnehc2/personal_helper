"""Interactive CLI for reading and saving real QQ chat samples.

Launch::

    python -m agent.qq_sample_reader

This script connects to a local NapCat service, lists available
conversations, lets the user pick one, reads the most recent N
messages, displays a simplified preview in the terminal, and saves
the complete raw API response to ``data/qq_samples/``.

This is a Phase 3A tool: it collects raw samples only and never
converts them to the unified ``Message`` model.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .qq_client import QQClient, QQClientError, QQConnectionError
from .llm import load_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SAMPLES_DIR = Path(__file__).resolve().parent.parent / "data" / "qq_samples"
_MAX_MESSAGES = 500


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _build_client() -> QQClient:
    """Create a QQClient from config.local.json values."""
    cfg = load_config()
    api_url = cfg.get("QQ_API_URL", "http://127.0.0.1:3000")
    token = cfg.get("QQ_ACCESS_TOKEN", "")
    return QQClient(api_url=api_url, access_token=token)


# ---------------------------------------------------------------------------
# Conversation list helpers
# ---------------------------------------------------------------------------
def build_conversation_list(client: QQClient) -> list[dict[str, Any]]:
    """Fetch groups and friends, return a unified sorted list.

    Each element::

        {
            "display_index": int,
            "chat_type": "group" | "private",
            "chat_id": int,
            "name": str,
        }

    ``display_index`` is ephemeral -- only for this session's selection.
    """
    groups = client.list_group_chats()
    friends = client.list_private_chats()

    conversations: list[dict[str, Any]] = []
    for g in groups:
        conversations.append({
            "chat_type": "group",
            "chat_id": g.get("group_id", 0),
            "name": g.get("group_name", "未知群"),
        })
    for f in friends:
        remark = (f.get("remark") or "").strip()
        nickname = (f.get("nickname") or "").strip()
        display_name = remark if remark else nickname if nickname else str(f.get("user_id", "未知"))
        conversations.append({
            "chat_type": "private",
            "chat_id": f.get("user_id", 0),
            "name": display_name,
        })

    # Stable sort: groups first, then private, within each by name.
    type_order = {"group": 0, "private": 1}
    conversations.sort(key=lambda c: (type_order.get(c["chat_type"], 9), c["name"]))

    for idx, conv in enumerate(conversations, start=1):
        conv["display_index"] = idx

    return conversations


def print_conversation_list(conversations: list[dict[str, Any]]) -> None:
    """Pretty-print the conversation list to stdout."""
    print("\n可用 QQ 会话：\n")
    for c in conversations:
        type_label = "群聊" if c["chat_type"] == "group" else "私聊"
        id_hint = (
            f"(群号: {c['chat_id']})" if c["chat_type"] == "group"
            else f"(QQ: {c['chat_id']})"
        )
        print(f"  [{c['display_index']}] [{type_label}] {c['name']} {id_hint}")
    print()


# ---------------------------------------------------------------------------
# Message preview
# ---------------------------------------------------------------------------
def _segment_preview(seg: dict[str, Any]) -> str:
    """Return a short human-readable label for a single message segment."""
    seg_type = seg.get("type", "unknown")
    data = seg.get("data", {})
    if seg_type == "text":
        return data.get("text", "")
    if seg_type == "image":
        return "[图片]"
    if seg_type == "face":
        return "[表情]"
    if seg_type == "at":
        qq = data.get("qq", "?")
        return f"@{qq}"
    if seg_type == "reply":
        return "[回复]"
    if seg_type == "file":
        return f"[文件] {data.get('file', '')}"
    if seg_type == "record":
        return "[语音]"
    if seg_type == "video":
        return "[视频]"
    if seg_type == "json":
        return "[JSON卡片]"
    if seg_type == "forward":
        return "[转发消息]"
    return f"[{seg_type}]"


def message_preview_text(msg: dict[str, Any]) -> str:
    """Build a one-line preview string for one QQ message object."""
    # Time
    ts = msg.get("time", 0)
    if ts:
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError):
            time_str = str(ts)
    else:
        time_str = "未知时间"

    # Sender
    sender = msg.get("sender", {})
    card = (sender.get("card") or "").strip()
    nickname = (sender.get("nickname") or "").strip()
    sender_name = card if card else nickname if nickname else str(sender.get("user_id", "?"))

    # Content
    raw = msg.get("raw_message", "")
    if raw:
        content = raw
    else:
        segments = msg.get("message", [])
        if isinstance(segments, list):
            content = "".join(_segment_preview(s) for s in segments)
        else:
            content = str(segments) if segments else "(空)"

    return f"[{time_str}] {sender_name}:\n  {content}"


def print_message_previews(messages: list[dict[str, Any]]) -> None:
    """Display a simplified preview of each message."""
    if not messages:
        print("\n  (无消息)\n")
        return
    for msg in messages:
        print(message_preview_text(msg))
        print()


# ---------------------------------------------------------------------------
# Sample saving
# ---------------------------------------------------------------------------
def generate_sample_filename(chat_type: str, chat_id: int) -> str:
    """Return a deterministic timestamp-based filename for a sample."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_type = "group" if chat_type == "group" else "private"
    return f"{stamp}_{safe_type}_{chat_id}.json"


def save_sample(
    conversation: dict[str, Any],
    requested_count: int,
    messages: list[dict[str, Any]],
    samples_dir: Path | None = None,
) -> Path:
    """Save the full raw sample to disk and return the file path."""
    out_dir = samples_dir or _SAMPLES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = generate_sample_filename(
        conversation["chat_type"], conversation["chat_id"]
    )
    path = out_dir / filename

    sample = {
        "sample_version": 1,
        "captured_at": datetime.now().astimezone().isoformat(),
        "conversation": {
            "type": conversation["chat_type"],
            "id": conversation["chat_id"],
            "name": conversation["name"],
        },
        "requested_count": requested_count,
        "messages": messages,
    }

    path.write_text(
        json.dumps(sample, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------
def _prompt_int(prompt: str, min_val: int, max_val: int) -> int:
    """Read an integer from stdin within [min_val, max_val]."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            print("  请输入一个数字。")
            continue
        try:
            value = int(raw)
        except ValueError:
            print("  无效编号，请重新输入")
            continue
        if value < min_val or value > max_val:
            print(f"  请输入 {min_val} ~ {max_val} 之间的数字。")
            continue
        return value


# ---------------------------------------------------------------------------
# Main interactive flow
# ---------------------------------------------------------------------------
def main() -> int:
    """Entry point for ``python -m agent.qq_sample_reader``."""
    print("=" * 50)
    print("  QQ 真实样本数据读取工具  (Phase 3A)")
    print("=" * 50)

    # 1. Connect
    print("\n正在连接 QQ 服务...")
    try:
        client = _build_client()
        login = client.get_login_info()
        print(f"  已连接：{login.get('nickname', '?')} (QQ: {login.get('user_id', '?')})")
    except QQConnectionError as exc:
        print(f"\n{exc}")
        return 1
    except QQClientError as exc:
        print(f"\nQQ 服务错误：{exc}")
        return 1

    # 2. Build conversation list
    print("\n正在获取会话列表...")
    try:
        conversations = build_conversation_list(client)
    except QQClientError as exc:
        print(f"\n获取会话失败：{exc}")
        return 1

    if not conversations:
        print("\n未获取到任何会话。请确认 NapCat 已登录且有好友或群聊。")
        return 1

    print_conversation_list(conversations)

    # 3. Select conversation
    n_convs = len(conversations)
    sel = _prompt_int("请输入会话编号：", min_val=1, max_val=n_convs)
    chosen = conversations[sel - 1]
    type_label = "群聊" if chosen["chat_type"] == "group" else "私聊"
    print(f"\n  已选择：[{type_label}] {chosen['name']}")

    # 4. Message count
    count = _prompt_int(
        f"\n读取最近多少条消息？(1~{_MAX_MESSAGES}): ",
        min_val=1,
        max_val=_MAX_MESSAGES,
    )

    # 5. Fetch messages
    print(f"\n正在读取 {chosen['name']} 最近 {count} 条消息...")
    try:
        if chosen["chat_type"] == "group":
            messages = client.get_group_messages(chosen["chat_id"], count)
        else:
            messages = client.get_private_messages(chosen["chat_id"], count)
    except QQClientError as exc:
        print(f"\n读取消息失败：{exc}")
        return 1

    print(f"  获取到 {len(messages)} 条消息。")

    # 6. Preview
    print("\n--- 消息预览 ---")
    print_message_previews(messages)

    # 7. Save
    path = save_sample(chosen, count, messages)
    print(f"--- 原始样本已保存 ---")
    print(f"  文件：{path}")
    print(f"  消息数：{len(messages)}")
    print()
    return 0


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    raise SystemExit(main())
