"""Read a matched email only at consumption; never import it into Memory."""
from .email_index import EmailIndex, INDEX_PATH
from .email_import import cached_eml, import_lock
from .email_parser import parse_email
from .messages import get_message
from .messages.email_adapter import email_locator


def read_event_email(event, settings, index_path=None):
    """Read the full EML for a matched email event.

    Uses the unified Message layer internally.  Old events with
    ``event["data"]["email"]`` identity dicts are still consumed
    correctly -- the identity is verified against the current index
    before the EML is fetched.
    """
    import agent.email_index as _ei
    original_path = _ei.INDEX_PATH
    if index_path is not None:
        _ei.INDEX_PATH = index_path
    try:
        message = get_message("email", event["data"]["email"]["id"])
    finally:
        _ei.INDEX_PATH = original_path

    locator = email_locator(message)
    expected = event["data"]["email"]

    # Stability check: the identity stored in the event must still match
    # the current index.  This guards against UIDVALIDITY resets and
    # index rebuilds that re-assigned the same numeric id.
    keys = ["id", "host", "account", "folder", "message_id"]
    if not expected["message_id"]:
        keys.extend(["uidvalidity", "imap_uid"])
    actual = {"id": message.id, **locator}
    if any(actual[k] != expected[k] for k in keys):
        raise ValueError("邮件稳定标识不一致；未读取正文，请检查索引")

    index = EmailIndex(INDEX_PATH if index_path is None else index_path)
    directory = index.path.parent / "raw"
    with import_lock(directory, message.id):
        path = cached_eml(message, directory, settings)
        return parse_email(path).model_data()
