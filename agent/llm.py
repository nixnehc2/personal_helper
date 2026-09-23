"""Anthropic Messages transport using only the Python standard library."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def load_config(path=None):
    path = Path(path) if path is not None else Path(__file__).resolve().parent.parent / "config.local.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError):
        raise ValueError("config.local.json must contain valid UTF-8 JSON") from None
    allowed = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "AGENT_TIMEOUT_SECONDS"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("config.local.json contains unsupported configuration fields")
    if any(not isinstance(value, str) or not value.strip() for value in config.values()):
        raise ValueError("config.local.json values must be non-empty strings")
    return config


class Client:
    def __init__(self, token, config=None):
        settings = dict(os.environ)
        settings.update(config or {})
        self.token = token
        base = settings.get("ANTHROPIC_BASE_URL", "https://api.zinyy.tech").rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("ANTHROPIC_BASE_URL must be a plain HTTPS base URL")
        self.url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        self.model = settings.get("ANTHROPIC_MODEL", "mimo-v2.5-pro")
        try:
            self.timeout = float(settings.get("AGENT_TIMEOUT_SECONDS", "120"))
        except ValueError:
            raise ValueError("AGENT_TIMEOUT_SECONDS must be numeric") from None
        if not 0 < self.timeout <= 600:
            raise ValueError("AGENT_TIMEOUT_SECONDS must be between 0 and 600")
        self.opener = urllib.request.build_opener(NoRedirect())

    def complete(self, system, messages, tools):
        payload = dict(model=self.model, max_tokens=4096, system=system, messages=messages, tools=tools)
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01",
                   "Authorization": "Bearer " + self.token}
        request = urllib.request.Request(self.url, json.dumps(payload).encode(), headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("API response too large")
                result = json.loads(raw)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"LLM HTTP {error.code}; check endpoint, token and model") from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("LLM network error or timeout; no automatic retry") from None
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise RuntimeError("API did not return Anthropic Messages content")
        return result
