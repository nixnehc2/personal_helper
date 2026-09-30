"""Tests for agent.feishu_test.config — credential loading."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.feishu_test.config import get_credentials, _load_file_config


class TestFeishuConfig(unittest.TestCase):
    """Verify credential loading from config.local.json and env vars."""

    def test_env_vars_override_file(self):
        """Environment variables take precedence over config.local.json."""
        with patch.dict(os.environ, {
            "FEISHU_APP_ID": "env_id",
            "FEISHU_APP_SECRET": "env_secret",
        }):
            app_id, app_secret = get_credentials()
            self.assertEqual(app_id, "env_id")
            self.assertEqual(app_secret, "env_secret")

    def test_missing_app_id_raises(self):
        """Missing FEISHU_APP_ID raises ValueError without leaking secret."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("agent.feishu_test.config._load_file_config", return_value={}):
                with self.assertRaises(ValueError) as ctx:
                    get_credentials()
                self.assertIn("FEISHU_APP_ID", str(ctx.exception))
                self.assertNotIn("secret", str(ctx.exception).lower())

    def test_missing_app_secret_raises(self):
        """Missing FEISHU_APP_SECRET raises ValueError without leaking it."""
        with patch.dict(os.environ, {"FEISHU_APP_ID": "x"}, clear=True):
            with patch("agent.feishu_test.config._load_file_config", return_value={}):
                with self.assertRaises(ValueError) as ctx:
                    get_credentials()
                self.assertIn("FEISHU_APP_SECRET", str(ctx.exception))

    def test_file_config_fallback(self):
        """Falls back to config.local.json when env vars are absent."""
        fake_cfg = {"FEISHU_APP_ID": "file_id", "FEISHU_APP_SECRET": "file_secret"}
        with patch.dict(os.environ, {}, clear=True):
            with patch("agent.feishu_test.config._load_file_config", return_value=fake_cfg):
                app_id, app_secret = get_credentials()
                self.assertEqual(app_id, "file_id")
                self.assertEqual(app_secret, "file_secret")

    def test_file_config_not_found_returns_empty(self):
        """Missing config.local.json returns empty dict (no crash)."""
        with patch("agent.feishu_test.config._CONFIG_PATH", Path("/nonexistent")):
            result = _load_file_config()
            self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
