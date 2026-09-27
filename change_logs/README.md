# 说明文档与修改日志

本目录集中保存项目说明和每次修改的记录；`logs/` 是本地运行日志目录，不用于版本化修改记录。以下说明中的命令和配置路径均以项目根目录为基准。

## 项目说明

| 文档 | 内容 |
| --- | --- |
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
| 2026-09-28 | [03：统一 Message 查询与选择导入](2026-09-28-03-unified-message-import.md) |
| 2026-09-28 | [02：文档归档与持续日志规则](2026-09-28-02-documentation-organization.md) |
| 2026-09-28 | [01：QQ 纯文字 Adapter、增量同步与去重](2026-09-28-01-qq-text-sync.md) |

## 后续记录方式

每次完成一项修改（含代码、配置、测试或文档），在本目录新增 `YYYY-MM-DD-NN-short-description.md`，日期使用 Asia/Shanghai，NN 为当天递增序号。同一项任务提交前的迭代更新同一日志，后续独立任务新增日志。参考 [日志模板](TEMPLATE.md)，记录目的、实际改动、验证结果、已知问题及提交关联，并更新本索引；日志与改动一起提交和推送。当前提交的哈希不必提前填写，可注明“与本日志同一提交”，通过 Git 历史查询。

保留固定位置的 Markdown：根目录 README 为导航入口；[AGENTS.md](../AGENTS.md) 为仓库规则；[prompt.md](../prompt.md) 为运行提示词；`tests/fixtures/` 内 Markdown 为测试数据。个人 Memory、生成文件、第三方依赖和本地运行日志不属于项目说明归档范围。
