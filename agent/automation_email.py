"""Read a matched email only at consumption; never import it into Memory."""
from .email_index import EmailIndex, INDEX_PATH
from .email_import import cached_eml, import_lock
from .email_parser import parse_email


def read_event_email(event, settings, index_path=None):
    index = EmailIndex(INDEX_PATH if index_path is None else index_path)
    expected = event["data"]["email"]
    directory = index.path.parent / "raw"
    with import_lock(directory, expected["id"]):
        row = index.get(expected["id"])
        keys = ["id", "host", "account", "folder", "message_id"]
        if not expected["message_id"]:
            keys.extend(["uidvalidity", "imap_uid"])
        if any(row[k] != expected[k] for k in keys):
            raise ValueError("邮件稳定标识不一致；未读取正文，请检查索引")
        path = cached_eml(row, directory, settings)
        return parse_email(path).model_data()
