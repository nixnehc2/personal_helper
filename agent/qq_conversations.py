"""Shared QQ discovery; display indexes are ephemeral, type + id are stable."""
from .qq_client import QQClientError


def conversation_identity(conversation):
    return conversation["type"], str(conversation["id"])


def build_conversation_list(client, *, on_error=None):
    """Groups first, friends second, each sorted by display name.

    Interactive callers fail on incomplete discovery. Sync can report a failed
    source through on_error and continue with the other source as before.
    """
    conversations = []
    for kind, method, key in (
        ("group", client.list_group_chats, "group_id"),
        ("private", client.list_private_chats, "user_id"),
    ):
        try:
            for item in method():
                if kind == "group":
                    name = item.get("group_name") or "未知群"
                else:
                    name = ((item.get("remark") or "").strip()
                            or (item.get("nickname") or "").strip() or str(item[key]))
                conversations.append(dict(type=kind, id=str(item[key]), name=name))
        except (QQClientError, ValueError, TypeError, KeyError) as exc:
            if on_error is None:
                raise
            on_error(kind, exc)
    conversations.sort(key=lambda c: (0 if c["type"] == "group" else 1, c["name"]))
    for index, conversation in enumerate(conversations, 1):
        conversation["display_index"] = index
    return conversations
