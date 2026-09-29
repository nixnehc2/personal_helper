"""Small source interface: prepare outside SQLite, check inside the rule transaction.
Sources never call an Agent or write Memory.
"""
from datetime import timedelta, timezone
from email.utils import getaddresses
import json
import re

from .automation_triggers import instant, schedule_time
from .email_index import update_email_index, INDEX_PATH
from .email_import import identity


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TimerSource:
    def __init__(self, tolerance):
        self.tolerance = tolerance

    def prepare(self, rule, now):
        if rule["mode"] == "once" and rule["pending_events"]:
            return None
        return True

    def check(self, rule, now, prepared):
        tolerance = self.tolerance
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
                event_id = f"schedule:{rule['id']}:{scheduled}"
                if not any(e["event_id"] == event_id for e in events):
                    events.append(dict(event_id=event_id, source="timer", event_type="schedule.due",
                        occurred_at=scheduled, content=rule["content"], data=dict(
                            scheduled_at=scheduled, detected_at=stamp(now), timezone=config["timezone"],
                            is_catch_up=late), status="pending", reply=None, attempts=0,
                        retry_at=None, last_error=None))
                    queued = dict(id=rule["id"], event_id=event_id)
            if latest > due or not should_queue:
                skipped = dict(id=rule["id"], from_at=stamp(due), through_at=stamp(latest),
                               reason="已错过并跳过" if not should_queue else "仅保留最近一次，其余计划时间跳过")
            cursor = stamp(latest)
            due = None if once else schedule_time(config, latest)
            if once and not should_queue and not events:
                status = "completed"
        rule.update(pending_events=events, cursor=cursor, next_check_at=stamp(due) if due else None, status=status)
        return ([queued] if queued else []), ([skipped] if skipped else [])


def matches_email(row, match):
    if "from_addresses" in match:
        addresses = {address.casefold() for _, address in getaddresses([row["from"]])}
        if not addresses.intersection(a.casefold() for a in match["from_addresses"]):
            return False
    if "subject_contains" in match and match["subject_contains"] not in row["subject"]:
        return False
    if "reply_to_message_id" in match:
        parents = re.findall(r"<[^<>\s]+>", row["in_reply_to"] + " " + row["references"])
        if match["reply_to_message_id"] not in parents:
            return False
    return True


class EmailSource:
    def __init__(self, settings, index_path=None):
        self.settings = settings
        self.index_path = INDEX_PATH if index_path is None else index_path
        self.synced = {}

    def prepare(self, rule, now):
        if rule["next_check_at"] and instant(rule["next_check_at"]) > now:
            return None
        if rule["mode"] == "once" and rule["pending_events"]:
            return None
        scope = rule["trigger_config"]["scope"]
        account = scope["account_id"].casefold()
        if account != self.settings.get("EMAIL_ACCOUNT", "").casefold():
            raise ValueError("规则邮箱与当前 EMAIL_ACCOUNT 不一致")
        key = (account, scope["folder"])
        if key not in self.synced:
            try:
                result = update_email_index(dict(self.settings, EMAIL_FOLDER=scope["folder"]), self.index_path)
                self.synced[key] = result
            except Exception as error:
                self.synced[key] = error
        result = self.synced[key]
        if isinstance(result, Exception):
            raise result
        return result

    def check(self, rule, now, prepared):
        source = prepared["source"]
        rows = sorted((r for r in prepared["emails"] if all(r[k] == source[k]
                      for k in ("host", "account", "folder"))), key=lambda r: r["id"])
        high = max((r["id"] for r in rows), default=0)
        previous = json.loads(rule["cursor"]) if rule["cursor"] else None
        # UIDVALIDITY reset can re-index old messages without Message-ID; baseline
        # the new generation rather than mistake history for newly arrived mail.
        baseline = previous is None or previous["source"] != source
        queued, skipped = [], []
        if baseline:
            skipped.append(dict(id=rule["id"], reason="邮件基线已建立；现有邮件不触发", through_id=high))
        else:
            for row in rows:
                if row["id"] <= previous["last_id"]:
                    continue
                if not matches_email(row, rule["trigger_config"]["match"]):
                    continue
                event_id = f"email:{rule['id']}:{row['id']}"
                if any(e["event_id"] == event_id for e in rule["pending_events"]):
                    continue
                rule["pending_events"].append(dict(event_id=event_id, source="email", event_type="email.matched",
                    occurred_at=stamp(now), content=rule["content"], data=dict(
                        email=identity(row), sender=row["from"], subject=row["subject"], date=row["date"],
                        in_reply_to=row["in_reply_to"], references=row["references"], detected_at=stamp(now)),
                    status="pending", reply=None, attempts=0, retry_at=None, last_error=None))
                queued.append(dict(id=rule["id"], event_id=event_id))
                if rule["mode"] == "once":
                    break
        rule["cursor"] = json.dumps(dict(source=source, last_id=max(high, previous["last_id"] if previous and not baseline else 0)))
        rule["next_check_at"] = stamp(now + timedelta(seconds=rule["trigger_config"]["check_interval_seconds"]))
        return queued, skipped


