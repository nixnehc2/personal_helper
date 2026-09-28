"""Source registry: storage, source preparation and formatting live behind one interface."""
import os
import json
from contextlib import closing

from .email_adapter import email_row_to_message, email_identity
from .formatters import format_qq, view_email, summarize_email, summarize_qq


class EmailSource:
    summarize = staticmethod(summarize_email)

    def __init__(self, path=None, config=None):
        from agent.email_index import EmailIndex, INDEX_PATH
        self.store = EmailIndex(INDEX_PATH if path is None else path)
        self.config = config

    def get(self, id):
        return email_row_to_message(self.store.get(id))

    def list(self):
        return [email_row_to_message(row) for row in self.store.read()["emails"]]

    def listing_sql(self, alias):
        if not self.store.path.exists() and not self.store.legacy_path.exists():
            return None, None
        with closing(self.store.connect()):
            pass
        return self.store.path, f"""SELECT 'email' AS source, id, payload,
            message_time(json_extract(payload, '$.date'), 'email') AS stamp,
            json_extract(payload, '$.imported') AS imported,
            json_extract(payload, '$.folder') AS conversation1,
            json_extract(payload, '$.message_id') AS conversation2,
            NULL AS conversation3 FROM {alias}.email_messages"""

    @staticmethod
    def decode_listing(payload):
        return email_row_to_message(json.loads(payload))

    def conversation_matches(self, message, conversation):
        return conversation in (message.content.get("folder"), message.content.get("message_id"))

    def lock(self, id):
        from .locking import import_lock
        return import_lock(self.store.path.parent / "raw", id)

    def original(self, message):
        from agent.email_import import cached_eml, load_config
        settings = dict(os.environ)
        settings.update(load_config() if self.config is None else self.config)
        directory = self.store.path.parent / "raw"
        directory.mkdir(parents=True, exist_ok=True)
        return cached_eml(message, directory, settings)

    def read(self, message):
        from agent.email_parser import parse_bytes, read_raw_email
        return view_email(parse_bytes(read_raw_email(self.original(message))))

    def process(self, message, client, files, **kwargs):
        # Keep the legacy EML API; its Agent/Temporary steps use shared process_input.
        from agent.email_workflow import process_eml
        path = self.original(message)
        return dict(process_eml(path, client, files, reprocess=True, **kwargs), eml_path=str(path))

    def mark_imported(self, message):
        row = self.store.mark_imported(email_identity(message))
        return dict(imported=True, imported_at=row["imported_at"])


class QQSource:
    summarize = staticmethod(summarize_qq)
    read = staticmethod(format_qq)

    def __init__(self, path=None):
        from agent.qq_sync import QQStore
        self.store = QQStore(path)

    def get(self, id):
        return self.store.get(id)

    def list(self):
        return self.store.list()

    def listing_sql(self, alias):
        if not self.store.path.exists():
            return None, None
        return self.store.path, f"""SELECT 'qq' AS source, id, payload,
            message_time(json_extract(payload, '$.time'), 'qq') AS stamp,
            json_extract(payload, '$.imported') AS imported,
            CAST(json_extract(payload, '$.content.conversation.id') AS TEXT) AS conversation1,
            json_extract(payload, '$.content.conversation.type') || ':' ||
                json_extract(payload, '$.content.conversation.id') AS conversation2,
            json_extract(payload, '$.content.conversation.name') AS conversation3
            FROM {alias}.messages"""

    @staticmethod
    def decode_listing(payload):
        from .models import Message
        return Message(**json.loads(payload))

    def conversation_matches(self, message, conversation):
        chat = message.content["conversation"]
        return conversation in (str(chat["id"]), f"{chat['type']}:{chat['id']}", chat.get("name"))

    def lock(self, id):
        from .locking import import_lock
        return import_lock(self.store.path.parent / "imports", id)

    def process(self, message, client, files, **kwargs):
        from .processing import MESSAGE_RULES, process_input
        return process_input(format_qq(message), MESSAGE_RULES, client, files, **kwargs)

    def mark_imported(self, message):
        self.store.mark_imported(message)
        return dict(imported=True)


SOURCES = {"email": EmailSource, "qq": QQSource}


def source_backend(source):
    try:
        factory = SOURCES[source]
    except (KeyError, TypeError):
        raise ValueError(f"不支持的消息来源 {source!r}；当前支持 {tuple(SOURCES)}") from None
    return factory()
