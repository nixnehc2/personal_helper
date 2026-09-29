"""Tests for post-sync auto import (agent.messages.auto_import)."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    """Redirect STATE_PATH to a temp file for every test."""
    state_path = tmp_path / "auto_import_state.json"
    monkeypatch.setattr(
        "agent.messages.auto_import.STATE_PATH", state_path
    )
    return state_path


def _make_message(mid, source="email", imported=False):
    msg = MagicMock()
    msg.id = mid
    msg.imported = imported
    msg.source = source
    return msg


# ── baseline ──

class TestBaseline:
    def test_first_run_establishes_baseline_email(self, _tmp_state):
        """First run sets cursor to current max without importing."""
        from agent.messages import auto_import as mod

        with patch.object(mod, "_email_max_id", return_value=20), \
             patch.object(mod, "_qq_max_rowid", return_value=0):
            result = mod.run_post_sync_auto_import(MagicMock(), MagicMock())

        assert result["baseline"] is True
        state = json.loads(_tmp_state.read_text(encoding="utf-8"))
        assert state["email"]["cursor"] == 20
        assert state["qq"]["cursor"] == 0

    def test_baseline_does_not_import_existing_messages(self, _tmp_state):
        """Baseline should not call import_message for existing messages."""
        from agent.messages import auto_import as mod

        with patch.object(mod, "_email_max_id", return_value=5), \
             patch.object(mod, "_qq_max_rowid", return_value=3), \
             patch("agent.messages.importing.import_message") as mock_import:
            result = mod.run_post_sync_auto_import(MagicMock(), MagicMock())

        mock_import.assert_not_called()
        assert result["baseline"] is True


# ── new messages after baseline ──

class TestNewMessages:
    def test_imports_new_messages_after_baseline(self, _tmp_state):
        """After baseline, new messages are imported via import_message."""
        from agent.messages import auto_import as mod

        # Set up baseline state
        _tmp_state.write_text(json.dumps({"email": {"cursor": 10}, "qq": {"cursor": 0}}), encoding="utf-8")

        new_msg = _make_message(15)
        with patch.object(mod, "_email_messages_after", return_value=[(new_msg, 15)]), \
             patch.object(mod, "_qq_messages_after", return_value=[]), \
             patch("agent.messages.importing.import_message", return_value={"status": "processed"}) as mock_import:
            result = mod.run_post_sync_auto_import(MagicMock(), MagicMock())

        mock_import.assert_called_once()
        assert result["processed"] >= 1

    def test_old_imported_messages_not_processed(self, _tmp_state):
        """Messages with imported=True are skipped and cursor advances."""
        from agent.messages import auto_import as mod

        _tmp_state.write_text(json.dumps({"email": {"cursor": 10}, "qq": {"cursor": 0}}), encoding="utf-8")

        already_msg = _make_message(12, imported=True)
        with patch.object(mod, "_email_messages_after", return_value=[(already_msg, 12)]), \
             patch.object(mod, "_qq_messages_after", return_value=[]), \
             patch("agent.messages.importing.import_message") as mock_import:
            result = mod.run_post_sync_auto_import(MagicMock(), MagicMock())

        mock_import.assert_not_called()
        state = json.loads(_tmp_state.read_text(encoding="utf-8"))
        assert state["email"]["cursor"] == 12


# ── failure stops processing ──

class TestFailureStops:
    def test_failure_stops_cursor_at_last_success(self, _tmp_state):
        """On failure, cursor stays at last successful import."""
        from agent.messages import auto_import as mod

        _tmp_state.write_text(json.dumps({"email": {"cursor": 10}, "qq": {"cursor": 0}}), encoding="utf-8")

        msg1 = _make_message(11)
        msg2 = _make_message(12)
        call_count = {"n": 0}
        def side_effect(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"status": "processed"}
            raise RuntimeError("boom")

        with patch.object(mod, "_email_messages_after", return_value=[(msg1, 11), (msg2, 12)]), \
             patch.object(mod, "_qq_messages_after", return_value=[]), \
             patch("agent.messages.importing.import_message", side_effect=side_effect):
            result = mod.run_post_sync_auto_import(MagicMock(), MagicMock())

        state = json.loads(_tmp_state.read_text(encoding="utf-8"))
        assert state["email"]["cursor"] == 11  # stopped before failing msg

    def test_retry_on_next_run(self, _tmp_state):
        """After failure, next run retries from cursor position."""
        from agent.messages import auto_import as mod

        _tmp_state.write_text(json.dumps({"email": {"cursor": 11}, "qq": {"cursor": 0}}), encoding="utf-8")

        msg = _make_message(12)
        with patch.object(mod, "_email_messages_after", return_value=[(msg, 12)]), \
             patch.object(mod, "_qq_messages_after", return_value=[]), \
             patch("agent.messages.importing.import_message", return_value={"status": "processed"}) as mock_import:
            mod.run_post_sync_auto_import(MagicMock(), MagicMock())

        mock_import.assert_called_once()
        state = json.loads(_tmp_state.read_text(encoding="utf-8"))
        assert state["email"]["cursor"] == 12


# ── already_imported skipped ──

class TestAlreadyImported:
    def test_already_imported_skipped_and_cursor_advances(self, _tmp_state):
        """Messages returning already_imported are skipped; cursor advances."""
        from agent.messages import auto_import as mod

        _tmp_state.write_text(json.dumps({"email": {"cursor": 10}, "qq": {"cursor": 0}}), encoding="utf-8")

        msg = _make_message(11, imported=False)
        with patch.object(mod, "_email_messages_after", return_value=[(msg, 11)]), \
             patch.object(mod, "_qq_messages_after", return_value=[]), \
             patch("agent.messages.importing.import_message", return_value={"status": "already_imported"}):
            mod.run_post_sync_auto_import(MagicMock(), MagicMock())

        state = json.loads(_tmp_state.read_text(encoding="utf-8"))
        assert state["email"]["cursor"] == 11


# ── sources independent ──

class TestSourcesIndependent:
    def test_email_failure_does_not_block_qq(self, _tmp_state):
        """Email source failure does not prevent QQ from processing."""
        from agent.messages import auto_import as mod

        _tmp_state.write_text(json.dumps({"email": {"cursor": 10}, "qq": {"cursor": 5}}), encoding="utf-8")

        email_msg = _make_message(11)
        qq_msg = _make_message("qq-1", source="qq", imported=False)

        def side_effect_import(message_id, client, files, source=None, emit=None):
            if source == "email":
                raise RuntimeError("email boom")
            return {"status": "processed"}

        with patch.object(mod, "_email_messages_after", return_value=[(email_msg, 11)]), \
             patch.object(mod, "_qq_messages_after", return_value=[(qq_msg, 6)]), \
             patch("agent.messages.importing.import_message", side_effect=side_effect_import):
            result = mod.run_post_sync_auto_import(MagicMock(), MagicMock())

        assert result["results"]["email"]["failed"] >= 1
        assert result["results"]["qq"]["processed"] >= 1