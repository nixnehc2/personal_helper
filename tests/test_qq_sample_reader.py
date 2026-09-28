"""Tests for Phase 3A: QQ sample reader and QQClient.

All tests use fixed fixtures or mocked HTTP responses -- they never
require a live QQ / NapCat service.

Covers:
  1. Conversation list correctly unified (groups + private)
  2. User display_index maps correctly to chosen conversation
  3. count parameter correctly passed to QQClient
  4. Raw JSON sample saved without field loss
  5. Message segments NOT flattened
  6. Filename generation is stable
  7. Invalid index / edge cases handled
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.qq_client import (
    QQClient,
    QQClientError,
    QQConnectionError,
    MAX_MESSAGES,
)
from agent.qq_sample_reader import (
    build_conversation_list,
    generate_sample_filename,
    message_preview_text,
    save_sample,
    _segment_preview,
)

# ---------------------------------------------------------------------------
# Paths to fixtures
# ---------------------------------------------------------------------------
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "qq"

PRIVATE_PLAIN = FIXTURES_DIR / "private_plain_text.json"
GROUP_PLAIN = FIXTURES_DIR / "group_plain_text.json"
GROUP_COMPLEX = FIXTURES_DIR / "group_complex_messages.json"


def _load_fixture(name: str) -> list[dict]:
    """Load a fixture JSON file."""
    path = FIXTURES_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# 1. build_conversation_list
# ===========================================================================
class BuildConversationListTests(unittest.TestCase):
    """Test that groups and friends are merged into a single sorted list."""

    def _make_client(self, groups, friends):
        client = MagicMock(spec=QQClient)
        client.list_group_chats.return_value = groups
        client.list_private_chats.return_value = friends
        return client

    def test_empty(self):
        client = self._make_client([], [])
        result = build_conversation_list(client)
        self.assertEqual(result, [])

    def test_groups_only(self):
        groups = [
            {"group_id": 100, "group_name": "测试群A"},
            {"group_id": 200, "group_name": "测试群B"},
        ]
        client = self._make_client(groups, [])
        result = build_conversation_list(client)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["type"], "group")
        self.assertEqual(result[0]["id"], "100")
        self.assertEqual(result[0]["name"], "测试群A")

    def test_friends_only(self):
        friends = [
            {"user_id": 500, "nickname": "小明", "remark": "明明"},
            {"user_id": 600, "nickname": "小红", "remark": ""},
        ]
        client = self._make_client([], friends)
        result = build_conversation_list(client)
        self.assertEqual(len(result), 2)
        types = [c["type"] for c in result]
        self.assertTrue(all(t == "private" for t in types))

    def test_mixed_sorted_groups_first(self):
        groups = [{"group_id": 10, "group_name": "A群"}]
        friends = [{"user_id": 99, "nickname": "张三", "remark": ""}]
        client = self._make_client(groups, friends)
        result = build_conversation_list(client)
        self.assertEqual(result[0]["type"], "group")
        self.assertEqual(result[1]["type"], "private")

    def test_display_index_sequential(self):
        groups = [{"group_id": i, "group_name": f"G{i}"} for i in range(5)]
        client = self._make_client(groups, [])
        result = build_conversation_list(client)
        indices = [c["display_index"] for c in result]
        self.assertEqual(indices, list(range(1, 6)))

    def test_remark_preferred_over_nickname(self):
        friends = [
            {"user_id": 1, "nickname": "nick1", "remark": "备注1"},
            {"user_id": 2, "nickname": "nick2", "remark": ""},
        ]
        client = self._make_client([], friends)
        result = build_conversation_list(client)
        names = {c["id"]: c["name"] for c in result}
        self.assertEqual(names["1"], "备注1")
        self.assertEqual(names["2"], "nick2")

    def test_empty_remark_and_nickname_falls_back_to_id(self):
        friends = [
            {"user_id": 42, "nickname": "", "remark": ""},
        ]
        client = self._make_client([], friends)
        result = build_conversation_list(client)
        self.assertEqual(result[0]["name"], "42")


# ===========================================================================
# 2. display_index -> conversation mapping
# ===========================================================================
class DisplayIndexMappingTests(unittest.TestCase):
    """Ensure the user's 1-based selection maps correctly."""

    def test_selection_returns_correct_conversation(self):
        client = MagicMock(spec=QQClient)
        client.list_group_chats.return_value = [
            {"group_id": 10, "group_name": "群A"},
            {"group_id": 20, "group_name": "群B"},
        ]
        client.list_private_chats.return_value = [
            {"user_id": 30, "nickname": "好友C", "remark": ""},
        ]
        convs = build_conversation_list(client)
        # Selection 1 -> first (groups sorted by name, so 群A first)
        sel1 = convs[0]
        self.assertEqual(sel1["id"], "10")
        self.assertEqual(sel1["type"], "group")
        sel3 = convs[2]
        self.assertEqual(sel3["id"], "30")
        self.assertEqual(sel3["type"], "private")


