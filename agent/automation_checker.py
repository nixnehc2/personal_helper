"""Local timer ingestion only: no Agent, transport or mailbox operations."""
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
from .automation_triggers import instant, schedule_time


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def connect(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(store.path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute(SCHEMA)
    return db


def check_once(store=None, now=None, tolerance_seconds=None):
    store = store or AutomationStore()
    now = instant((now or store.clock()).isoformat())
    tolerance = float(tolerance_seconds if tolerance_seconds is not None else
                      store.settings.get("AUTOMATION_TOLERANCE_SECONDS", 60))
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("容差必须是有限非负秒数")
    result = dict(enqueued=[], skipped=[], failed=[])
    with closing(connect(store)) as db:
        ids = [r[0] for r in db.execute("SELECT id FROM automations WHERE status='active' AND trigger_type='schedule'")]
    for id in ids:
        try:
            with closing(connect(store)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM automations WHERE id=?", (id,)).fetchone()
                if row is None or row["status"] != "active" or row["trigger_type"] != "schedule":
                    continue
                rule = store.decode(row)
                config = rule["trigger_config"]
                once = config["schedule_type"] == "once"
                cursor = rule["cursor"]
                due = instant(rule["next_check_at"]) if rule["next_check_at"] else None
                if due is None and not (once and cursor):
                    due = schedule_time(config, now, inclusive=True)
                events = rule["pending_events"]
                queued = skipped = None
                status = rule["status"]
                if due is not None and due <= now:
                    # croniter's reverse search near DST can precede its forward
                    # occurrence. The persisted forward schedule remains authoritative.
                    latest = due if once else max(due, schedule_time(config, now, previous=True))
                    late = (now - latest).total_seconds() > tolerance
                    should_queue = not late or config["missed_policy"] == "latest"
                    if should_queue:
                        scheduled = stamp(latest)
                        event_id = f"schedule:{id}:{scheduled}"
                        if not any(e["event_id"] == event_id for e in events):
                            events.append(dict(event_id=event_id, source="timer", event_type="schedule.due",
                                occurred_at=scheduled, content=rule["content"], data=dict(
                                    scheduled_at=scheduled, detected_at=stamp(now), timezone=config["timezone"],
                                    is_catch_up=late), status="pending", reply=None, attempts=0,
                                retry_at=None, last_error=None))
                            queued = dict(id=id, event_id=event_id)
                    if latest > due or not should_queue:
                        skipped = dict(id=id, from_at=stamp(due), through_at=stamp(latest),
                                       reason="已错过并跳过" if not should_queue else "仅保留最近一次，其余计划时间跳过")
                    cursor = stamp(latest)
                    due = None if once else schedule_time(config, latest)
                    if once and not should_queue and not events:
                        status = "completed"
                db.execute("""UPDATE automations SET pending_events=?,cursor=?,next_check_at=?,
                    last_checked_at=?,last_error=NULL,status=? WHERE id=?""",
                    (json.dumps(events, ensure_ascii=False), cursor, stamp(due) if due else None,
                     stamp(now), status, id))
            if queued:
                result["enqueued"].append(queued)
            if skipped:
                result["skipped"].append(skipped)
        except Exception as error:
            failure = dict(id=id, error=str(error))
            try:
                with closing(connect(store)) as db, db:
                    db.execute("UPDATE automations SET last_error=? WHERE id=?", (str(error), id))
            except Exception as record_error:
                failure["record_error"] = str(record_error)
            result["failed"].append(failure)
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
def loop_lock(store):
    """OS-owned file lock is released even if the process crashes; never unlink it."""
    path = store.path.resolve().with_suffix(".checker.lock")
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
            raise RuntimeError("该数据库已有检查循环在运行") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description="前台定时检查器；仅入队，不执行任务")
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
