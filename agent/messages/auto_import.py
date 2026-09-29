"""Post-Sync Auto Import: import new Messages after sync using existing import_message.

Each source maintains an independent cursor. First run establishes baseline
(does not import history). Each message gets its own Agent turn.
Failure stops processing for that source; cursor stays at last success.
"""
import json
import logging
import os
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data/messages/auto_import_state.json"


def _load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _email_max_id():
    """Return the current max email local ID, or 0 if empty."""
    from .sources import EmailSource
    backend = EmailSource()
    messages = backend.list()
    return max((m.id for m in messages), default=0)


def _email_messages_after(cursor):
    """Return email Message objects with id > cursor, in id order."""
    from .sources import EmailSource
    backend = EmailSource()
    messages = [m for m in backend.list() if m.id > cursor]
    messages.sort(key=lambda m: m.id)
    return [(m, m.id) for m in messages]


def _qq_max_rowid():
    """Return the current max QQ rowid, or 0 if empty."""
    from agent.qq_sync import QQStore
    return QQStore().rowid_cursor()


def _qq_messages_after(cursor):
    """Return QQ Message objects with rowid > cursor, in rowid order."""
    from agent.qq_sync import QQStore
    from .models import Message
    store = QQStore()
    rows = store.messages_after_rowid(cursor)
    result = []
    for payload, rowid in rows:
        try:
            msg = Message(**json.loads(payload))
            result.append((msg, rowid))
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return result


# Source registry: string names only; functions resolved dynamically for patchability.
_SOURCES = ("email", "qq")


def _source_max(source):
    """Return the current max identifier for a source (dynamic lookup)."""
    if source == "email":
        return _email_max_id()
    elif source == "qq":
        return _qq_max_rowid()
    raise ValueError(f"不支持的 auto-import source: {source}")


def _source_messages_after(source, cursor):
    """Return (message, identifier) pairs after cursor for a source (dynamic lookup)."""
    if source == "email":
        return _email_messages_after(cursor)
    elif source == "qq":
        return _qq_messages_after(cursor)
    raise ValueError(f"不支持的 auto-import source: {source}")


def _ensure_baseline(state):
    """On first run, establish baseline cursors for all sources without importing."""
    changed = False
    for source in _SOURCES:
        if source not in state:
            current_max = _source_max(source)
            state[source] = {"cursor": current_max}
            changed = True
            logging.info("[auto-import] %s baseline: cursor=%s", source, current_max)
    if changed:
        _save_state(state)
    return changed


def auto_import_source(source, client, files, emit=print):
    """Process new messages for one source. Returns (processed, failed, cursor).

    On failure, stops processing and returns cursor at last success.
    Already-imported messages are skipped and cursor advances past them.
    """
    if source not in _SOURCES:
        raise ValueError(f"不支持的 auto-import source: {source}")

    state = _load_state()
    if source not in state:
        # No baseline yet; establish it
        state[source] = {"cursor": _source_max(source)}
        _save_state(state)
        return 0, 0, state[source]["cursor"]

    cursor = state[source]["cursor"]
    new_messages = _source_messages_after(source, cursor)

    processed = 0
    failed = 0

    for message, identifier in new_messages:
        if message.imported:
            # Already imported (manually or by previous run); skip and advance
            cursor = identifier
            state[source]["cursor"] = cursor
            _save_state(state)
            continue

        try:
            from .importing import import_message
            result = import_message(message.id, client, files, source=source, emit=emit)
            if result.get("status") in ("processed", "already_imported"):
                cursor = identifier
                processed += 1
            else:
                # Unexpected status; stop
                failed += 1
                break
        except Exception as error:
            logging.warning("[auto-import] %s message %s failed: %s", source, identifier, error)
            failed += 1
            break

        state[source]["cursor"] = cursor
        _save_state(state)

    return processed, failed, cursor


def run_post_sync_auto_import(client, files, emit=print):
    """Run auto-import for all sources after sync completes.

    Returns a summary dict.
    """
    state = _load_state()
    first_run = _ensure_baseline(state)

    if first_run:
        return dict(baseline=True, sources={s: state[s]["cursor"] for s in _SOURCES},
                    display="自动导入基线已建立；历史消息不会自动处理。")

    results = {}
    for source in _SOURCES:
        processed, failed, cursor = auto_import_source(source, client, files, emit)
        results[source] = dict(processed=processed, failed=failed, cursor=cursor)

    total_processed = sum(r["processed"] for r in results.values())
    total_failed = sum(r["failed"] for r in results.values())

    parts = []
    for source, r in results.items():
        if r["processed"] or r["failed"]:
            parts.append(f"{source}: 导入 {r['processed']}，失败 {r['failed']}")
    display = "自动导入完成" + ("；" + "；".join(parts) if parts else "（无新消息）")
    if total_failed:
        display += "\n失败的消息将在下次同步后重试。"

    return dict(baseline=False, results=results, processed=total_processed,
                failed=total_failed, display=display)