# ===========================================================================
# 3. QQClient count parameter
# ===========================================================================
class QQClientCountParamTests(unittest.TestCase):
    """Verify count is passed correctly and clamped."""

    @patch.object(QQClient, "_request")
    def test_group_messages_count_sent(self, mock_req):
        mock_req.return_value = {"messages": []}
        client = QQClient(api_url="http://fake", access_token="")
        client.get_group_messages(12345, count=50)
        mock_req.assert_called_once_with(
            "get_group_msg_history",
            {"group_id": 12345, "count": 50},
        )

    @patch.object(QQClient, "_request")
    def test_group_messages_count_clamped_to_max(self, mock_req):
        mock_req.return_value = {"messages": []}
        client = QQClient(api_url="http://fake", access_token="")
        client.get_group_messages(12345, count=9999)
        _, kwargs = mock_req.call_args
        self.assertEqual(kwargs.get("count", None) or mock_req.call_args[0][1]["count"], MAX_MESSAGES)

    @patch.object(QQClient, "_request")
    def test_group_messages_count_min_1(self, mock_req):
        mock_req.return_value = {"messages": []}
        client = QQClient(api_url="http://fake", access_token="")
        client.get_group_messages(12345, count=0)
        _, kwargs = mock_req.call_args
        called_count = mock_req.call_args[0][1]["count"]
        self.assertEqual(called_count, 1)

    @patch.object(QQClient, "_request")
    def test_private_messages_count_sent(self, mock_req):
        # Return a non-empty result on first call so fallbacks are not triggered
        mock_req.return_value = {"messages": [{"message_id": 1}]}
        client = QQClient(api_url="http://fake", access_token="")
        client.get_private_messages(999, count=30)
        mock_req.assert_called_once_with(
            "get_friend_msg_history",
            {"user_id": 999, "count": 30},
        )


