"""Pure trigger validation and preview. No mailbox access or execution."""
from datetime import datetime, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


def object_fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(f"配置字段错误；允许 {', '.join(allowed)}；必填 {', '.join(required)}")


def nonempty(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def positive(value, field):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} 必须是有限正数")


def zone(value):
    nonempty(value, "timezone")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"无效时区：{value}") from None


def instant(value):
    nonempty(value, "时间")
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except ValueError:
        raise ValueError("时间必须是包含时区偏移的 ISO 8601 时间") from None


def schedule_time(config, reference, *, previous=False, inclusive=False):
    """Shared UTC occurrence calculation for preview and runtime (no iteration over gaps)."""
    kind = config["schedule_type"]
    if kind == "once":
        return instant(config["at"])
    if kind == "interval":
        start = instant(config["start_at"])
        step = timedelta(seconds=config["interval_seconds"])
        count = (reference - start) // step
        if previous:
            return start + count * step if count >= 0 else None
        if count < 0:
            return start
        if not inclusive or start + count * step < reference:
            count += 1
        return start + count * step
    base = reference.astimezone(zone(config["timezone"]))
    # get_prev is exclusive; one microsecond includes an exact minute boundary.
    if previous:
        base = (reference + timedelta(microseconds=1)).astimezone(zone(config["timezone"]))
    iterator = croniter(config["expression"], base, day_or=True, max_years_between_matches=8)
    return (iterator.get_prev(datetime) if previous else iterator.get_next(datetime)).astimezone(timezone.utc)


def schedule(config, default_timezone, now):
    kind = config.get("schedule_type") if isinstance(config, dict) else None
    required = {"once": ("at",), "interval": ("start_at", "interval_seconds"),
                "cron": ("expression",)}.get(kind)
    if required is None:
        raise ValueError("schedule_type 必须是 once、interval 或 cron")
    fields = ("schedule_type", "missed_policy", "timezone", *required)
    object_fields(config, fields, ("schedule_type", "missed_policy", *required))
    config = dict(config)
    tz_name = config.setdefault("timezone", default_timezone)
    tz = zone(tz_name)
    if config["missed_policy"] not in ("latest", "skip"):
        raise ValueError("missed_policy 必须是 latest 或 skip")
    times = []
    if kind == "once":
        at = instant(config["at"])
        config["at"] = at.isoformat()
        if at >= now:
            times = [at]
    elif kind == "interval":
        start = instant(config["start_at"])
        positive(config["interval_seconds"], "interval_seconds")
        step = timedelta(seconds=config["interval_seconds"])
        if step <= timedelta(0):
            raise ValueError("interval_seconds 小于时间精度（微秒）")
        config["start_at"] = start.isoformat()
    else:
        expression = nonempty(config["expression"], "expression")
        # Deliberately a numeric Unix subset, excluding macros, seconds and extensions.
        if len(expression.split()) != 5 or not re.fullmatch(r"[0-9*/ ,\-]+", expression):
            raise ValueError("Cron 仅支持五字段数字 Unix 格式：分 时 日 月 星期；支持 * , - /")
        if not croniter.is_valid(expression):
            raise ValueError("非法 Cron 表达式")
    # Both preview and checker use exactly the same occurrence engine.
    if kind != "once":
        try:
            first = schedule_time(config, now, inclusive=kind == "interval")
            second = schedule_time(config, first)
            times = [first, second, schedule_time(config, second)]
        except ValueError:
            raise ValueError("Cron 在未来八年内没有有效触发时间") from None
    return config, "once" if kind == "once" else "continuous", [t.astimezone(tz).isoformat() for t in times]


def email(config, settings):
    object_fields(config, ("scope", "match", "check_interval_seconds"),
                  ("scope", "match", "check_interval_seconds"))
    scope, match = config["scope"], config["match"]
    object_fields(scope, ("account_id", "folder"), ("account_id", "folder"))
    account = nonempty(scope["account_id"], "account_id")
    if account.casefold() != settings.get("EMAIL_ACCOUNT", "").casefold():
        raise ValueError("account_id 不对应本地 EMAIL_ACCOUNT 配置")
    nonempty(scope["folder"], "folder")
    object_fields(match, ("from_addresses", "subject_contains", "reply_to_message_id"))
    for key, value in match.items():
        if key == "from_addresses":
            if not isinstance(value, list) or not value:
                raise ValueError("from_addresses 必须是非空地址列表")
            if any(not isinstance(v, str) or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", v) for v in value):
                raise ValueError("from_addresses 包含无效邮件地址")
        else:
            nonempty(value, key)
            if key == "reply_to_message_id" and not re.fullmatch(r"<[^<>\s]+>", value):
                raise ValueError("reply_to_message_id 必须是 <...> 格式的 Message-ID")
    positive(config["check_interval_seconds"], "check_interval_seconds")
    return config



def qq(config, settings):
    object_fields(config, ("match", "check_interval_seconds"), ("match",))
    if "check_interval_seconds" in config:
        positive(config["check_interval_seconds"], "check_interval_seconds")
    match = config["match"]
    if not isinstance(match, dict):
        raise ValueError("match 必须是对象")
    for key in match:
        if key not in ("conversations", "sender_ids", "text_contains"):
            raise ValueError(f"未知 match 字段：{key}；允许 conversations、sender_ids、text_contains")
    if "conversations" in match:
        convs = match["conversations"]
        if not isinstance(convs, list) or not convs:
            raise ValueError("conversations 必须是非空列表")
        for item in convs:
            if not isinstance(item, dict):
                raise ValueError("conversation 必须是对象")
            if set(item) - {"type", "id"} or "type" not in item or "id" not in item:
                raise ValueError("conversation 只允许 type 和 id")
            if item["type"] not in ("group", "private"):
                raise ValueError("conversation type 必须是 group 或 private")
            if not isinstance(item["id"], (str, int)) or not str(item["id"]).strip():
                raise ValueError("conversation id 必须是非空值")
    if "sender_ids" in match:
        sids = match["sender_ids"]
        if not isinstance(sids, list) or not sids:
            raise ValueError("sender_ids 必须是非空列表")
        for v in sids:
            if not isinstance(v, (str, int)) or not str(v).strip():
                raise ValueError("sender_ids 项必须是非空值")
    if "text_contains" in match:
        nonempty(match["text_contains"], "text_contains")
    return config


EVENT_VALIDATORS = {"email": email, "qq": qq}
