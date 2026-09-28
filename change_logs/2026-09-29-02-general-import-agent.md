# 2026-09-29：General Import Agent

- 日期：2026-09-29（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

将 `import_message` 从 Memory 导向导入升级为“用户选择单条外部 Message → 正常 Agent”，统一 Email/QQ，不新增监听、自动导入、自动发送、thread 批量注入或工具注册体系。

## 实际改动

- `agent/messages/importing.py` 新增无模型 loader、结构化 External Message JSON、本轮 ImportSession 与单条锁管理。直接命令调用正常 `run_turn()`；已有 Agent 工具调用只返回 loaded 内容，原 Agent 继续当前轮，没有嵌套 run_turn。
- 来源适配由 `process()` 改为 `import_content()`：QQ 保留结构化源内容，Email 保留索引身份并读取现有验证缓存、解析 MIME 正文和附件元数据；不再强制走 Memory 归档。人工本地 `/email` 旧流程保留。
- 正常聊天与 General Import 共用 `run_turn`、FileTools 和 TOOLS。通用外部数据规则进入可信系统说明，来源字段只进入 JSON 数据或 tool_result；不默认注入 thread，需要时用既有 list/search/read_message 查询上下文。
- imported 明确为“已完成 Agent 本轮处理”，不等待 Memory commit，不因 review no、cancel 或关闭会话而撤销。无动作也成功；工具错误、模型异常、截断、步骤耗尽及标记失败不宣称成功。保留防重、身份校验和每条导入锁，锁在本轮结束后释放。
- Memory 候选仍走现有事务，邮件发送仍要求 Runtime 确认，文件和只读权限不降低。没有修改 Email/QQ 同步与 checker 职责。
- 新增 `tests/test_general_import.py`，更新 Message/Email 导入测试的旧状态和嵌套 Agent 断言。新增 [详细设计与人工验收](docs/GENERAL-IMPORT.md)，更新根导航、完整说明和历史 Message 文档提示。

## 验证

本任务实际执行的回归：

```text
python -m pytest tests/test_general_import.py tests/test_message_workflow.py tests/test_email_import.py tests/test_email_memory.py tests/test_email_drafts.py tests/test_email_send.py tests/test_file_reader.py tests/test_file_writer.py tests/test_memory_transaction.py tests/test_agent.py -q --tb=short
```

- **162 passed，41 subtests passed**，79.42 秒。覆盖单条来源、无动作、Automation/文件工具、按需上下文、恶意文本数据边界、单一 Agent 轮次、异常重试、防重、Memory 事务、邮件发送确认、文件边界与正常聊天。

```text
python -m pytest tests/test_general_import.py tests/test_automation_consumer.py tests/test_phase5.py tests/test_context_visibility.py tests/test_debug_logger.py tests/test_message_read_layer.py tests/test_message_email_adapter.py tests/test_message_pagination.py tests/test_message_search.py -q --tb=short
```

- **165 passed，58 subtests passed**，12.89 秒。含 General Import 重跑，验证共享 run_turn 对消费者、上下文、日志及查询的兼容性。
- 最后更新测试命名后复跑 `python -m pytest tests/test_message_workflow.py tests/test_email_import.py -q --tb=short`：**42 passed，12 subtests passed**，7.51 秒。
- `git diff --check`：通过。以上是本任务实际执行结果，未引用历史通过记录。
- 实现过程中旧 Memory-commit/嵌套 Agent 断言曾失败，已按新契约更新并通过。一个并发 Message 锁测试先被全局 Agent 锁阻塞，已隔离测试中的全局入场并终止卡住的测试进程，最终测试全部完成。

## 已知问题与边界

- 测试仍有现有 `PurePath.is_reserved()` 弃用警告，涉及 tools/file_reader/file_writer，未在本任务扩展修改。
- 未运行全部测试文件，未访问真实模型、NapCat 或个人邮件/Memory；自动测试不证明真实模型每次会选择正确行动，详细文档提供人工验收。
- 外部副作用与 imported 不是跨存储原子事务：副作用完成后模型失败或状态写入失败时，重试可能重复，必须先检查现有提醒/文件/草稿/Memory。本阶段没有自动重试或通用幂等操作账本。
- 工具错误采用保守策略：当前轮出现执行错误即不标记成功；历史 imported=true 不重置。状态仍使用现有字段，不迁移数据库。
- 本地 `/email <path>` 保留原 EML 专用行为；General Import 使用索引 Message ID。Email thread 没有新建聚合系统，按现有查询工具补充上下文。