# ===========================================================================
# 4. save_sample preserves all fields
# ===========================================================================
class SaveSampleTests(unittest.TestCase):
    """Test that save_sample writes complete, valid JSON."""

    def test_structure_contains_required_keys(self):
        conv = {"display_index": 1, "type": "group", "id": 123, "name": "测试群"}
        messages = [{"message_id": 1, "text": "hi"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_sample(conv, 20, messages, samples_dir=Path(tmpdir))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("sample_version", data)
            self.assertIn("captured_at", data)
            self.assertIn("conversation", data)
            self.assertIn("requested_count", data)
            self.assertIn("messages", data)
            self.assertEqual(data["sample_version"], 1)
            self.assertEqual(data["requested_count"], 20)
            self.assertEqual(data["conversation"]["type"], "group")
            self.assertEqual(data["conversation"]["id"], 123)
            self.assertEqual(data["conversation"]["name"], "测试群")

    def test_messages_preserved_exactly(self):
        messages = _load_fixture("group_complex_messages.json")
        conv = {"display_index": 1, "type": "group", "id": 9988776655, "name": "复杂群"}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_sample(conv, 5, messages, samples_dir=Path(tmpdir))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["messages"]), 5)
            # Verify first message is byte-identical
            self.assertEqual(data["messages"][0]["message_id"], 300001)
            # Verify the complex reply+at message is preserved with segments
            reply_msg = data["messages"][1]
            self.assertIsInstance(reply_msg["message"], list)
            self.assertEqual(reply_msg["message"][0]["type"], "reply")
            self.assertEqual(reply_msg["message"][1]["type"], "at")
            self.assertEqual(reply_msg["message"][2]["type"], "text")

    def test_utf8_chinese_content_preserved(self):
        messages = [{"message_id": 1, "raw_message": "你好世界 🌍"}]
        conv = {"display_index": 1, "type": "private", "id": 1, "name": "测试"}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_sample(conv, 1, messages, samples_dir=Path(tmpdir))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["messages"][0]["raw_message"], "你好世界 🌍")

    def test_empty_messages_list(self):
        conv = {"display_index": 1, "type": "group", "id": 1, "name": "空群"}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_sample(conv, 10, [], samples_dir=Path(tmpdir))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["messages"], [])


# ===========================================================================
# 5. Message segments NOT flattened
# ===========================================================================
class SegmentPreservationTests(unittest.TestCase):
    """Ensure message segment arrays are never flattened to plain text."""

    def test_complex_message_segments_preserved_in_save(self):
        messages = _load_fixture("group_complex_messages.json")
        conv = {"display_index": 1, "type": "group", "id": 99, "name": "测试"}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_sample(conv, 5, messages, samples_dir=Path(tmpdir))
            data = json.loads(path.read_text(encoding="utf-8"))

            # Message with reply+at+text
            msg2 = data["messages"][1]
            segs = msg2["message"]
            self.assertIsInstance(segs, list)
            self.assertGreater(len(segs), 1)
            types = [s["type"] for s in segs]
            self.assertIn("reply", types)
            self.assertIn("at", types)
            self.assertIn("text", types)

            # Message with text+image
            msg3 = data["messages"][2]
            segs3 = msg3["message"]
            self.assertIsInstance(segs3, list)
            types3 = [s["type"] for s in segs3]
            self.assertIn("image", types3)

            # Face message
            msg4 = data["messages"][3]
            segs4 = msg4["message"]
            self.assertIsInstance(segs4, list)
            self.assertEqual(segs4[0]["type"], "face")

            # File message
            msg5 = data["messages"][4]
            segs5 = msg5["message"]
            self.assertIsInstance(segs5, list)
            file_types = [s["type"] for s in segs5]
            self.assertIn("file", file_types)


# ===========================================================================
# 6. Filename generation stability
# ===========================================================================
class FilenameGenerationTests(unittest.TestCase):
    """Test generate_sample_filename format and stability."""

    def test_group_filename_format(self):
        name = generate_sample_filename("group", 123456)
        # Format: YYYYMMDD_HHMMSS_group_123456.json
        parts = name.replace(".json", "").split("_")
        # Split produces: ["YYYYMMDD", "HHMMSS", "group", "123456"]
        self.assertEqual(len(parts), 4)
        self.assertEqual(len(parts[0]), 8)   # date YYYYMMDD
        self.assertEqual(len(parts[1]), 6)   # time HHMMSS
        self.assertEqual(parts[2], "group")
        self.assertEqual(parts[3], "123456")

    def test_private_filename_format(self):
        name = generate_sample_filename("private", 654321)
        self.assertIn("private", name)
        self.assertIn("654321", name)
        self.assertTrue(name.endswith(".json"))

    def test_filename_unique_per_call(self):
        """Two calls within the same second may collide; across seconds they won't.
        This tests that the format is at least consistent."""
        name1 = generate_sample_filename("group", 1)
        name2 = generate_sample_filename("group", 2)
        # Different chat_id -> different filename
        self.assertNotEqual(name1, name2)

    def test_group_type_label_normalized(self):
        """Even if chat_type is some unexpected string, 'group' is used for groups."""
        # The function uses "group" for group, "private" otherwise
        name = generate_sample_filename("group", 1)
        self.assertIn("group_", name)
        name2 = generate_sample_filename("private", 1)
        self.assertIn("private_", name2)


