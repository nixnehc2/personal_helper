"""Convert EmailIndex rows to unified Message objects.

This module is a pure adapter: it reads an email row dict and returns a
Message.  It never mutates the row or writes to the index.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any

from .models import Message

# Fields that belong to the email source itself and must be preserved
# in Message.content so downstream pipelines can locate the original
# message in the IMAP mailbox.
_IDENTITY_FIELDS = (
    "host",
    "account",
    "folder",
    "uidvalidity",
    "imap_uid",
    "message_id",
)

# Payload fields parsed from the email header.
_CONTENT_FIELDS = (
    "subject",
    "from",
    "in_reply_to",
    "references",
)

# imported is now a top-level Message field (mapped directly).
# imported_at stays in the old EmailIndex only; it is not carried over.
# id is also a top-level field.
_EXCLUDED_FIELDS = frozenset({"id", "imported", "imported_at"})


def _parse_date(raw: str) -> str | None:
    """Try to normalise raw into an ISO 8601 string with timezone.

    Returns None when raw is empty or cannot be reliably parsed.
    The original string is always kept in content["date"] separately.
    """
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.isoformat()
    except (ValueError, TypeError, IndexError):
        return None


def email_row_to_message(row: dict[str, Any]) -> Message:
    """Convert a single EmailIndex row dict into a Message.

    Parameters
    ----------
    row:
        A dict matching the schema stored in data/email/index.json.

    Returns
    -------
    Message
        A unified message whose content preserves every email-specific
        field needed by the existing pipeline.
    """
    content: dict[str, Any] = {}

    # Preserve identity fields first (order matters for readability).
    for key in _IDENTITY_FIELDS:
        content[key] = row[key]

    # Preserve the raw date string.
    content["date"] = row["date"]

    # Preserve remaining payload fields.
    for key in _CONTENT_FIELDS:
        content[key] = row[key]

    # Preserve any extra email-specific keys not already covered and
    # not explicitly excluded (future-proofing without polluting the
    # model contract).
    for key, value in row.items():
        if key not in _EXCLUDED_FIELDS and key not in content:
            content[key] = value

    return Message(
        id=row["id"],
        source="email",
        time=_parse_date(row["date"]),
        imported=bool(row["imported"]),
        content=content,
    )
