"""Manually consume saved events through the existing interactive Agent."""
from contextlib import closing
import json

from .automations import AutomationStore
from .automation_checker import connect, loop_lock


def change_event(store, rule_id, event_id, change, *, allow_paused=False):
    """Re-read and modify only the selected event under the SQLite write lock."""
    with closing(connect(store)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM automations WHERE id=?", (rule_id,)).fetchone()
        allowed = ("active", "paused") if allow_paused else ("active",)
        if row is None or row["status"] not in allowed:
            return None
        rule = store.decode(row)
        event = next((e for e in rule["pending_events"] if e["event_id"] == event_id), None)
        if event is None:
            return None
        change(rule, event)
        db.execute("UPDATE automations SET pending_events=?,status=? WHERE id=?",
                   (json.dumps(rule["pending_events"], ensure_ascii=False), rule["status"], rule_id))
        return event


def consume_once(client, files, store=None, emit=None, email_index_path=None, read=None):
    """Manual delivery keeps the caller's terminal, but uses a fresh event session."""
    from .event_runtime import events, live, run_event, deliver, failure, claim_lock
    from .tools import FileTools
    import time
    import uuid
    import os

    store = store or AutomationStore()
    emit = emit or (lambda text: print(text, flush=True))
    read = read or input
    result = dict(delivered=[], failed=[])
    if files is not None and files.policy._active():
        return dict(result, display="当前会话持有 Memory 事务；请先 /commit 或 /cancel，再手动消费。")
    with loop_lock(store, "consumer"):
        batch = [(r, e) for r, e in events(store) if r["status"] == "active"]
    for rule, event in batch:
        identity = dict(id=rule["id"], event_id=event["event_id"])
        token = uuid.uuid4().hex
        delivery_lock = None
        reserved = False
        try:
            # Only reserve here. Never hold the parent consumer lock across an
            # Agent turn or user confirmation; saved replies must remain deliverable.
            with loop_lock(store, "consumer"):
                event = change_event(store, rule["id"], event["event_id"], lambda r,e:None)
                if event is None or event.get("suspended") or event.get("active_session") or live(event):
                    continue
                if event.get("reply") is not None:
                    delivery_lock = claim_lock(event["event_id"])
                    if not delivery_lock.acquire():
                        continue
                else:
                    change_event(store, rule["id"], event["event_id"], lambda r,e:e.update(
                        active_session=token, launch_at=time.time(), pid=os.getpid()))
                reserved = True
            if event.get("reply") is not None:
                deliver(store, rule, event, emit)
            else:
                session = FileTools(files.root, files.policy.confirm_batch,
                                    files.policy.confirm_transaction, files.confirm_email)
                run_event(client, session, store, rule["id"], event["event_id"], token,
                          emit=emit, read=read, automatic=False, email_index_path=email_index_path)
            remaining = next((e for r, e in events(store) if e["event_id"] == event["event_id"]), None)
            if remaining is None:
                result["delivered"].append(identity)
            elif remaining.get("last_error"):
                result["failed"].append(dict(identity, error=remaining["last_error"]))
        except Exception as error:
            if reserved:
                failure(store, rule["id"], identity["event_id"], error)
            result["failed"].append(dict(identity, error=str(error)))
        finally:
            if delivery_lock:
                delivery_lock.release()
    result["display"] = (f"已展示并移除 {len(result['delivered'])}；失败 {len(result['failed'])}\n"
                         + json.dumps(result, ensure_ascii=False))
    return result
