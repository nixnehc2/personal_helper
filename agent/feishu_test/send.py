"""Feishu Phase 1 — send a text message to a user.

Usage:
    python -m agent.feishu_test.send --open-id ou_xxxxx
"""
import argparse
import json
import sys

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from agent.feishu_test.config import get_credentials

HELLO_TEXT = "Hello from personal_ai_helper"


def send_hello(client: lark.Client, open_id: str) -> None:
    """Send HELLO_TEXT to *open_id* and print the result."""
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(open_id)
        .msg_type("text")
        .content(json.dumps({"text": HELLO_TEXT}))
        .build()
    )
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(body)
        .build()
    )

    resp = client.im.v1.message.create(request)
    if resp.success():
        print("[feishu] send success")
        print(f"message_id: {resp.data.message_id}")
    else:
        print("[feishu] send failed")
        print(f"code: {resp.code}")
        print(f"message: {resp.msg}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a test message via Feishu bot")
    parser.add_argument(
        "--open-id",
        required=True,
        help="Target user's open_id (e.g. ou_xxxxx)",
    )
    args = parser.parse_args()

    # --- load credentials ---
    try:
        app_id, app_secret = get_credentials()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    client = (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )

    send_hello(client, args.open_id)


if __name__ == "__main__":
    main()
