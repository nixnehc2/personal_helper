"""Unified Message query API with source-specific storage and presentation."""
from datetime import datetime, timezone

from .models import Message
from .email_adapter import email_row_to_message
from .qq_adapter import qq_to_message
from .sources import source_backend, SOURCES

__all__ = ['Message', 'email_row_to_message', 'qq_to_message', 'get_message',
           'list_messages', 'query_messages', 'search_messages', 'read_message']


def get_message(source: str, id: int) -> Message:
    if type(id) is not int or id <= 0:
        raise ValueError('Message ID 必须是正整数')
    return source_backend(source).get(id)


def _time(value):
    if not isinstance(value, str):
        raise ValueError('时间必须是带时区的 ISO 8601 字符串')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('时间必须是带时区的 ISO 8601 字符串') from None
    if parsed.tzinfo is None:
        raise ValueError('时间必须包含时区，例如 2026-09-28T00:00:00+08:00')
    return parsed.astimezone(timezone.utc)


def list_messages(source: str | None = None, *, conversation=None, time_from=None, time_to=None,
                  imported=None, limit=20, offset=0) -> list[Message]:
    backends = {name: source_backend(name) for name in SOURCES} if source is None else {source: source_backend(source)}
    if imported is not None and type(imported) is not bool:
        raise ValueError('imported 必须是布尔值')
    if conversation is not None and (not isinstance(conversation, str) or not conversation):
        raise ValueError('conversation 必须是非空字符串')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit 必须为 1~100 的整数')
    if type(offset) is not int or not 0 <= offset <= 9223372036854775807:
        raise ValueError('offset 必须为 0~9223372036854775807 的整数')
    begin = _time(time_from) if time_from is not None else None
    end = _time(time_to) if time_to is not None else None
    if begin and end and begin > end:
        raise ValueError('时间范围起点不能晚于终点')
    from .pagination import page_messages
    return page_messages(backends, conversation=conversation, begin=begin, end=end,
                         imported=imported, limit=limit, offset=offset)


def query_messages(source=None, *, limit=20, offset=0, **filters):
    messages = list_messages(source, limit=limit, offset=offset, **filters)
    return dict(_summaries(messages, f'当前返回 {len(messages)} 条消息，offset={offset}'),
                offset=offset, limit=limit)


def _summaries(messages, heading=None):
    summaries = [dict(id=m.id, source=m.source, time=m.time, imported=m.imported,
                      **source_backend(m.source).summarize(m)) for m in messages]
    lines = [heading or f'当前返回 {len(summaries)} 条消息']
    for row in summaries:
        # User content is displayed as data; no source-specific branches in the query layer.
        preview = row['preview'].replace('\r', '\\r').replace('\n', '\\n')
        lines.append(f"{row['id']} | {row['time'] or '未知时间'} | {row['conversation']} | "
                     f"{row['sender']} | imported={str(row['imported']).lower()} | {preview}")
    if not summaries:
        lines.append('没有符合条件的 Message')
    return dict(messages=summaries, count=len(summaries),
                display='\n'.join(lines))


def read_message(id, source=None):
    from .importing import resolve_message
    backend, message = resolve_message(id, source)
    return dict(id=id, source=message.source, time=message.time, imported=message.imported,
                display=backend.read(message))


def search_messages(query: str, source: str | None = None):
    """Read-only literal substring search over all stored leaf values, newest 100."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query 必须是非空字符串，不能仅包含空白')
    backends = ({name: source_backend(name) for name in SOURCES} if source is None
                else {source: source_backend(source)})
    from .pagination import page_messages
    messages, truncated = page_messages(backends, conversation=None, begin=None, end=None,
                                        imported=None, limit=101, offset=0, query=query)
    import json
    heading = f'搜索 {json.dumps(query, ensure_ascii=False)}，'
    heading += ('结果超过 100 条，仅显示最近 100 条，请使用更具体的关键词。' if truncated
                else f'找到 {len(messages)} 条 Message，按时间从新到旧：')
    return dict(_summaries(messages, heading), query=query, source=source, truncated=truncated)
