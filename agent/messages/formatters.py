"""Source-specific presentation; formatters never run an Agent or write Memory."""
import json


def qq_context(message):
    content = message.content
    conversation, sender = content["conversation"], content["sender"]
    kind = {"private": "私聊", "group": "群聊"}.get(conversation["type"], conversation["type"])
    chat = f"{kind} {conversation.get('name', '')} ({conversation['id']})"
    person = f"{sender.get('card') or sender.get('nickname') or ''} ({sender['user_id']})"
    return chat, person


def format_qq(message):
    chat, person = qq_context(message)
    return (f"来源：QQ\n时间：{message.time or '未知'}\n会话：{chat}\n"
            f"发送者：{person}\n消息内容：\n{message.content['text']}")


def summarize_qq(message):
    chat, person = qq_context(message)
    return dict(conversation=chat, sender=person, preview=message.content["text"][:160])


def summarize_email(message):
    content = message.content
    return dict(conversation=content.get("folder", ""), sender=content.get("from", ""),
                subject=content.get("subject", ""), preview=content.get("subject", "")[:160])


def format_email(parsed, *, raw_path=None, authored_by_user=False):
    # Preserve the existing email prompt contract, body, attachment metadata and provenance.
    return "Ingest the following source data: " + json.dumps(dict(
        raw_path=raw_path, authored_by_user=authored_by_user, email=parsed.model_data()), ensure_ascii=False)


def view_email(parsed):
    return "来源：Email\n" + json.dumps(parsed.model_data(), ensure_ascii=False, indent=2)
