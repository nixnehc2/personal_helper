# 第二阶段：定时检查与事件入队

> 本文记录检查器职责；手动调用 Agent 消费事件及展示回复见 [第三阶段说明](AUTOMATIONS-V3.md)。检查器仍不消费事件。

复用 `automations` 表及 JSON `pending_events` 字段，无需迁移或重建旧规则。
仅手动启动检查器；不调用 Agent、不连接邮箱、不发通知、不消费事件。
规则管理接口保持不变，聊天诊断命令直接调用检查器，不经过模型。

## 运行与诊断

```text
python -m agent.automation_checker --interval 10
python -m agent.automation_checker --once
```

第一条在前台循环运行，Ctrl+C 正常退出；第二条仅检查一次。
同一数据库的循环使用操作系统文件锁，第二个循环立即报错退出，崩溃后锁自动释放。
日志包含检查时间、入队/跳过/失败结果；单条规则失败不阻断其他规则。
聊天主程序不会自动启动循环。

现有 `python -m agent.main` 聊天中：

```text
/automation check
/automation pending
/automation pending 1
```

`pending` 只读取并显示待处理事件，不消费或删除它们；编号为规则编号。
这些命令不调用模型，聊天程序本身仍沿用原有启动配置。

`config.local.json` 或环境变量（配置文件优先）：

```json
{"AUTOMATION_CHECK_INTERVAL_SECONDS": 10, "AUTOMATION_TOLERANCE_SECONDS": 60}
```

`--interval` 覆盖检查间隔。容差为有限非负秒数，默认 60，边界包含 60 秒。
独立调用：`check_once(store, now=带时区datetime, tolerance_seconds=60)`；默认取 store 时钟。

## 进度与补执行

- `next_check_at`：下一次尚未处理的计划时间；`cursor`：最后处理（入队或跳过）的计划时间。
- `last_checked_at`：最近成功检查时间；成功清空 `last_error`，失败保留原进度并记录错误。
- 首次检查重复规则只建立当前及未来起点，不补历史：interval 可含恰好当前时刻，cron 从当前之后开始，与预览一致。
- once 始终按明确的 `at` 判断；已处理后用非空 cursor 区分结束与尚未初始化，入队后仍为 active。
- 同一批到期时间直接定位最近一次，`latest` 保留最近一次，其余跳过；`skip` 跳过超出容差的时间。如果最近一次仍在容差内则正常入队，`is_catch_up=false`。
- interval 始终按 start_at 的 UTC 间隔推进；cron 与预览共用时间计算器，沿用指定 IANA 时区及 croniter 6.0.0 的夏令时语义。
- once 因 skip 错过且没有待处理事件时可变为 completed；否则保持待处理状态。
- 暂停保留事件与进度；恢复按保留进度补查；取消清除 pending 事件并停止检查。
- 修改名称或 content 保留进度；替换为不同时间配置时，在修改事务内从修改时刻重建起点，保留既有事件快照。过去的 once 仍按错过策略处理。修改不恢复 cancelled/completed 状态。

每条规则在 SQLite `BEGIN IMMEDIATE` 事务中重读状态、配置及事件，按稳定事件 ID 去重，追加事件并仅更新运行字段。
事件 ID 为 `schedule:<规则编号>:<UTC计划时间>`，UTC 使用 `Z`，有必要时保留微秒。
content 是入队时快照；occurred_at/scheduled_at 为计划时间，detected_at 为检查时间。
事务失败整体回滚；多个并发检查也不会重复入队。错误记录失败会在检查结果中另行报告。

## 两分钟人工验收

1. 在项目目录 PowerShell 生成两分钟后的创建命令：

```powershell
$at = (Get-Date).AddMinutes(2).ToUniversalTime().ToString('o')
$rule = @{name='二阶段验收';trigger_type='schedule';content='提醒用户检查申请材料是否齐全。';trigger_config=@{schedule_type='once';at=$at;missed_policy='latest';timezone='Asia/Shanghai'}}
'/automation create ' + ($rule | ConvertTo-Json -Depth 5 -Compress)
```

2. 将输出命令粘贴到 `python -m agent.main`，记下规则编号。
3. 另开终端运行 `python -m agent.automation_checker --interval 10`。
4. 到期后在聊天输入 `/automation pending <规则编号>`，应只有一条 pending，reply/retry_at/last_error 为 null，attempts 为 0。
5. Ctrl+C 停止循环，再启动；再次查询并执行 `/automation check`，仍只有一条事件。
6. 验收后 `/automation cancel <规则编号>` 可清理这条验收规则的 pending 事件。

## 测试

```text
python -m unittest discover -s tests -p "test_automation*.py" -v
python -m unittest discover -s tests -v
```

使用临时数据库、可控时间；包含重启/并发去重、暂停竞争、容差和错过策略、时间修改、事务故障注入及前台循环退出验证，无真实等待和网络调用。
