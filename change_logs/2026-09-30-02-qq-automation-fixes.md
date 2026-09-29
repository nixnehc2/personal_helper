# 2026-09-30：QQ Automation 三项修正

- 日期：2026-09-30（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

对已实现的 QQ Automation Event Source 做三项小范围修正：
1. 删除 QQ 每规则独立的 `check_interval_seconds` / `next_check_at` gating
2. 修复 rowid cursor 的 snapshot 上界并发问题
3. 泛化残留的 Email-only 文案

## 实际改动

### 1. 删除 QQ per-rule polling interval

- `agent/automation_triggers.py` — QQ validator `object_fields` 仅允许 `("match",)`，移除 `check_interval_seconds` 和 `positive()`；Email validator 的 `check_interval_seconds` 保持不变
- `agent/automation_sources.py` — QQSource.prepare() 移除 `next_check_at` gating，每 tick 允许检查；QQSource.check() 不再写 `next_check_at`
- `agent/tools.py` — automation tool 描述更新：QQ trigger_config 只含 match，QQ 网络同步频率由 `QQ_SYNC_INTERVAL_SECONDS` 全局控制

### 2. 修复 rowid snapshot 上界

- `agent/qq_sync.py` — 新增 `messages_between_rowids(after_rowid, through_rowid)`，SQL 使用 `rowid > ? AND rowid <= ?`
- `agent/automation_sources.py` — QQSource.check() 改用 `messages_between_rowids(previous["last_rowid"], current_max_rowid)`，cursor 直接推进到 `current_max_rowid`（prepare snapshot），防止并发插入的消息被越界读取

### 3. 泛化 Email-only 文案

- `agent/automation_checker.py` — argparse description 从"前台时间/邮件检查器"改为"前台时间/消息检查器"

### 测试修改

- `tests/test_automation_qq.py`
  - `qq_rule()` helper 移除 `check_interval_seconds`
  - `test_check_interval_sets_next_check_at` 替换为 `test_qq_source_does_not_use_next_check_at`（验证 next_check_at 始终为 NULL）
  - 新增 `test_rowid_snapshot_upper_bound`（验证并发插入不越界、消息只触发一次）
  - 新增 `test_every_tick_checks_local_sqlite`（验证每 tick 即时检查）
  - 新增 `test_validator_rejects_check_interval_seconds`（验证 validator 拒绝 check_interval_seconds）

## 验证

- QQ Automation 测试：48 passed
- Email Automation 回归：12 passed
- Automations + Checker + Consumer + QQ sync schedule：45 passed
- 完整 test suite：544 passed, 0 failed

## 已知问题与边界

无。