def matches_qq(message, match):
    """Match a QQ message against the trigger match config.

    Same-field values are OR; different fields are AND.
    Empty match matches everything.
    """
    content = message.content
    if "conversations" in match:
        conv = content.get("conversation", {})
        kind, cid = conv.get("type"), str(conv.get("id", ""))
        if not any(c["type"] == kind and str(c["id"]) == cid for c in match["conversations"]):
            return False
    if "sender_ids" in match:
        sender_id = str(content.get("sender", {}).get("user_id", ""))
        if sender_id not in [str(s) for s in match["sender_ids"]]:
            return False
    if "text_contains" in match:
        if match["text_contains"] not in content.get("text", ""):
            return False
    return True


class QQSource:
    def __init__(self, db_path=None):
        from .qq_sync import QQStore, DB_PATH
        self.store = QQStore(db_path if db_path is not None else DB_PATH)
        self._cursor_cache = {}

    def prepare(self, rule, now):
        if rule["mode"] == "once" and rule["pending_events"]:
            return None
        db_path = str(self.store.path)
        if db_path not in self._cursor_cache:
            self._cursor_cache[db_path] = self.store.rowid_cursor()
        return self._cursor_cache[db_path]

    def check(self, rule, now, prepared):
        current_max_rowid = prepared
        previous = json.loads(rule["cursor"]) if rule["cursor"] else None
        baseline = previous is None
        queued, skipped = [], []
        if baseline:
            skipped.append(dict(id=rule["id"], reason="QQ 基线已建立；现有消息不触发", through_rowid=current_max_rowid))
        else:
            from .messages.models import Message
            rows = self.store.messages_between_rowids(previous["last_rowid"], current_max_rowid)
            for payload, rowid in rows:
                try:
                    message = Message(**json.loads(payload))
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue
                if not matches_qq(message, rule["trigger_config"]["match"]):
                    continue
                event_id = f"qq:{rule['id']}:{message.id}"
                if any(e["event_id"] == event_id for e in rule["pending_events"]):
                    continue
                conv = message.content.get("conversation", {})
                rule["pending_events"].append(dict(event_id=event_id, source="qq", event_type="qq.matched",
                    occurred_at=stamp(now), content=rule["content"], data=dict(
                        message=dict(source="qq", id=message.id),
                        conversation=dict(type=conv.get("type", ""), id=str(conv.get("id", "")),
                                          name=conv.get("name", "")),
                        sender=dict(message.content.get("sender", {})),
                        message_time=message.time, detected_at=stamp(now)),
                    status="pending", reply=None, attempts=0, retry_at=None, last_error=None))
                queued.append(dict(id=rule["id"], event_id=event_id))
                if rule["mode"] == "once":
                    break
        rule["cursor"] = json.dumps(dict(last_rowid=current_max_rowid))
        return queued, skipped
