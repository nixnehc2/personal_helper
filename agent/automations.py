"""Persistent rule management only. Never schedules or executes work."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from .automation_triggers import EVENT_VALIDATORS, nonempty, object_fields, schedule, schedule_time
from .llm import load_config

DB_PATH = Path(__file__).resolve().parent.parent / "data/automations.sqlite3"
NOTICE = "规则已保存；手动启动检查器可将定时事件和匹配的新邮件入队；/automation consume 可调用 Agent 并在终端展示，聊天程序空闲时会打开独立事件终端自动消费，完成后提交 Windows 通知；检查器仍须单独启动。"
EDITABLE = ("name", "trigger_config", "content", "mode")
SCHEMA = """
CREATE TABLE IF NOT EXISTS automations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL,
 trigger_type TEXT NOT NULL CHECK(trigger_type IN ('schedule','event')),
 source TEXT,
 trigger_config TEXT NOT NULL,
 content TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('once','continuous')),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','completed','cancelled')),
 next_check_at TEXT, cursor TEXT, pending_events TEXT NOT NULL DEFAULT '[]',
 last_checked_at TEXT, last_error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)
"""


class AutomationStore:
    def __init__(self, path=None, settings=None, clock=None):
        self.path = Path(path) if path is not None else DB_PATH
        self.settings = {**os.environ, **load_config()} if settings is None else settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def validate(self, rule, now, infer_mode=False):
        for field in ("name", "content"):
            nonempty(rule.get(field), field)
        preview = []
        if rule.get("trigger_type") == "schedule":
            if rule.get("source") not in (None, ""):
                raise ValueError("定时规则 source 必须为空")
            config, mode, preview = schedule(rule.get("trigger_config"),
                                            self.settings.get("AGENT_TIMEZONE", "Asia/Shanghai"), now)
            if not infer_mode and rule.get("mode", mode) != mode:
                raise ValueError("once 时间规则必须使用 once；interval/cron 必须使用 continuous")
            rule.update(trigger_config=config, mode=mode, source=None)
        elif rule.get("trigger_type") == "event":
            validator = EVENT_VALIDATORS.get(rule.get("source"))
            if validator is None:
                raise ValueError("当前仅支持 source=email 的事件规则")
            rule["trigger_config"] = validator(rule.get("trigger_config"), self.settings)
            rule.setdefault("mode", "continuous")
        else:
            raise ValueError("trigger_type 必须是 schedule 或 event")
        if rule["mode"] not in ("once", "continuous"):
            raise ValueError("mode 必须是 once 或 continuous")
        return preview

    @staticmethod
    def decode(row):
        rule = dict(row)
        for key in ("trigger_config", "pending_events"):
            rule[key] = json.loads(rule[key])
        return rule

    def result(self, rule, now, preview=None):
        result = dict(rule=rule, notice=NOTICE)
        if rule["trigger_type"] == "schedule":
            if preview is None:
                _, _, preview = schedule(rule["trigger_config"], self.settings.get("AGENT_TIMEZONE", "Asia/Shanghai"), now)
            result.update(preview=preview, timezone=rule["trigger_config"]["timezone"])
        conditions = json.dumps(rule["trigger_config"], ensure_ascii=False)
        result["display"] = (f"#{rule['id']} {rule['name']} | {rule['status']}\n"
                             f"触发条件：{rule['trigger_type']}/{rule['source'] or 'time'} {conditions}\n"
                             f"执行指令：{rule['content']}\n持续方式：{rule['mode']}\n")
        if rule["trigger_type"] == "schedule":
            result["display"] += f"时间预览（{result['timezone']}）：" + ("、".join(result["preview"]) or "无未来触发时间（指定时刻已过去）") + "\n"
        result["display"] += NOTICE
        return result

    def manage(self, action, id=None, rule=None):
        if action not in ("create", "list", "get", "update", "pause", "resume", "cancel"):
            raise ValueError("action 必须是 create/list/get/update/pause/resume/cancel")
        if action in ("create", "list"):
            if id is not None:
                raise ValueError("此操作不接受 id")
        elif type(id) is not int or id <= 0:
            raise ValueError("规则编号必须是正整数")
        if action not in ("create", "update") and rule is not None:
            raise ValueError("此操作不接受 rule")
        now = self.clock().astimezone(timezone.utc)
        stamp = now.isoformat()
        preview = None
        if action == "create":
            object_fields(rule, (*EDITABLE, "trigger_type", "source"), ("name", "trigger_type", "trigger_config", "content"))
            rule = dict(rule)
            preview = self.validate(rule, now)
        elif action == "update":
            object_fields(rule, EDITABLE)
            if not rule:
                raise ValueError("修改内容不能为空")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            if action == "list":
                rows = [self.decode(r) for r in db.execute("SELECT * FROM automations ORDER BY id")]
                return dict(rules=rows, display=("\n".join(f"#{r['id']} {r['name']} | {r['trigger_type']} | {r['mode']} | {r['status']}" for r in rows) or "暂无规则") + "\n" + NOTICE,
                            notice=NOTICE)
            if action == "create":
                cursor = db.execute("""INSERT INTO automations
                    (name,trigger_type,source,trigger_config,content,mode,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?)""", (rule["name"], rule["trigger_type"], rule.get("source"),
                    json.dumps(rule["trigger_config"], ensure_ascii=False), rule["content"], rule["mode"], stamp, stamp))
                id = cursor.lastrowid
            else:
                row = db.execute("SELECT * FROM automations WHERE id=?", (id,)).fetchone()
                if row is None:
                    raise ValueError(f"规则编号 {id} 不存在")
                current = self.decode(row)
                if action == "update":
                    candidate = {**current, **rule}
                    preview = self.validate(candidate, now, infer_mode="trigger_config" in rule and "mode" not in rule)
                    db.execute("UPDATE automations SET name=?,trigger_config=?,content=?,mode=?,updated_at=? WHERE id=?",
                               (candidate["name"], json.dumps(candidate["trigger_config"], ensure_ascii=False),
                                candidate["content"], candidate["mode"], stamp, id))
                    if current["trigger_type"] == "schedule" and candidate["trigger_config"] != current["trigger_config"]:
                        next_at = schedule_time(candidate["trigger_config"], now, inclusive=True)
                        db.execute("UPDATE automations SET next_check_at=?,cursor=NULL,last_checked_at=NULL,last_error=NULL WHERE id=?",
                                   (next_at.isoformat(), id))
                    elif current["source"] == "email" and candidate["trigger_config"] != current["trigger_config"]:
                        reset = candidate["trigger_config"]["scope"] != current["trigger_config"]["scope"]
                        db.execute("UPDATE automations SET cursor=?,next_check_at=NULL WHERE id=?",
                                   (None if reset else current["cursor"], id))
                elif action in ("pause", "resume", "cancel"):
                    status = current["status"]
                    target = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
                    if action == "resume" and status in ("cancelled", "completed"):
                        raise ValueError(f"{status} 规则不能恢复；请重新创建")
                    if status != target:
                        if action != "cancel" and status not in ("active", "paused"):
                            raise ValueError(f"{status} 规则不能暂停")
                        db.execute("UPDATE automations SET status=?,updated_at=? WHERE id=?", (target, stamp, id))
                    if action == "resume" and current["source"] in ("email", "qq"):
                            db.execute("UPDATE automations SET cursor=NULL,next_check_at=NULL WHERE id=?", (id,))
                    if action == "cancel":
                        events = [e for e in current["pending_events"] if e.get("status") != "pending"]
                        db.execute("UPDATE automations SET pending_events=? WHERE id=?",
                                   (json.dumps(events, ensure_ascii=False), id))
            saved = self.decode(db.execute("SELECT * FROM automations WHERE id=?", (id,)).fetchone())
            # Build the response before commit; validation/preview failures roll back.
            return self.result(saved, now, preview)
