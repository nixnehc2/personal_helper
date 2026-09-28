"""Source checks and transactional ingestion only; no Agent execution."""
import argparse
from contextlib import closing, contextmanager
from datetime import timezone
import json
import logging
import math
import os
import sqlite3
import time

from .automations import AutomationStore, SCHEMA
from .automation_triggers import instant
from .qq_sync_schedule import check_qq_sync_due


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def connect(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(store.path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute(SCHEMA)
    return db


def check_once(store=None, now=None, tolerance_seconds=None, email_index_path=None,
               qq_db_path=None, qq_whitelist_path=None):
    store = store or AutomationStore()
    now = instant((now or store.clock()).isoformat())
    tolerance = float(tolerance_seconds if tolerance_seconds is not None else
                      store.settings.get("AUTOMATION_TOLERANCE_SECONDS", 60))
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("容差必须是有限非负秒数")
    result = dict(enqueued=[], skipped=[], failed=[])
    from .automation_sources import TimerSource, EmailSource
    sources = {"timer": TimerSource(tolerance), "email": EmailSource(store.settings, email_index_path)}
    with closing(connect(store)) as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM automations WHERE status='active' ORDER BY id")]
    for snapshot in rows:
        id = snapshot["id"]
        try:
            original = store.decode(snapshot)
            source = sources.get("timer" if original["trigger_type"] == "schedule" else original["source"])
            if source is None:
                continue
            prepared = source.prepare(original, now)
            if prepared is None:
                continue
            with closing(connect(store)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM automations WHERE id=?", (id,)).fetchone()
                if row is None or row["status"] != "active":
                    continue
                # Network sync may overlap edits, another checker, or consumption.
                # Discard stale snapshots instead of applying them to a new baseline.
                if any(row[k] != snapshot[k] for k in ("trigger_config", "updated_at", "cursor", "next_check_at", "pending_events")):
                    continue
                rule = store.decode(row)
                queued, skipped = source.check(rule, now, prepared)
                db.execute("""UPDATE automations SET pending_events=?,cursor=?,next_check_at=?,
                    last_checked_at=?,last_error=NULL,status=? WHERE id=?""",
                    (json.dumps(rule["pending_events"], ensure_ascii=False), rule["cursor"], rule["next_check_at"],
                     stamp(now), rule["status"], id))
            result["enqueued"].extend(queued)
            result["skipped"].extend(skipped)
        except Exception as error:
            failure = dict(id=id, error=str(error))
            try:
                with closing(connect(store)) as db, db:
                    db.execute("UPDATE automations SET last_error=? WHERE id=?", (str(error), id))
            except Exception as record_error:
                failure["record_error"] = str(record_error)
            result["failed"].append(failure)
    check_qq_sync_due(store.settings, now, qq_db_path, qq_whitelist_path)
    result["mailbox_syncs"] = [sync["sync_summary"] for sync in sources["email"].synced.values()
                               if isinstance(sync, dict) and "sync_summary" in sync]
    result["display"] = (f"入队 {len(result['enqueued'])}；跳过 {len(result['skipped'])} 条规则；失败 {len(result['failed'])}\n"
                         + json.dumps({k: v for k, v in result.items() if k != "display"}, ensure_ascii=False))
    return result


def pending(store=None, id=None):
    store = store or AutomationStore()
    with closing(connect(store)) as db:
        rows = db.execute("SELECT id,pending_events FROM automations" + (" WHERE id=?" if id else "") + " ORDER BY id",
                          (id,) if id else ()).fetchall()
        if id and not rows:
            raise ValueError(f"规则编号 {id} 不存在")
        events = [dict(rule_id=row["id"], **event) for row in rows
                  for event in json.loads(row["pending_events"]) if event["status"] == "pending"]
    return dict(events=events, display=json.dumps(events, ensure_ascii=False, indent=2))


@contextmanager
def loop_lock(store, purpose="checker"):
    """OS-owned file lock is released even if the process crashes; never unlink it."""
    path = store.path.resolve().with_suffix(f".{purpose}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("该数据库已有检查循环在运行" if purpose == "checker" else "该数据库已有事件消费者在运行") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description="前台时间/邮件检查器；仅入队，不执行任务")
    parser.add_argument("--interval", type=float, help="检查间隔秒数（默认配置或 10）")
    parser.add_argument("--once", action="store_true", help="只检查一次")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = AutomationStore()
    try:
        interval = args.interval if args.interval is not None else float(store.settings.get("AUTOMATION_CHECK_INTERVAL_SECONDS", 10))
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("检查间隔必须是有限正数")
        if args.once:
            result = check_once(store)
            logging.info(result["display"])
            return int(bool(result["failed"]))
        with loop_lock(store):
            logging.info("检查循环已启动；Ctrl+C 退出")
            while True:
                try:
                    result = check_once(store)
                    logging.log(logging.ERROR if result["failed"] else logging.INFO, result["display"])
                except Exception:
                    logging.exception("本轮检查失败，下轮重试")
                time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("检查循环已退出")
        return 0
    except Exception as error:
        logging.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
