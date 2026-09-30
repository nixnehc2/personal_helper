# 说明文档与修改日志

本目录集中保存项目说明和每次修改的记录；`logs/` 是本地运行日志目录，不用于版本化修改记录。以下说明中的命令和配置路径均以项目根目录为基准。

## 项目说明

| 文档 | 内容 |
| --- | --- |
| [QQ 可读消息](docs/QQ-READABLE-MESSAGES.md) | text/@/reply、混合消息、CQ 与原始 segments |
| [QQ 周期同步与白名单](docs/QQ-STARTUP-SYNC.md) | 稳定会话身份、持久化周期、重试与手动同步 |
| [飞书 Phase 1 测试](../docs/feishu_phase1_test.md) | 第一阶段双向文本通信验证 |
| [飞书 Phase 2](2026-09-30-07-call-for-user-feishu-dual-channel.md) | call_for_user 双通道接入 |
| [General Import Agent](docs/GENERAL-IMPORT.md) | 单条外部消息进入正常 Agent、共享工具与处理状态 |
| [完整使用说明](docs/README.md) | 启动、配置、Memory、文件读写、Email、Automation、QQ |
| [统一 Message 第三阶段](docs/MESSAGE-V3.md) | 查询、查看、选择导入与 imported/Memory 完成边界 |
| [Email V1](docs/EMAIL-V1.md) | 邮件功能与导入流程 |
| [Email 增量同步](docs/EMAIL-INCREMENTAL.md) | UID 增量同步与失败处理 |
| [Automation V1](docs/AUTOMATIONS-V1.md) | 规则保存与管理 |
| [Automation V2](docs/AUTOMATIONS-V2.md) | 调度检查与事件入队 |
| [Automation V3](docs/AUTOMATIONS-V3.md) | Agent 消费事件 |
| [Automation V4](docs/AUTOMATIONS-V4.md) | 邮件事件来源 |
| [Automation V5](docs/AUTOMATIONS-V5.md) | 自动消费、事件终端与通知 |

历史阶段说明保留原文；当前行为以完整使用说明及较新阶段文档为准。

## 修改日志（较新在前）

| 日期 | 记录 |
| --- | --- |
| 2026-10-01 | [01：complete_event 参数校验拆分与 message 别名兼容](2026-10-01-01-complete-event-parameter-validation.md) |

| 2026-10-01 | [02：Automation Turn Protocol Violation Detection and Recovery](2026-10-01-02-automation-protocol-violation-recovery.md) |
| 2026-10-01 | [03：Fix Runtime Recovery Control Flow](2026-10-01-03-fix-runtime-recovery-control-flow.md) |
| 2026-10-01 | [04：Fix call_for_user Resume and Feishu Wake-up](2026-10-01-04-fix-call-for-user-resume-and-feishu-wakeup.md) |
| 2026-10-01 | [05：Replace Blocking input() with Pollable Keyboard](2026-10-01-05-replace-blocking-input-with-pollable-keyboard.md) |
| 2026-09-30 | [07：call_for_user 飞书双通道接入](2026-09-30-07-call-for-user-feishu-dual-channel.md) |
| 2026-09-30 | [06：飞书接入第一阶段 — 双向文本通信验证](2026-09-30-06-feishu-phase1-bidirectional.md) |
| 2026-09-30 | [05：waiting_for_user 不阻塞新 Automation 启动](2026-09-30-05-waiting-for-user-scheduling.md) |
| 2026-09-30 | [04：call_for_user Automation 工具](2026-09-30-04-call-for-user-automation.md) |
| 2026-09-30 | [03：架构简化与权限模型统一](2026-09-30-03-architecture-simplification-unified-permissions.md) |
| 2026-09-30 | [02：QQ Automation 三项修正](2026-09-30-02-qq-automation-fixes.md) |
| 2026-09-30 | [01：QQ Automation Event Source](2026-09-30-01-qq-automation-event-source.md) |
| 2026-09-29 | [05：允许空邮件匹配条件](2026-09-29-05-allow-empty-email-match.md) |
| 2026-09-29 | [04：Memory 默认提交与邮件非阻塞确认](2026-09-29-04-nonblocking-email-approval.md) |
| 2026-09-29 | [03：QQ 混合消息可读内容适配](2026-09-29-03-qq-readable-segments.md) |
| 2026-09-29 | [02：General Import Agent](2026-09-29-02-general-import-agent.md) |
| 2026-09-29 | [01：QQ 持久化周期同步](2026-09-29-01-qq-persistent-sync-schedule.md) |
| 2026-09-28 | [08：QQ 稳定身份白名单与启动同步](2026-09-28-08-qq-startup-whitelist.md) |
| 2026-09-28 | [07：复查并提交 QQ 进度与会话跳过](2026-09-28-07-review-pending-qq-changes.md) |
| 2026-09-28 | [06：Message 通用关键词搜索 V1](2026-09-28-06-message-keyword-search.md) |
| 2026-09-28 | [05：Message 数据库分页与 Email SQLite 迁移](2026-09-28-05-message-sql-pagination.md) |
| 2026-09-28 | [04：QQ 同步跳过会话功能](2026-09-28-04-qq-skip-conversation.md) |
| 2026-09-28 | [03：统一 Message 查询与选择导入](2026-09-28-03-unified-message-import.md) |
| 2026-09-28 | [02：文档归档与持续日志规则](2026-09-28-02-documentation-organization.md) |
| 2026-09-28 | [01：QQ 纯文字 Adapter、增量同步与去重](2026-09-28-01-qq-text-sync.md) |

## 后续记录方式

每次完成一项修改（含代码、配置、测试或文档），在本目录新增 `YYYY-MM-DD-NN-short-description.md`，日期使用 Asia/Shanghai，NN 为当天递增序号。同一项任务提交前的迭代更新同一日志，后续独立任务新增日志。参考 [日志模板](TEMPLATE.md)，记录目的、实际改动、验证结果、已知问题及提交关联，并更新本索引；日志与改动一起提交和推送。当前提交的哈希不必提前填写，可注明“与本日志同一提交”，通过 Git 历史查询。

保留固定位置的 Markdown：根目录 README 为导航入口；[AGENTS.md](../AGENTS.md) 为仓库规则；[prompt.md](../prompt.md) 为运行提示词；`tests/fixtures/` 内 Markdown 为测试数据。个人 Memory、生成文件、第三方依赖和本地运行日志不属于项目说明归档范围。