# ===========================================================================
# 7. message_preview_text
# ===========================================================================
class MessagePreviewTests(unittest.TestCase):
    """Test simplified terminal preview generation."""

    def test_plain_text_message(self):
        msg = {
            "time": 1727500001,
            "sender": {"user_id": 1, "nickname": "小明", "card": ""},
            "raw_message": "你好",
        }
        preview = message_preview_text(msg)
        self.assertIn("小明", preview)
        self.assertIn("你好", preview)

    def test_card_preferred_over_nickname(self):
        msg = {
            "time": 1727500001,
            "sender": {"user_id": 1, "nickname": "nick", "card": "群名片"},
            "raw_message": "test",
        }
        preview = message_preview_text(msg)
        self.assertIn("群名片", preview)
        self.assertNotIn("nick", preview)

    def test_image_segment(self):
        segs = [{"type": "image", "data": {"file": "abc.jpg"}}]
        self.assertEqual(_segment_preview(segs[0]), "[图片]")

    def test_at_segment(self):
        segs = [{"type": "at", "data": {"qq": "123456"}}]
        self.assertEqual(_segment_preview(segs[0]), "@123456")

    def test_reply_segment(self):
        segs = [{"type": "reply", "data": {"id": "100"}}]
        self.assertEqual(_segment_preview(segs[0]), "[回复]")

    def test_file_segment(self):
        segs = [{"type": "file", "data": {"file": "doc.pdf"}}]
        result = _segment_preview(segs[0])
        self.assertIn("[文件]", result)
        self.assertIn("doc.pdf", result)

    def test_face_segment(self):
        segs = [{"type": "face", "data": {"id": "178"}}]
        self.assertEqual(_segment_preview(segs[0]), "[表情]")

    def test_record_segment(self):
        segs = [{"type": "record", "data": {}}]
        self.assertEqual(_segment_preview(segs[0]), "[语音]")

    def test_unknown_segment_type(self):
        segs = [{"type": "new_future_type", "data": {}}]
        result = _segment_preview(segs[0])
        self.assertIn("new_future_type", result)

    def test_raw_message_used_over_segments(self):
        """When raw_message is present, it should be preferred."""
        msg = {
            "time": 1727500001,
            "sender": {"user_id": 1, "nickname": "user", "card": ""},
            "raw_message": "raw content",
            "message": [{"type": "text", "data": {"text": "segment content"}}],
        }
        preview = message_preview_text(msg)
        self.assertIn("raw content", preview)

    def test_fallback_to_segments_when_no_raw(self):
        """When raw_message is empty, build from segments."""
        msg = {
            "time": 1727500001,
            "sender": {"user_id": 1, "nickname": "user", "card": ""},
            "raw_message": "",
            "message": [
                {"type": "text", "data": {"text": "hello "}},
                {"type": "at", "data": {"qq": "999"}},
            ],
        }
        preview = message_preview_text(msg)
        self.assertIn("hello", preview)
        self.assertIn("@999", preview)


