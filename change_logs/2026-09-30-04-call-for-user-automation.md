# 2026-09-30：call_for_user Automation 工具

- 日期：2026-09-30（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

在 Automation 独立事件会话中新增 call_for_user(prompt) 工具，让 Agent 在需要用户输入时显式调用，不再依赖隐式文本等待。

## 实际改动

### 1. agent/tools.py

- tool_specs 属性：Automation session 同时注册 call_for_user 和 complete_event
- _execute() 新增 call_for_user 处理：验证 event_session、prompt 非空、与 complete_event 互斥
- __init__ 初始化 event_complete、event_reply、call_for_user_active、call_for_user_prompt
- complete_event 新增与 call_for_user 互斥检查

### 2. agent/main.py

- 终端动作检查扩展：complete_event 和 call_for_user 都必须单独调用
- run_turn 工具循环末尾新增 call_for_user_active 检查，提前返回 status=call_for_user

### 3. agent/event_runtime.py

- 事件会话 prompt 更新：明确 call_for_user 语义和与 complete_event 的互斥关系
- run_turn() 返回后新增 call_for_user 处理分支：生成 user_call_id、更新 phase、显示 prompt、用户输入循环、恢复 session
- 新增协议警告：Automation turn 未调用 call_for_user 或 complete_event 时记录 warning

### 4. 新增测试 tests/test_call_for_user.py

14 个测试覆盖：普通会话无 event 工具、Automation session 拥有两个工具、call_for_user 终止 turn、互斥检查、普通会话拒绝、空 prompt 拒绝、多轮 call_for_user、processing_message/read_only 阻止、Automation 权限链

## 验证

- 新测试 14/14 通过
- 全量测试 557 passed, 0 failed
- 旧的 Automation/Phase5/Agent 测试无回归

## 已知问题与边界

- V1 不支持进程重启后恢复 call_for_user 等待状态
- send_email 的 Runtime yes/no 确认机制不变
- Memory lock 在 call_for_user 等待期间保持
