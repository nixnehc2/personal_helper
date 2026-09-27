"""Unified Message reading layer.

Provides `get_message` and `list_messages` as the single entry point
for upper-layer code to access messages from any supported source.

Currently only `"email"` is implemented, backed by
:mod:gent.email_index and :mod:gent.messages.email_adapter.
"""

from __future__ import annotations

from typing import Any

from .models import Message
from .email_adapter import email_row_to_message

__all__ = ["Message", "email_row_to_message", "get_message", "list_messages"]

_SUPPORTED_SOURCES = ("email",)


def get_message(source: str, id: int) -> Message:
    """Return a single Message identified by *(source, id)*.

    Parameters
    ----------
    source:
        Origin identifier, e.g. `"email"`.
    id:
        Source-local message identifier.

    Raises
    ------
    ValueError
        If *source* is not supported or *id* does not exist.
    """
    if source not in _SUPPORTED_SOURCES:
        raise ValueError(
            f"不支持的消息来源 {source!r}；当前仅支持 {_SUPPORTED_SOURCES}"
        )
    if source == "email":
        from agent.email_index import EmailIndex, INDEX_PATH

        row = EmailIndex(INDEX_PATH).get(id)
        return email_row_to_message(row)
    # Future sources (qq, wechat, ...) go here.
    raise ValueError(f"消息来源 {source!r} 暂未实现")


def list_messages(source: str, *, imported: bool | None = None) -> list[Message]:
    """Return all Messages for *source*, optionally filtered by *imported*.

    Parameters
    ----------
    source:
        Origin identifier, e.g. `"email"`.
    imported:
        If not `None`, filter by the imported flag.

    Raises
    ------
    ValueError
        If *source* is not supported.
    """
    if source not in _SUPPORTED_SOURCES:
        raise ValueError(
            f"不支持的消息来源 {source!r}；当前仅支持 {_SUPPORTED_SOURCES}"
        )
    if source == "email":
        from agent.email_index import EmailIndex, INDEX_PATH

        data = EmailIndex(INDEX_PATH).read()
        rows = data.get("emails", [])
        if imported is not None:
            rows = [r for r in rows if bool(r["imported"]) == imported]
        return [email_row_to_message(r) for r in rows]
    raise ValueError(f"消息来源 {source!r} 暂未实现")
