# 2026-09-28：Message 数据库分页与 Email 独立 SQLite 迁移

- 日期：2026-09-28（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

解决 list_messages 全量载入消息、Python 筛选排序后截取的问题。默认只返回最近 20 条，支持来源筛选及 LIMIT/OFFSET。用户补充授权将 Email 原 JSON 存储迁移至独立 SQLite。

## 实际改动

- `agent/messages/pagination.py`、`sources.py`、`__init__.py`：在来源数据库中进行 conversation/imported/时间过滤；跨来源通过 ATTACH + UNION ALL 查询，SQL 排序、LIMIT/OFFSET 后才解析本页 JSON。完整整数 ID 使用十进制文本长度及字典序排序，日期标量函数保留时区与微秒语义。
- `agent/email_index.py`：Email 独立 SQLite 保存原行及同步元数据；首次访问事务性迁移旧 JSON，保留原文件作为备份，迁移失败可重试。原同步、单条查询的快照接口与 ID 查找方式保持兼容。
- `agent/email_drafts.py`：草稿原先借用 EmailIndex 的 JSON 写入，现保留独立的原子 JSON 写入，草稿格式不迁移。
- `agent/messages/email_adapter.py`：更新存储说明，Message 公共字段及 adapter 映射不变。
- `agent/tools.py`、`agent/main.py`：source 可省略或为 null，limit 默认 20、范围 1~100，offset 默认 0、范围 0~SQLite 最大有符号整数；无效值报错。支持无参数命令及 source/limit/offset 位置参数，兼容原 JSON 过滤条件；返回本页条数与 offset，不查询总数。
- `tests/test_message_pagination.py`：默认分页、单来源和跨来源分页、参数校验、时区/微秒/未知日期、巨大 ID、过滤后分页、命令及工具分派、万条数据 SQL/解码 spy、迁移保真和失败回滚。
- `tests/test_email_index.py`、`test_email_incremental.py`、`test_email_entrypoints.py`、`test_message_workflow.py`：适配 SQLite 持久化及新的无参数命令。
- `change_logs/docs/MESSAGE-V3.md`、`change_logs/docs/README.md`：更新参数、命令、存储位置和迁移/性能边界；更新日志索引。

## 验证

- 本次运行 `python -m unittest discover -s tests -p test_message_pagination.py -v`：13 项全部通过。
- 本次运行 `python -m unittest discover -s tests -p test_email_import.py`：20 项全部通过。
- 早期消息回归 96 项通过；后续增加日期和跨来源并列排序测试，以最终全量结果为准。
- 早期 Email 回归发现草稿复用 JSON 写入及工具 nullable 参数校验不兼容，已修复；初始失败不计为通过。
- 最终全量回归：`unittest.defaultTestLoader.discover("tests")` + `TextTestRunner(verbosity=2)`，增加每用例 45 秒 faulthandler 超时堆栈保护；当前工作区 431 项全部通过，112.661 秒，无超时。覆盖 Message、Email、Agent、Memory、Automation、QQ 和文件读写。
- 首次标准 `python -m unittest discover -s tests` 在前部用例长时间未推进，已终止该次运行；独立 Agent 25 项与上述完整重跑均通过，未修改无关调度代码。
- 大数据验收：各 10,005 条 QQ/Email，分别验证 qq、email、跨来源以 limit=20、offset=5000 查询，均仅调用 20 次 JSON 解码，实际 SQL 包含 ORDER BY 和 LIMIT 20 OFFSET 5000；禁止调用旧全量列表/快照读取路径。
- `git diff --check`：通过。

## 已知问题与边界

- 首次迁移必须一次性读取旧 JSON；迁移完成后列表只从 SQLite 返回当前页，旧 JSON 不再同步更新。
- 没有增加搜索索引。SQLite 仍可能扫描、排序匹配记录，深 offset 存在扫描成本；时间标量函数处理日期字段，但不会解析整条消息。
- 同步及 read_message 的现有快照/ID 查找方式未优化；本次性能保证针对 list_messages。
- 测试出现现有 `PurePath.is_reserved()` 弃用告警，不影响结果。
- 保留用户已有 QQ 同步跳过会话等未提交改动，不纳入本任务提交。
