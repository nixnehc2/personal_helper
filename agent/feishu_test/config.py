"""Load Feishu credentials from config.local.json or environment variables.

Priority: environment variables > config.local.json.
App Secret is never logged or returned in error messages.
"""
import json
import os
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.local.json"


def _load_file_config() -> dict:
    """Read the shared config.local.json (same file used by agent.llm)."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, ValueError, UnicodeError):
        return {}


def get_credentials() -> tuple[str, str]:
    """Return (app_id, app_secret).

    Raises ValueError with a helpful message if either value is missing.
    The secret itself is never included in the error text.
    """
    file_cfg = _load_file_config()

    app_id = os.environ.get("FEISHU_APP_ID") or file_cfg.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or file_cfg.get("FEISHU_APP_SECRET")

    if not app_id:
        raise ValueError(
            "缺少 FEISHU_APP_ID，请先配置环境变量或在 config.local.json 中设置。"
        )
    if not app_secret:
        raise ValueError(
            "缺少 FEISHU_APP_SECRET，请先配置环境变量或在 config.local.json 中设置。"
        )

    return app_id, app_secret
