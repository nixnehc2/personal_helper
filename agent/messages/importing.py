"""Source-independent selection, duplicate protection and import completion."""
from contextlib import ExitStack

from .sources import SOURCES, source_backend


def resolve_message(id, source=None, backend=None):
    if type(id) is not int or id <= 0:
        raise ValueError("Message ID 必须是正整数")
    if backend is not None:
        return backend, backend.get(id)
    if source is not None:
        selected = source_backend(source)
        return selected, selected.get(id)
    matches = []
    for name in SOURCES:
        selected = source_backend(name)
        # Do not mask storage corruption as 'not found'.
        matches.extend((selected, message) for message in selected.list() if message.id == id)
    if len(matches) != 1:
        raise ValueError("Message ID 不存在或跨来源重复，请明确指定 source 和 ID")
    return matches[0]


def import_message(id, client, files, *, source=None, messages=None, emit=print, backend=None):
    if files.processing_message or files.read_only or files.edit_learning:
        raise ValueError("当前流程不允许导入 Message")
    with files.policy.scheduler.turn(emit):
        selected, message = resolve_message(id, source, backend)
        key = (message.source, message.id)
        if key in files.policy.completion_callbacks:
            return dict(id=id, source=message.source, status="pending_review", imported=False,
                        note="该消息已有待审阅修改，请先提交或取消 Temporary")
        with ExitStack() as stack:
            stack.enter_context(selected.lock(id))
            message = selected.get(id)
            if message.imported:
                return dict(id=id, source=message.source, status="already_imported", imported=True,
                            note="该消息已经导入")
            discarded = files.policy.discard_revision
            result = selected.process(message, client, files, messages=messages, emit=emit)
            if result.get("status") != "processed":
                raise ValueError("Message 导入流程未正常完成；未标记已导入，Temporary 保留，可重试")
            result.update(id=id, source=message.source, imported=False)
            if files.policy.discard_revision != discarded:
                return dict(result, status="discarded", note="本次 Memory 修改已放弃，未标记已导入")
            if files.policy.changes:
                # Keep the cross-session import lock until the user's final decision.
                held = stack.pop_all()
                def complete(committed):
                    try:
                        state = selected.mark_imported(message) if committed else dict(imported=False)
                        return dict(id=id, source=message.source, **state)
                    finally:
                        held.close()
                files.policy.completion_callbacks[key] = complete
                return dict(result, status="pending_review", note="Temporary 尚未提交；提交后才标记已导入")
            return dict(result, **selected.mark_imported(message), note="消息处理完成")
