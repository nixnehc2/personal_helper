"""Feishu dual-channel client for call_for_user.

Sends questions to the user via Feishu and receives answers through a
WebSocket long connection.  Each instance is designed to live for the
duration of a single call_for_user flow and be discarded afterwards.
"""
import json
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from .feishu_test.config import get_credentials


def _parse_text_content(raw):
    """Extract plain text from Feishu message content JSON."""
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        return obj.get("text", raw)
    except (json.JSONDecodeError, TypeError):
        return raw


class FeishuClient:
    """Feishu send/receive client for call_for_user dual-channel flow."""

    def __init__(self, request_manager):
        self._request_manager = request_manager
        self._http_client = None
        self._ws_client = None
        self._user_open_id = None
        self._running = False
        self._started = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Build HTTP + WebSocket clients and start the long connection."""
        from .llm import load_config
        cfg = load_config()
        app_id, app_secret = get_credentials()
        self._user_open_id = cfg.get("FEISHU_USER_OPEN_ID", "")

        self._http_client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

        def on_message(data):
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
            event = data.event
            if event is None or event.message is None:
                return
            self._handle_message(event)

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .build()
        )

        self._ws_client = lark.ws.Client(
            app_id=app_id,
            app_secret=app_secret,
            log_level=lark.LogLevel.WARNING,
            event_handler=handler,
        )

        self._running = True
        thread = threading.Thread(target=self._run_ws, daemon=True)
        thread.start()
        self._started.wait(timeout=10)

    def _run_ws(self):
        try:
            self._started.set()
            self._ws_client.start()
        except Exception as exc:
            print(f"[feishu] WebSocket error: {exc}")
        finally:
            self._running = False

    def stop(self):
        """Signal the client to stop (best-effort)."""
        self._running = False

    @property
    def client(self):
        return self._http_client

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------

    def _handle_message(self, event):
        msg = event.message
        sender = event.sender
        sender_open_id = ""
        if sender and sender.sender_id:
            sender_open_id = sender.sender_id.open_id or ""

        if self._user_open_id and sender_open_id != self._user_open_id:
            return

        message_type = msg.message_type or ""
        if message_type != "text":
            return

        text = _parse_text_content(msg.content).strip()
        if not text:
            return

        # Cancel command: find the latest waiting request for this chat
        if text in ("/cancel", "cancel", "取消"):
            request = self._find_waiting_request(msg.chat_id)
            if request is not None:
                self._request_manager.mark_cancelled(request["request_id"])
                self._send_text(msg.chat_id, "事件已终止(request_id=" + request["request_id"] + ")")
            else:
                self._send_text(msg.chat_id, "没有等待回答的请求")
            return

        # Answer: match to waiting request
        request = self._find_waiting_request(msg.chat_id)
        if request is None:
            self._send_text(msg.chat_id, "没有等待回答的请求。")
            return

        result = self._request_manager.submit_answer(
            request["request_id"], text, "feishu",
        )
        if result == "success":
            self._send_text(msg.chat_id, "收到回答，正在继续处理。")
        else:
            self._send_text(msg.chat_id, "请求已处理，本次回答无效。")

    def _find_waiting_request(self, chat_id):
        """Find the most recent waiting request matching *chat_id*."""
        with self._request_manager._connect() as db:
            row = db.execute(
                "SELECT * FROM user_requests "
                "WHERE status='waiting' AND feishu_chat_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
            return self._request_manager._row_to_dict(row)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_question(self, chat_id, text, request_id):
        """Send *text* to *chat_id* and return (message_id, chat_id)."""
        msg_id, cid = self._send_text(chat_id, text)
        if msg_id:
            self._request_manager.mark_feishu_info(request_id, msg_id, cid or chat_id)
        return msg_id, cid

    def send_invalidated(self, request_id):
        req = self._request_manager.get_request(request_id)
        if req and req.get("feishu_chat_id"):
            self._send_text(
                req["feishu_chat_id"],
                f"Agent 已退出，本次请求已失效。\n"
                f"问题：{req['prompt']}\n"
                f"请回复 /cancel 终止事件，或等待下次重试。",
            )

    def send_cancelled(self, request_id):
        req = self._request_manager.get_request(request_id)
        if req and req.get("feishu_chat_id"):
            self._send_text(req["feishu_chat_id"], "事件已终止。")

    def update_message(self, message_id, text):
        if not self._http_client or not message_id:
            return
        body = (
            PatchMessageRequestBody.builder()
            .content(json.dumps({"text": text}))
            .build()
        )
        request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        try:
            self._http_client.im.v1.message.patch(request)
        except Exception:
            pass

    def _send_text(self, receive_id, text):
        """Send a text message. Returns (message_id, chat_id) or (None, None)."""
        if not self._http_client:
            return None, None
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
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
        try:
            resp = self._http_client.im.v1.message.create(request)
            if resp.success():
                return resp.data.message_id, receive_id
            print(f"[feishu] send failed: code={resp.code} msg={resp.msg}")
        except Exception as exc:
            print(f"[feishu] send error: {exc}")
        return None, None
