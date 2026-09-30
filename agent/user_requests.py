"""Lightweight user-interaction requests for call_for_user dual-channel support."""
from contextlib import closing
from datetime import datetime, timezone
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "user_requests.sqlite3"

_STATUSES = ("waiting", "answered", "invalid", "cancelled")
_SOURCES = ("terminal", "feishu")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_requests (
    request_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    rule_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'call_for_user',
    status TEXT NOT NULL DEFAULT 'waiting',
    prompt TEXT NOT NULL,
    answer TEXT,
    answer_source TEXT,
    feishu_message_id TEXT,
    feishu_chat_id TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
)
"""


class UserRequestManager:
    """Manage call_for_user requests backed by SQLite."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DB_PATH
        self._init_db()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA)
        return conn

    def _init_db(self):
        with closing(self._connect()) as db:
            db.execute(_SCHEMA)

    @staticmethod
    def _row_to_dict(row):
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_request(self, request_id, event_id, rule_id, session_id,
                       kind, prompt):
        stamp = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO user_requests "
                "(request_id,event_id,rule_id,session_id,kind,status,prompt,created_at) "
                "VALUES (?,?,?,?,?,'waiting',?,?)",
                (request_id, event_id, rule_id, session_id, kind, prompt, stamp),
            )
        return dict(request_id=request_id, event_id=event_id, rule_id=rule_id,
                    session_id=session_id, kind=kind, status="waiting",
                    prompt=prompt, answer=None, answer_source=None,
                    feishu_message_id=None, feishu_chat_id=None,
                    created_at=stamp, answered_at=None)

    def submit_answer(self, request_id, answer, source):
        """Atomically submit an answer; returns 'success' or 'already_resolved'."""
        stamp = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM user_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["status"] != "waiting":
                return "already_resolved"
            db.execute(
                "UPDATE user_requests SET status='answered', answer=?, "
                "answer_source=?, answered_at=? WHERE request_id=?",
                (answer, source, stamp, request_id),
            )
            return "success"

    def mark_feishu_info(self, request_id, message_id, chat_id):
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE user_requests SET feishu_message_id=?, feishu_chat_id=? "
                "WHERE request_id=?",
                (message_id, chat_id, request_id),
            )

    def mark_invalid(self, request_id):
        stamp = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE user_requests SET status='invalid', answered_at=? "
                "WHERE request_id=? AND status='waiting'",
                (stamp, request_id),
            )

    def mark_cancelled(self, request_id):
        stamp = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE user_requests SET status='cancelled', answered_at=? "
                "WHERE request_id=? AND status='waiting'",
                (stamp, request_id),
            )

    def get_request(self, request_id):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM user_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            return self._row_to_dict(row)

    def get_waiting_for_event(self, event_id):
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM user_requests "
                "WHERE event_id=? AND status='waiting' ORDER BY created_at",
                (event_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def invalidate_waiting_for_event(self, event_id):
        stamp = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE user_requests SET status='invalid', answered_at=? "
                "WHERE event_id=? AND status='waiting'",
                (stamp, event_id),
            )

    def update_feishu_message(self, request_id, message_id, chat_id):
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE user_requests SET feishu_message_id=?, feishu_chat_id=? "
                "WHERE request_id=?",
                (message_id, chat_id, request_id),
            )
