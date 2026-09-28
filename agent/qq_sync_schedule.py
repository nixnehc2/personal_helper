"""Persistent QQ polling deadline in the existing message database."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import math

from .qq_sync import QQStore

DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_RETRY_SECONDS = 60
STATE_KEY = "qq_next_sync_at"


def utc_now():
    return datetime.now(timezone.utc)


def seconds(config, key, default):
    value = float((config or {}).get(key, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} 必须是有限正数")
    return value


def parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("QQ sync deadline must include timezone")
    return result.astimezone(timezone.utc)


def get_next_sync_at(db_path=None):
    with closing(QQStore(db_path).connect()) as db:
        row = db.execute("SELECT value FROM sync_state WHERE key=?", (STATE_KEY,)).fetchone()
    return parse_time(row[0]) if row else None


def set_next_sync_at(value, db_path=None, *, expected=None):
    """Transactional write; optional CAS preserves a newer manual sync deadline."""
    value = parse_time(value.isoformat()).isoformat()
    with closing(QQStore(db_path).connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if expected is None:
            db.execute("INSERT OR REPLACE INTO sync_state VALUES (?, ?)", (STATE_KEY, value))
            return True
        return bool(db.execute("UPDATE sync_state SET value=? WHERE key=? AND value=?",
                               (value, STATE_KEY, expected)).rowcount)


def record_manual_success(config=None, db_path=None):
    interval = seconds(config, "QQ_SYNC_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
    set_next_sync_at(utc_now() + timedelta(seconds=interval), db_path)


def _claim_due(now, retry, db_path):
    """Atomically reserve this attempt; release the DB before network work.

    A crash leaves a bounded retry deadline rather than a permanent reservation.
    The long-running checker is additionally protected by its existing loop lock.
    """
    claim = (now + timedelta(seconds=retry)).isoformat()
    with closing(QQStore(db_path).connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM sync_state WHERE key=?", (STATE_KEY,)).fetchone()
        if row:
            try:
                if now < parse_time(row[0]):
                    return None
            except ValueError:
                logging.warning("[QQ] invalid persisted deadline; checking now")
        db.execute("INSERT OR REPLACE INTO sync_state VALUES (?, ?)", (STATE_KEY, claim))
    return claim


def check_qq_sync_due(config=None, now=None, db_path=None, whitelist_path=None):
    """Check SQLite every tick, connect only when due, never read stdin."""
    from .qq_sync import update_qq
    from .qq_sync_selector import load_whitelist

    try:
        now = parse_time((now or utc_now()).isoformat())
        interval = seconds(config, "QQ_SYNC_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
        retry = seconds(config, "QQ_SYNC_RETRY_SECONDS", DEFAULT_RETRY_SECONDS)
        claim = _claim_due(now, retry, db_path)
        if claim is None:
            return
        delay = interval
        failed = False
        try:
            try:
                allowed = load_whitelist(whitelist_path)
            except FileNotFoundError:
                logging.warning("[QQ] automatic sync skipped: whitelist not configured")
                allowed = None
            except Exception as error:
                raise ValueError(f"invalid sync_conversations.json: {error}") from error
            if allowed == set():
                logging.info("[QQ] automatic sync skipped: no conversations selected")
            if allowed:
                def progress(event):
                    if event["event"] == "start":
                        logging.info("[QQ] selected conversations: %s", event["total_conversations"])

                logging.info("[QQ] automatic sync started")
                result = update_qq(config=config, db_path=db_path,
                                   allowed_conversations=allowed, progress=progress)
                if result["failed"]:
                    raise RuntimeError("; ".join(result["errors"]))
                logging.info("[QQ] automatic sync completed")
        except Exception as error:
            delay = retry
            failed = True
            logging.warning("[QQ] automatic sync failed: %s", error)
        deadline = now + timedelta(seconds=delay)
        if set_next_sync_at(deadline, db_path, expected=claim):
            logging.info("[QQ] %s: %s", "retry at" if failed else "next sync", deadline.isoformat())
    except Exception as error:
        # State/config/storage failures must not prevent Timer or Email checks.
        logging.warning("[QQ] automatic sync scheduling failed: %s", error)
