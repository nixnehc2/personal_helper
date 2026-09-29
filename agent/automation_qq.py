"""Read a matched QQ message only at consumption; never import it into Memory."""
from .messages import get_message
from .messages.formatters import format_qq


def read_event_qq(event, settings):
    """Read the full QQ message for a matched event.

    Uses the unified Message layer.  Does not call import_message or
    modify Message.imported.
    """
    message_id = event["data"]["message"]["id"]
    message = get_message("qq", message_id)
    return dict(
        source="qq",
        id=message.id,
        time=message.time,
        conversation=message.content.get("conversation", {}),
        sender=message.content.get("sender", {}),
        text=message.content.get("text", ""),
        formatted=format_qq(message),
    )