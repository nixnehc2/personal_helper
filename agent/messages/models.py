"""Unified Message data model.

This is a minimal, source-agnostic representation of a message.
The ``content`` dict holds source-specific fields; its schema is
not mandated by this model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    """Minimal unified message representation.

    Attributes
    ----------
    id:
        Local unified message ID.
    source:
        Origin identifier, e.g. ``"email"``.
    time:
        ISO 8601 string with timezone offset when the timestamp can be
        reliably parsed; ``None`` otherwise.
    content:
        Source-specific payload.  For email this preserves every field
        the existing pipeline depends on (host, account, folder, ...).
    """

    id: int
    source: str
    time: str | None
    content: dict[str, Any] = field(default_factory=dict)
