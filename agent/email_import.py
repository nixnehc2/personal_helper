"""Resolve one local ID, retrieve its original bytes, and reuse EML processing."""
from contextlib import contextmanager
from email import policy
from email.parser import BytesHeaderParser
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
import tempfile

from .email_index import EmailIndex, INDEX_PATH
from .email_parser import MAX_EMAIL_BYTES
from .llm import load_config
from .messages import get_message, Message
from .messages.email_adapter import email_identity, email_locator

IDENTITY_KEYS = ("id", "host", "account", "folder", "uidvalidity", "imap_uid", "message_id")


def identity(row):
    """Legacy helper for code that still operates on raw row dicts."""
    return {key: row[key] for key in IDENTITY_KEYS}


@contextmanager
def import_lock(directory, id):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{id}.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError(f"邮件 {id} 正在导入；异常退出后请确认无导入进程再删除 {id}.lock") from None
    try:
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def atomic_bytes(path, raw):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".eml-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_eml(locator, settings):
    """Download the raw RFC 822 bytes for an email via IMAP.

    *locator* is a dict with at least ``host``, ``account``, ``folder``,
    ``uidvalidity``, ``imap_uid`` and ``message_id`` -- either a raw
    EmailIndex row or the output of :func:`email_locator`.
    """
    account = settings.get("EMAIL_ACCOUNT", "")
    host = settings.get("EMAIL_IMAP_HOST", "imap.qq.com")
    if account.lower() != locator["account"] or host.lower() != locator["host"]:
        raise ValueError("当前邮箱配置与该邮件索引不一致，未下载邮件")
    if not settings.get("EMAIL_AUTH_CODE"):
        raise ValueError("请配置 EMAIL_AUTH_CODE")
    if not re.fullmatch(r"[1-9][0-9]*", locator["imap_uid"]):
        raise ValueError("索引中的 IMAP UID 无效，请重新同步")
    connection = None
    stage = "连接"
    try:
        connection = imaplib.IMAP4_SSL(host, int(settings.get("EMAIL_IMAP_PORT", "993")),
                                      ssl_context=ssl.create_default_context(), timeout=30)
        stage = "登录"
        status, _ = connection.login(account, settings["EMAIL_AUTH_CODE"])
        if status != "OK":
            raise ValueError("login failed")
        stage = "选择文件夹"
        status, _ = connection.select(locator["folder"], readonly=True)
        if status != "OK":
            raise ValueError("select failed")
        stage = "校验 UIDVALIDITY（请先 update_email 再重试）"
        _, validity = connection.response("UIDVALIDITY")
        if not validity or validity[0] != locator["uidvalidity"].encode("ascii"):
            raise ValueError("UIDVALIDITY changed")
        stage = "下载指定邮件（邮件可能已删除，请重新同步）"
        status, parts = connection.uid("fetch", locator["imap_uid"], "(UID RFC822.SIZE BODY.PEEK[])")
        if status != "OK":
            raise ValueError("fetch failed")
        raw = None
        for part in parts or []:
            if not isinstance(part, tuple) or not isinstance(part[0], bytes):
                continue
            uid = re.search(rb"\bUID\s+(\d+)\b", part[0])
            size = re.search(rb"\bRFC822.SIZE\s+(\d+)\b", part[0])
            if uid and uid[1].decode("ascii") == locator["imap_uid"] and b"BODY[]" in part[0].upper():
                if not size or not isinstance(part[1], bytes) or len(part[1]) != int(size[1]):
                    raise ValueError("incomplete RFC822 body")
                raw = part[1]
        if not raw:
            raise ValueError("missing RFC822 body")
        stage = "校验完整 EML（须不超过现有 25 MiB 导入上限）"
        if len(raw) > MAX_EMAIL_BYTES:
            raise ValueError("email too large")
        message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
        stage = "校验 Message-ID（请重新同步）"
        if locator["message_id"] and str(message.get("Message-ID", "")).strip() != locator["message_id"]:
            raise ValueError("Message-ID mismatch")
        return raw
    except (OSError, imaplib.IMAP4.error, ValueError, UnicodeError):
        raise ValueError(f"IMAP {stage}失败；未标记已导入") from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


def cached_eml(message_or_row, directory, settings):
    """Download (or reuse cached) EML for a message.

    *message_or_row* may be a :class:`Message` (preferred) or a raw row
    dict (legacy compatibility for automation_email).
    """
    if isinstance(message_or_row, Message):
        msg_id = message_or_row.id
        ident = email_identity(message_or_row)
    else:
        msg_id = message_or_row["id"]
        ident = identity(message_or_row)
    locator = ident  # locator dict == identity dict for download_eml

    path = directory / f"{msg_id}.eml"
    manifest = path.with_suffix(".json")
    try:
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        with path.open("rb") as stream:
            raw = stream.read(MAX_EMAIL_BYTES + 1)
        if (saved.get("identity") == ident and len(raw) <= MAX_EMAIL_BYTES
                and saved.get("sha256") == hashlib.sha256(raw).hexdigest()):
            return path
    except (OSError, ValueError, AttributeError):
        pass
    raw = download_eml(locator, settings)
    atomic_bytes(path, raw)
    atomic_bytes(manifest, json.dumps(dict(identity=ident, sha256=hashlib.sha256(raw).hexdigest()),
                                    ensure_ascii=False).encode("utf-8"))
    return path


def import_email(id, client, files, *, messages=None, emit=print, index_path=None, config=None):
    """Single implementation for direct commands and Agent tool calls."""
    from .email_workflow import process_eml

    # Resolve the effective index path.  ``INDEX_PATH`` at module scope may
    # have been patched by tests, so we read it first and only override
    # ``agent.email_index.INDEX_PATH`` when an explicit *index_path* was
    # given.
    effective_path = INDEX_PATH if index_path is None else index_path

    # Temporarily point the email_index module at the same path so that
    # ``get_message`` (which imports INDEX_PATH from email_index) sees the
    # correct location.
    import agent.email_index as _ei
    original_ei_path = _ei.INDEX_PATH
    _ei.INDEX_PATH = effective_path
    try:
        message = get_message("email", id)
    finally:
        _ei.INDEX_PATH = original_ei_path

    # EmailIndex is still needed for directory layout and mark_imported.
    index = EmailIndex(effective_path)
    directory = index.path.parent / "raw"
    with import_lock(directory, id):
        row = index.get(id)
        if row["imported"]:
            return dict(id=id, status="already_imported", note="该邮件已经导入", imported=True,
                        imported_at=row["imported_at"])
        settings = dict(os.environ)
        settings.update(load_config() if config is None else config)
        path = cached_eml(message, directory, settings)
        # An archived raw file alone is not proof of completed processing. This
        # internal retry allows recovery after failure; no force option is exposed.
        result = process_eml(path, client, files, messages=messages, emit=emit, reprocess=True)
        if result.get("status") != "processed":
            raise ValueError("EML 导入流程未正常完成；未标记已导入，Temporary 保留，可重试")
        updated = index.mark_imported(row)
        return dict(result, id=id, imported=True, imported_at=updated["imported_at"],
                    eml_path=str(path), note="邮件处理完成；正式 Memory 仍以用户 review 结果为准")