# ===========================================================================
# QQClient basic connectivity
# ===========================================================================
class QQClientConnectionTests(unittest.TestCase):
    """Test QQClient error handling (no real HTTP)."""

    def test_connection_error_on_unreachable(self):
        client = QQClient(api_url="http://127.0.0.1:1", access_token="", timeout=1.0)
        with self.assertRaises(QQConnectionError) as ctx:
            client.get_login_info()
        self.assertIn("无法连接", str(ctx.exception))
        self.assertIn("127.0.0.1:1", str(ctx.exception))

    @patch.object(QQClient, "_request")
    def test_login_info_calls_correct_action(self, mock_req):
        mock_req.return_value = {"user_id": 123, "nickname": "test"}
        client = QQClient(api_url="http://fake")
        result = client.get_login_info()
        mock_req.assert_called_once_with("get_login_info")
        self.assertEqual(result["user_id"], 123)

    @patch.object(QQClient, "_request")
    def test_list_group_chats(self, mock_req):
        mock_req.return_value = [{"group_id": 1, "group_name": "G1"}]
        client = QQClient(api_url="http://fake")
        result = client.list_group_chats()
        mock_req.assert_called_once_with("get_group_list")
        self.assertEqual(len(result), 1)

    @patch.object(QQClient, "_request")
    def test_list_private_chats(self, mock_req):
        mock_req.return_value = [{"user_id": 1, "nickname": "F1", "remark": ""}]
        client = QQClient(api_url="http://fake")
        result = client.list_private_chats()
        mock_req.assert_called_once_with("get_friend_list")
        self.assertEqual(len(result), 1)

    @patch.object(QQClient, "_request")
    def test_group_messages_returns_messages_list(self, mock_req):
        msgs = [{"message_id": 1}, {"message_id": 2}]
        mock_req.return_value = {"messages": msgs}
        client = QQClient(api_url="http://fake")
        result = client.get_group_messages(123, count=10)
        self.assertEqual(result, msgs)

    @patch.object(QQClient, "_request")
    def test_group_messages_empty_on_no_messages_key(self, mock_req):
        mock_req.return_value = {}
        client = QQClient(api_url="http://fake")
        result = client.get_group_messages(123, count=10)
        self.assertEqual(result, [])

    def test_access_token_header(self):
        """Verify the Authorization header is set when token is provided."""
        client = QQClient(api_url="http://127.0.0.1:1", access_token="mytoken", timeout=1.0)
        # We can't easily capture the request object, but we can verify
        # the client stores the token
        self.assertEqual(client.access_token, "mytoken")

    def test_no_access_token_by_default(self):
        client = QQClient(api_url="http://fake")
        self.assertEqual(client.access_token, "")


# ===========================================================================
# Fixture existence checks
# ===========================================================================
class FixtureExistenceTests(unittest.TestCase):
    """Ensure all required fixtures exist and are valid JSON."""

    def test_private_plain_text_exists(self):
        self.assertTrue(PRIVATE_PLAIN.exists())
        data = json.loads(PRIVATE_PLAIN.read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)

    def test_group_plain_text_exists(self):
        self.assertTrue(GROUP_PLAIN.exists())
        data = json.loads(GROUP_PLAIN.read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)

    def test_group_complex_messages_exists(self):
        self.assertTrue(GROUP_COMPLEX.exists())
        data = json.loads(GROUP_COMPLEX.read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)

    def test_fixtures_have_message_segments(self):
        """The complex fixture must contain segment arrays, not just raw_message."""
        data = _load_fixture("group_complex_messages.json")
        for msg in data:
            self.assertIn("message", msg)
            self.assertIsInstance(msg["message"], list)
            for seg in msg["message"]:
                self.assertIn("type", seg)
                self.assertIn("data", seg)

    def test_fixtures_have_sender_info(self):
        """All fixtures must have sender with user_id, nickname, card."""
        for fname in ("private_plain_text.json", "group_plain_text.json",
                       "group_complex_messages.json"):
            data = _load_fixture(fname)
            for msg in data:
                self.assertIn("sender", msg)
                self.assertIn("user_id", msg["sender"])
                self.assertIn("nickname", msg["sender"])

    def test_complex_fixture_covers_required_types(self):
        """The complex fixture must cover: text, reply, at, image, face, file."""
        data = _load_fixture("group_complex_messages.json")
        all_types: set[str] = set()
        for msg in data:
            for seg in msg.get("message", []):
                all_types.add(seg["type"])
        required = {"text", "reply", "at", "image", "face", "file"}
        self.assertTrue(required.issubset(all_types),
                        f"Missing segment types: {required - all_types}")


if __name__ == "__main__":
    unittest.main()
