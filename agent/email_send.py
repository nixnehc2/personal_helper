"""Confirmed SMTP sending for saved email drafts."""
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import os
import re
import smtplib
import ssl

from .email_index import EmailIndex
from .email_drafts import DraftStore
from .llm import load_config

EMAIL_ADDRESS = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def load_settings():
    settings = dict(os.environ)
    settings.update(load_config())
    return settings


def valid_address(address):
    return bool(EMAIL_ADDRESS.fullmatch(address or ""))


def validate_for_sending(draft):
    if draft["status"] != "draft":
        raise ValueError(
            f"Draft #{draft['id']} 已经于 {draft.get('sent_at', '未知时间')} 发送，拒绝重复发送")
    if not valid_address(draft["to"]):
        raise ValueError(f"Draft #{draft['id']} 缺少有效收件人邮箱，已拒绝发送")


def build_message(snapshot, settings):
    account = settings.get("EMAIL_ACCOUNT", "")
    if not valid_address(account):
        raise ValueError("请在 config.local.json 配置有效的 EMAIL_ACCOUNT")
    message = EmailMessage()
    message["From"] = account
    message["To"] = snapshot["to"]
    message["Subject"] = snapshot["subject"]
    message["Date"] = formatdate(localtime=False)
    domain = account.rsplit("@", 1)[1]
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(snapshot["body"])
    return message


def smtp_deliver(message, settings, connect=None):
    account = settings.get("EMAIL_ACCOUNT", "")
    password = settings.get("EMAIL_AUTH_CODE", "")
    if not account or not password:
        raise ValueError("请在 config.local.json 配置 EMAIL_ACCOUNT 和 EMAIL_AUTH_CODE")
    host = settings.get("EMAIL_SMTP_HOST", "smtp.qq.com")
    try:
        port = int(settings.get("EMAIL_SMTP_PORT", "465"))
    except ValueError:
        raise ValueError("EMAIL_SMTP_PORT 必须是整数") from None
    if not 1 <= port <= 65535:
        raise ValueError("EMAIL_SMTP_PORT 必须在 1 到 65535 之间")
    connect = connect or smtplib.SMTP_SSL
    try:
        with connect(host, port, timeout=30,
                     context=ssl.create_default_context()) as connection:
            connection.login(account, password)
            connection.send_message(message)
    except smtplib.SMTPAuthenticationError:
        raise ValueError("SMTP 认证失败，请检查邮箱地址和授权码；草稿未删除，可以重新尝试") from None
    except (OSError, smtplib.SMTPException):
        raise ValueError("SMTP 连接或发送失败，草稿未删除，可以重新尝试") from None


def send_email(draft_id, confirm, connect=None):
    if type(draft_id) is not int or draft_id <= 0:
        raise ValueError("草稿 ID 必须是正整数")
    if not callable(confirm):
        raise ValueError("发送邮件必须经过 Runtime 用户确认")
    store = DraftStore()
    # Keep the approval, SMTP operation, and sent-state update serialized.
    with EmailIndex(store.path / "drafts.json").locked():
        draft = store.read(draft_id)
        validate_for_sending(draft)
        snapshot = {key: draft[key] for key in ("id", "to", "subject", "body",
                                                "status", "created_at", "updated_at")}
        if not confirm(snapshot):
            return dict(snapshot, status="cancelled", message=f"发送已取消，Draft #{draft_id} 保留。",
                        display=f"发送已取消，Draft #{draft_id} 保留。")
        settings = load_settings()
        message = build_message(snapshot, settings)
        smtp_deliver(message, settings, connect)
        sent = store.mark_sent(snapshot, str(message["Message-ID"]))
        return dict(sent, display=(
            f"Draft #{sent['id']} 已发送。\n\nTo: {sent['to']}\nSubject: {sent['subject']}\n\n{sent['body']}"))
