# 2026-09-29：QQ 持久化周期同步

- 日期：2026-09-29（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

将上一阶段 checker 启动同步一次改为按持久化时间周期同步。自动只同步人工白名单，默认 600 秒，失败 60 秒重试；重启不重新计时，保持 Email 机制和手动全量 QQ 能力。

## 实际改动

- 删除 `qq_startup_sync`，由 `check_once()` 调用独立 `check_qq_sync_due()`，长期循环仍先获得现有锁。`--once` 与 `/automation check` 同样检查 QQ 到期状态。
- 在现有 QQ SQLite 中增加 `sync_state` 表，保存带时区的 `qq_next_sync_at`。首次立即尝试，未到期静默跳过，不读取白名单或访问 NapCat。
- 新增轻量 `qq_sync_schedule.py`，集中默认间隔、时区解析、事务到期判断及写入。到期先预留重试截止时间，网络前释放事务，完成后条件更新，避免覆盖期间手动同步产生的新截止时间；不增加数据库或文件锁。
- 成功、缺失/空白名单按正常间隔顺延；JSON/格式错误和同步失败按重试间隔顺延；缺失名单不回退全量。继续复用既有稳定身份过滤、永久 skip、checkpoint 和 rollback，不改变 EmailSource。
- 无过滤的手动全量同步成功后按完成时间更新截止时间；部分/整体失败不更新。配置读取支持新字符串字段 `QQ_SYNC_INTERVAL_SECONDS`、`QQ_SYNC_RETRY_SECONDS`，手动与自动均以本地配置覆盖环境变量。
- 更新当前 [QQ 使用说明](docs/QQ-STARTUP-SYNC.md) 和索引，保留原文件名以维持历史链接。原启动测试由周期测试替换；共享 Automation 测试夹具及子进程显式隔离 QQ 路径，避免读取个人数据。

## 验证

本任务最终执行：

```text
python -m pytest tests/test_qq_sync_schedule.py tests/test_qq_whitelist.py tests/test_qq_sync.py tests/test_qq_progress.py tests/test_qq_sample_reader.py tests/test_automation_checker.py tests/test_automation_email.py tests/test_automations.py tests/test_automation_consumer.py tests/test_phase5.py tests/test_agent.py -q
```

- 结果：**192 passed，10 subtests passed**，22.19 秒。
- 覆盖首次/未到期/到期、多个 tick 只在周期边界同步、真实新子进程重启保留截止时间、白名单/永久 skip、失败重试、缺失/空/非法配置、配置字段和环境优先级、手动成功/失败时间更新、并发到期事务与手动更新保护、时区、状态损坏、存储失败隔离、checker 锁与 `--once`，以及现有 Email/Timer/消费流程与手动 QQ 进度/跳过回归。
- 本次 `git diff --check`：通过。未引用历史测试结果作为本次验证。

## 已知问题与边界

- 有 2651 条来自现有 `agent/tools.py:231` 的 `PurePath.is_reserved()` 弃用警告，本阶段未修改该逻辑。
- 未运行全仓库测试，未连接真实 NapCat、未修改真实白名单或个人消息；真实网络和 10 分钟等待验收尚未执行。
- 截止时间为 QQ 源级单一状态，不按会话计时。自动以本轮检查时间计算截止，手动以完成时间计算；配置或名单变更在下一到期时生效。
- 长期实例由现有 checker 锁保护；重试截止的短时预留不提供超过重试间隔的跨入口全程网络互斥。数据库不可写时无法持久化退避，但不会使 Timer/Email 因 QQ 异常退出。
