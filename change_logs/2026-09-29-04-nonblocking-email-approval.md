# 2026-09-29：Memory 默认提交与邮件非阻塞确认

- 日期：2026-09-29（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

保留 Temporary Memory 的事务机制，取消默认提交时的人工等待；邮件仍须明确批准，但等待期间释放 Agent 执行权，允许其他会话和事件运行。

## 实际改动

- `agent/main.py`：默认 commit 确认返回批准，self 批次默认接受；discard 的确认语义保留。邮件请求结束当前模型轮次，同批剩余工具返回未执行结果。人工输入循环在锁外处理 yes/no，运行时重新取得 execution lock 后发送，并记录反馈到会话。
- `agent/tools.py`：会话内 `pending_email_send` 保存完整草稿快照；send_email 只请求发送，不读取输入、不调用 SMTP；活动 Memory 事务必须先提交或取消，待发送邮件未处理时禁止 complete_event。
- `agent/email_send.py`：拆分 request_email_send / send_email_confirmed；后者未注册为工具。批准时重新检查草稿状态和快照，变化则拒绝发送；草稿锁只覆盖读取或实际发送，不跨用户等待。
- `agent/event_runtime.py`：复用 waiting_feedback；确认输入发生在 run_turn 返回之后；yes 重新进入 running、加锁发送，结果作为普通反馈继续；退出清除会话 pending。
- `agent/email_workflow.py`、工具说明和使用文档同步默认提交语义。`agent/memory.py` 无需改动，原校验、Temporary 合并、回滚和锁释放继续复用；原注入式确认回调接口保留以兼容调用方和故障测试，默认运行入口不再等待 Memory 审批。
- 更新 `tests/test_agent.py`、`test_automation_consumer.py`、`test_email_memory.py`、`test_email_send.py`、`test_general_import.py`、`test_phase5.py`：覆盖新默认值、两种入口、真实执行锁、第二事件执行、快照变化、非法输入/no/退出、SMTP 失败及内部路径不可公开调用。

## 验证

- 开发中旧测试曾因旧审批预期失败，已按新行为更新；新增测试的临时对象弱引用和模拟启动参数问题已修复。
- 本次定向：设置 `PYTHONUTF8=1`、`PYTHONPATH=tests`，执行 `python -m unittest test_email_send test_phase5 test_agent test_email_memory test_general_import test_automation_consumer`，106 项通过（36.810 秒）。
- 本次完整测试：`PYTHONUTF8=1 python -m unittest discover -s tests`，495 项通过（77.444 秒）。测试中的故障注入日志和既有弃用警告不影响结果。
- `git diff --check`：通过。

## 已知问题与边界

- SMTP 使用离线模拟，未向真实收件人发送邮件；不验证真实邮箱服务。
- pending 仅在会话内保存，不支持重启恢复；退出前未批准的草稿保持 draft。
- 保留现有 Memory 并发原则，因此请求邮件批准前要求结束活动 Memory 事务。
- 未引入审批数据库、队列或新的全局调度状态。
