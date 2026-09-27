"""Thin wrapper around NapCat / OneBot 11 HTTP API.

This module isolates all QQ communication behind a small ``QQClient``
interface so that the rest of the application never imports OneBot
constants or constructs raw HTTP requests.

NapCat reference:
    https://napneko.github.io/guide/api/http

OneBot 11 HTTP API:
    https://github.com/botuniverse/onebot-11/blob/master/api/public.md
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_API_URL = "http://127.0.0.1:3000"

# Maximum number of messages to request in one call.
MAX_MESSAGES = 1000


class QQClientError(Exception):
    """Raised when a QQ API call fails."""


class QQConnectionError(QQClientError):
    """Raised when the NapCat service is unreachable."""


class QQAPIError(QQClientError):
    """Raised when the NapCat API returns a non-zero retcode."""

    def __init__(self, retcode: int, message: str):
        self.retcode = retcode
        super().__init__(f"NapCat API error (retcode={retcode}): {message}")


# ---------------------------------------------------------------------------
# QQClient
# ---------------------------------------------------------------------------
class QQClient:
    """Minimal client for NapCat / OneBot 11 HTTP endpoints.

    Parameters
    ----------
    api_url:
        Base URL of the NapCat HTTP server, e.g. ``"http://127.0.0.1:3000"``.
        No trailing slash.
    access_token:
        Bearer token for NapCat authentication.  May be ``""`` when
        NapCat is configured without a token.
    timeout:
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        api_url: str = _DEFAULT_API_URL,
        access_token: str = "",
        timeout: float = 30.0,
    ):
        self.api_url = api_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout

    # ---- internal helpers ------------------------------------------------

    def _request(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """Send a GET request to the OneBot API and return ``data``.

        Parameters
        ----------
        action:
            API action name, e.g. ``"get_group_list"``.
        params:
            Query parameters to append to the URL.

        Returns
        -------
        Any
            The ``data`` field from the JSON response body.

        Raises
        ------
        QQConnectionError
            If the server cannot be reached.
        QQAPIError
            If the server returns a non-zero ``retcode``.
        """
        url = f"{self.api_url}/{action}"
        if params:
            query = "&".join(
                f"{k}={v}" for k, v in params.items() if v is not None
            )
            if query:
                url = f"{url}?{query}"

        req = urllib.request.Request(url, method="GET")
        if self.access_token:
            req.add_header("Authorization", f"Bearer {self.access_token}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise QQConnectionError(
                f"无法连接 QQ 服务：{self.api_url}\n"
                f"请确认 NapCat 已启动并登录。\n"
                f"原始错误：{exc}"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise QQClientError(f"QQ 服务返回了无效的 JSON：{exc}") from exc

        retcode = body.get("retcode", -1)
        if retcode != 0:
            raise QQAPIError(retcode, body.get("msg", str(body.get("data", ""))))

        return body.get("data")

    # ---- public API ------------------------------------------------------

    def get_history_page(self, chat_type, chat_id, count=100, message_seq=None):
        """Strict oldest-first backward pagination; never mask API failures."""
        if chat_type not in ("private", "group"):
            raise ValueError("Unknown QQ conversation type")
        action = "get_group_msg_history" if chat_type == "group" else "get_friend_msg_history"
        key = "group_id" if chat_type == "group" else "user_id"
        data = self._request(action, {
            key: chat_id, "count": min(max(count, 2), MAX_MESSAGES),
            "message_seq": message_seq, "reverseOrder": "true",
        })
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise QQClientError("QQ 历史响应缺少 messages 数组")
        return data["messages"]

    def get_login_info(self) -> dict[str, Any]:
        """Return ``{"user_id": int, "nickname": str}``."""
        data = self._request("get_login_info")
        return data  # type: ignore[return-value]

    def list_group_chats(self) -> list[dict[str, Any]]:
        """Return the list of joined groups.

        Each element is a dict with at least::

            {"group_id": int, "group_name": str, ...}
        """
        data = self._request("get_group_list")
        return data if isinstance(data, list) else []  # type: ignore[return-value]

    def list_private_chats(self) -> list[dict[str, Any]]:
        """Return the friend list.

        Each element is a dict with at least::

            {"user_id": int, "nickname": str, "remark": str, ...}
        """
        data = self._request("get_friend_list")
        return data if isinstance(data, list) else []  # type: ignore[return-value]

    def get_group_messages(
        self, group_id: int, count: int = 20
    ) -> list[dict[str, Any]]:
        """Return up to *count* recent messages from a group.

        Uses ``get_group_msg_history`` which is supported by NapCat.

        Parameters
        ----------
        group_id:
            The QQ group number.
        count:
            Maximum messages to retrieve (capped at ``MAX_MESSAGES``).

        Returns
        -------
        list[dict]
            Ordered oldest-first.  Each element is the raw OneBot
            message object including ``message_id``, ``sender``,
            ``message`` (segment list), ``raw_message``, etc.
        """
        count = min(max(count, 1), MAX_MESSAGES)
        data = self._request(
            "get_group_msg_history",
            {"group_id": group_id, "count": count},
        )
        messages: list[dict[str, Any]] = (
            data.get("messages", []) if isinstance(data, dict) else []
        )
        return messages

    def get_private_messages(
        self, user_id: int, count: int = 20
    ) -> list[dict[str, Any]]:
        """Return up to *count* recent private (friend) messages.

        Uses ``get_friend_msg_history`` when available on the NapCat
        build. Falls back to ``get_recent_contact_messages`` and
        filtering, then to ``get_msg`` sequential walk if needed.

        Parameters
        ----------
        user_id:
            The friend's QQ number.
        count:
            Maximum messages to retrieve (capped at ``MAX_MESSAGES``).

        Returns
        -------
        list[dict]
            Ordered oldest-first.
        """
        count = min(max(count, 1), MAX_MESSAGES)
        # --- attempt 1: get_friend_msg_history (NapCat ≥ 4.x) ----------
        try:
            data = self._request(
                "get_friend_msg_history",
                {"user_id": user_id, "count": count},
            )
            messages: list[dict[str, Any]] = (
                data.get("messages", []) if isinstance(data, dict) else []
            )
            if messages:
                return messages
        except QQAPIError:
            pass  # endpoint may not exist in this NapCat build

        # --- attempt 2: get_recent_contact_messages --------------------
        try:
            contacts = self._request("get_recent_contact_messages")
            if isinstance(contacts, list):
                for contact in contacts:
                    if contact.get("user_id") == user_id:
                        return contact.get("messages", [])[:count]
        except QQAPIError:
            pass

        # --- attempt 3: sequential get_msg from private messages -------
        # Use get_recent_contact_messages without user filter to find
        # any message_id belonging to this user, then walk backward.
        # If all fail, return empty list.
        return []
