# 2026-10-01：Automation Turn Protocol Violation Detection and Recovery

- 日期：2026-10-01（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

修复 Automation 独立事件会话中，Agent 执行任务后忘记调用 `complete_event` 或 `call_for_user`，导致事件一直处于 pending 状态的问题。

修复前，如果 Agent 只输出普通文本而没有调用结束工具，事件会一直挂起，用户后续输入会被误解为该事件的下一轮输入。

## 实际改动

### `agent/event_runtime.py`

1. **新增 `classify_automation_turn()` 函数**：纯函数，检查结构化解析状态（不解析自然语言），返回 turn 结果分类：
   - `completed` — 事件已完成（`event["reply"]` 或 `session.event_complete`）
   - `waiting_for_user` — 等待用户输入（phase 为 `waiting_feedback` 或 `waiting_for_user`）
   - `waiting_for_approval` — 等待审批（`pending_email_send` 或 `pending_approval`）
   - `protocol_violation` — 无合法终止状态

2. **新增 `MAX_PROTOCOL_RECOVERY_ATTEMPTS = 1`**：限制 recovery 最多尝试 1 次

3. **在 `while True` LLM 循环结束后插入 protocol violation 检查**：
   - 先调用 `classify_automation_turn()` 判断结果
   - 如果是 `protocol_violation`，向 `messages` 注入一条 Runtime 指令，要求 Agent 立即调用 `complete_event` 或 `call_for_user`
   - 再次调用 `run_turn()` 执行 recovery
   - 恢复后检查新状态，如果已完成则 return
   - 如果 recovery 失败，记录日志并落入原有的等待用户反馈流程

4. **日志输出**：
   - `[automation] protocol_violation event_id=xxx`
   - `[automation] recovery_attempt=1 event_id=xxx`
   - `[automation] protocol_recovered event_id=xxx outcome=completed`
   - `[automation] protocol_recovery_failed event_id=xxx`

### `tests/test_automation_protocol_violation.py`（新文件）

12 个测试：

- **`TestClassifyAutomationTurn`（7 个）**：单元测试纯函数分类逻辑（reply、session flag、phase、email approval、generic approval、violation、priority）
- **`TestRecoveryConstants`（1 个）**：验证 MAX_PROTOCOL_RECOVERY_ATTEMPTS = 1
- **`TestProtocolViolationRecovery`（4 个）**：集成测试
  - 正常 complete_event → 不触发 recovery
  - Agent 忘记 complete_event → recovery → 成功完成
  - Recovery 失败 → 落入等待用户反馈
  - 已有工具调用结果不重复执行

## 验证

- 命令：`python -m unittest tests.test_automation_protocol_violation tests.test_complete_event tests.test_call_for_user`
- 实际结果：37 个测试全部通过
- 历史测试（test_complete_event, test_call_for_user）无回归

## 已知问题与边界

- `test_waiting_for_user` 有预存的导入错误（`ModuleNotFoundError: test_automations`），与本次改动无关
- Recovery 的指令文本包含中文，可能在某些终端显示乱码，但文件内容为正确 UTF-8
- 不修改数据模型、QQ 同步、Email 触发逻辑或并发调度架构

## 提交关联

与本日志同一提交
