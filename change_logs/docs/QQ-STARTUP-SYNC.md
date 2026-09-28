# QQ 周期同步与人工白名单

Email 继续由现有 Automation 的 `next_check_at` 和 `check_interval_seconds` 控制同步频率。QQ 在每次 `check_once()` 中检查独立的持久化 `qq_next_sync_at`，仅到期才同步白名单。默认成功后 10 分钟、失败后 1 分钟再尝试，不需要创建 Automation Rule，也不增加 QQ 事件来源或通用监听框架。

本说明已更新为周期同步（2026-09-29）；旧文档文件名保留以维持历史链接。上一阶段“每次启动同步一次”的行为已删除。

## 配置与使用

先启动并登录 NapCat，沿用本地 `QQ_API_URL`、`QQ_ACCESS_TOKEN` 配置，在项目根目录运行：

```bash
python -m agent.qq_sync_selector
```

脚本列出群聊和私聊（群聊优先，同类按名称排序，私聊优先显示备注），输入 `1,3,4` 或 `1 3 4`。数字仅是本次列表编号。非法编号会报错并要求重新输入，重复编号会去重；留空可清空自动同步选择。脚本展示最终会话，只有输入 `yes` 才整体替换配置，其他回答或取消均保留旧文件。

配置保存于本地 `data/qq/sync_conversations.json`，示例仅含虚构身份：

```json
{
  "conversations": [
    {"type": "group", "id": "100", "name": "示例群"},
    {"type": "private", "id": "300", "name": "示例好友"}
  ]
}
```

只以 `(type, str(id))` 匹配。名称是供人查看的快照，不参与匹配；改名或新增其他好友导致编号变化都不影响已选身份。脚本使用同目录临时文件完整写入、flush/fsync 后原子替换；写入或替换失败保留旧配置。配置已由现有 `.gitignore` 排除，不提交个人会话名单。

然后运行：

```bash
python -m agent.automation_checker --interval 10
```

每轮 checker 可以间隔 10 秒，QQ 网络同步默认间隔 600 秒。首次没有调度状态时立即同步；之后读取 `data/qq/messages.sqlite3` 中 `sync_state` 表的 `qq_next_sync_at`（带时区 ISO 时间，存储为 UTC）。例如 00:00 同步后下次为 00:10，00:04 重启 checker 不会重新计时或立即同步。未到期时只检查 SQLite，不读取白名单、不连接 NapCat，也不输出 QQ 跳过日志。

在现有私有 `config.local.json` 中可配置（checker 同时支持同名环境变量，本地配置优先）：

```json
{
  "QQ_SYNC_INTERVAL_SECONDS": "600",
  "QQ_SYNC_RETRY_SECONDS": "60"
}
```

沿用项目配置格式，两项以字符串填写，内容必须为有限正秒数，默认值集中在 `agent/qq_sync_schedule.py`。修改间隔不会改写已有截止时间，下一次到期后使用新间隔。修改白名单在下一次到期时生效，无需重启；selector 不修改调度状态。

`python -m agent.automation_checker --once` 和 `/automation check` 现在同样执行 QQ 到期检查：到期可触发网络同步，未到期不会触发。自动同步不读取 stdin，不显示 Enter skip 提示。

## 成功、失败与手动同步

- 自动成功：以本次检查时间为基准设置 `next = now + QQ_SYNC_INTERVAL_SECONDS`。
- NapCat/HTTP/登录失败或任何会话同步失败：记录错误，设置 `next = now + QQ_SYNC_RETRY_SECONDS`，不在本轮循环重试，Timer/Email 正常继续。
- 缺失或空白名单：跳过网络同步，按正常间隔顺延，避免每个 tick 重复告警；绝不回退到全量同步。
- 白名单 JSON 或格式错误：明确记录文件错误，按失败间隔重试。
- 未找到配置的会话：告警并继续其他匹配会话，不删除或重写白名单；无其他失败时按正常间隔顺延。
- `conversation_states=skip` 优先于白名单；未选会话的 Message、checkpoint 和 skip 状态不变。
- 手动 `/update_qq` 及 Agent 的同名工具不读取此白名单，仍同步所有未永久跳过的会话，保留进度、Enter skip、rollback 和 checkpoint。
- 手动全量同步无失败时，以完成时间顺延正常间隔；整体或部分失败时保留原截止时间。显式永久跳过仍视为正常操作，不计失败。调度写入失败会在同步结果中报告，不回滚已提交消息。
- 白名单仅由人工 selector 配置，不新增 Agent 修改工具，checker 不修改它。现有 Agent 文件工具限于 Memory 根目录。

## 状态与并发边界

`sync_state` 与消息复用一个 SQLite 数据库，不新增数据库或 QQ 文件锁。长期 checker 仍先取得现有循环锁。到期判断与预留重试时间在一个 SQLite 事务中完成，网络操作前释放事务；进程异常退出后，预留时间到期即可重试。同步结束用条件更新保存下一时间，避免覆盖期间手动同步写入的新截止时间。

状态是本地 QQ 源级单一调度，不按会话分别计时；checkpoint 继续使用账号 + 类型 + ID 的既有 scope。数据库不可写或间隔配置无效时记录调度错误并继续 Timer/Email；无法写入时无法保证持久化退避。现有循环锁保护长期实例；短时到期预留不提供超过重试间隔的跨入口全程网络互斥。

## 人工验收

运行 selector 选择会话，再启动 checker。确认首次同步后 10 分钟内没有 QQ 网络同步，10 分钟后再次同步；在保存的截止时间之前重启 checker，确认仍按原截止时间执行。也可在本地配置较短 QQ 间隔做验证，注意它独立于 `--interval`。

自动化测试使用虚构会话、模拟 NapCat 与临时 SQLite，包括真实子进程重启后的截止时间保留。未连接真实服务或修改个人白名单；真实连接及交互仍需按上述步骤验收。
