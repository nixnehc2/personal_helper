# 2026-09-30：QQ Automation Event Source

- 日期：2026-09-30（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

在现有 Email Automation 框架上增加第二个消息来源 **QQ Automation Event Source**。所有 Email 和 QQ 可共用的部分（DB、Consumer、Event Runtime、Agent Session、once/continuous、pause/resume、retry、notification）全部复用，只新增 QQ-specific 的 validator、matcher、source、reader。

## 实际改动

### 新增文件

- `agent/automation_qq.py` — QQ event reader，通过统一 Message API 读取完整 QQ 消息内容（不修改 imported）
- `tests/test_automation_qq.py` — 45 个 QQ Automation 专属测试（validator、matcher、baseline、cursor、once/continuous、pause/resume、consumer/runtime、imported 不变）

### 修改文件

- `agent/qq_sync.py` — QQStore 新增 `rowid_cursor()` 和 `messages_after_rowid()` 方法，供 QQSource 内部扫描使用
- `agent/automation_triggers.py` — 新增 `qq()` validator，注册到 `EVENT_VALIDATORS`；支持 match（conversations/sender_ids/text_contains）和可选 check_interval_seconds
- `agent/automation_sources.py` — 新增 `matches_qq()` 匹配函数和 `QQSource` 类（prepare/check 接口与 EmailSource 一致，使用 rowid cursor）
- `agent/automation_checker.py` — 注册 QQSource 到 sources dict；将 `check_qq_sync_due` 移至 sources 循环前，确保同一 tick 内 QQ sync → QQSource 顺序执行
- `agent/automations.py` — resume 时扩展 cursor 重置支持 `source="qq"`
- `agent/event_runtime.py` — 增加 QQ source 分支调用 `read_event_qq`；泛化安全提示文本覆盖 QQ 消息
- `agent/tools.py` — 更新 automation tool 描述，增加 QQ source 文档

### 未修改（直接复用）

- `agent/automation_consumer.py` — QQ event 和 Email event 使用同一个 Consumer
- `agent/messages/models.py` — Message schema 不变
- `agent/messages/qq_adapter.py` — QQ 消息解析不变
- Automation DB schema — 不新增表

## 验证

- `tests/test_automation_qq.py`：45 passed
- `tests/test_automation_email.py`：12 passed（Email 回归无破坏）
- `tests/test_automations.py`：8 passed
- `tests/test_automation_checker.py`：14 passed
- `tests/test_automation_consumer.py`：12 passed
- `tests/test_qq_sync_schedule.py`：12 passed
- 完整 test suite：540 passed, 1 flaky（`test_submitted_user_has_priority_over_automatic_turn` 独立通过，为时序相关预存问题）

## 已知问题与边界

无。本次不实现：QQ scope、regex、WebSocket 监听、自动回复、发送消息、多消息聚合、图片 OCR、语音转写等（见开发计划第三十一节）。
