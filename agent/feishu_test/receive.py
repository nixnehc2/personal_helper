"""Feishu Phase 1 — receive messages via long connection (WebSocket).

Usage:
    python -m agent.feishu_test.receive          # receive only
    python -m agent.feishu_test.receive --echo    # receive + auto-reply
"""
import argparse
import json
import sys

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.ws import Client as WsClient

from agent.feishu_test.config import get_credentials


def _parse_text_content(raw: str | None) -> str:
    """Extract plain text from Feishu message content JSON."""
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        return obj.get("text", raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _make_send_client(app_id: str, app_secret: str) -> lark.Client:
    """Build an HTTP client for sending replies (used in echo mode)."""
    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )


def _send_reply(http_client: lark.Client, chat_id: str, text: str) -> None:
    """Send a text reply to *chat_id*.  Prints result; never raises."""
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
    )

    body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .msg_type("text")
        .content(json.dumps({"text": text}))
        .build()
    )
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(body)
        .build()
    )
    resp = http_client.im.v1.message.create(request)
    if resp.success():
        print(f"[feishu] echo reply sent  message_id: {resp.data.message_id}")
    else:
        print(f"[feishu] echo reply failed  code: {resp.code}  msg: {resp.msg}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu message receiver (Phase 1)")
    parser.add_argument(
        "--echo",
        action="store_true",
        help="Auto-reply '收到：<text>' to every received text message",
    )
    args = parser.parse_args()

    # --- load credentials ---
    try:
        app_id, app_secret = get_credentials()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    echo_mode = args.echo
    http_client = _make_send_client(app_id, app_secret) if echo_mode else None

    # --- event handler ---
    def on_message(data: P2ImMessageReceiveV1) -> None:
        event = data.event
        if event is None or event.message is None:
            return

        msg = event.message
        sender = event.sender
        sender_open_id = ""
        if sender and sender.sender_id:
            sender_open_id = sender.sender_id.open_id or ""

        message_type = msg.message_type or ""

        print("[feishu] message received")
        print(f"sender_open_id: {sender_open_id}")
        print(f"message_id: {msg.message_id}")
        print(f"chat_id: {msg.chat_id}")
        print(f"chat_type: {msg.chat_type}")
        print(f"message_type: {message_type}")

        if message_type == "text":
            text = _parse_text_content(msg.content)
            print(f"text: {text}")

            if echo_mode and http_client and msg.chat_id:
                _send_reply(http_client, msg.chat_id, f"收到：{text}")
        else:
            print(f"[feishu] unsupported message type: {message_type}")

        print()  # blank line separator

    # --- build event handler ---
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    # --- start WebSocket long connection ---
    ws_client = WsClient(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    print("[feishu] starting long connection …")
    print("[feishu] press Ctrl+C to stop")

    try:
        ws_client.start()
    except KeyboardInterrupt:
        print("\n[feishu] stopped")
    except Exception as exc:
        print(f"[feishu] connection error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
