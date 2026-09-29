# 2026-09-30：waiting_for_user 不阻塞新 Automation 启动

- 日期：2026-09-30（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

修复 Automation 事件中 Agent 调用 call_for_user 后，后台消费者仍把该事件视为“正在执行的 Agent”，导致其他 pending Automation 无法启动的调度问题。

## 实际改动

### agent/event_runtime.py

- 新增 USER_WAIT_PHASES 常量：{waiting_feedback, waiting_for_user}
- 新增 locks_new_event(event) 函数：当 ctive_session 存在且 phase 不在 USER_WAIT_PHASES 中时返回 True
- 	ick() 中的 has_active 检查改为使用 locks_new_event()

核心原则：ctive_session 表示事件会话仍存在，phase 决定是否占用 Agent 执行资格。

### 新增测试 tests/test_waiting_for_user.py

10 个测试：
- locks_new_event() 单元测试（running/waiting_for_user/waiting_feedback/no_session/delivery）
- 	ick() 集成测试：waiting_for_user 不阻塞新事件、waiting_feedback 不回归、running 仍阻塞、内存锁阻塞/释放

## 验证

- 新测试 10/10 通过
- 全量测试 566 passed, 0 failed

## 已知问题与边界

- 未修改 Scheduler 串行 Agent 机制
- 未修改 Memory Transaction 独占规则
- call_for_user 等待期间持有 Memory lock 时仍会阻塞其他 Agent（通过 .memory-owner.lock 实现）
