# 第三阶段：手动消费事件与终端回复

仍使用同一张 `automations` 表及 `pending_events` JSON，无需迁移。
第二阶段检查器只负责入队，不执行 Agent；消费者不会自动启动。
本阶段不接入邮件来源、系统通知或开机自启。

## 执行方式

在现有 `python -m agent.main` 聊天中依次执行：

```text
/automation check
/automation pending
/automation consume
/automation pending
```

也可以另开终端运行 `python -m agent.automation_checker --interval 10` 负责入队，再回到聊天手动 consume。
consume 是直接管理命令，不注册为模型工具，不先经过模型解析。
它按规则编号、规则内事件保存顺序串行处理本轮开始时的 active 定时事件。
本轮新入队的事件和失败事件留给下次命令，不在本轮不断重试。
paused 规则保留事件，恢复后才能消费；cancelled 规则不消费。
同一数据库只允许一个消费者运行，操作系统文件锁随进程退出释放，检查循环可以同时运行。

## 回复与事务

- 事件在 `pending_events` 内即表示未完成，不引入 processing/ready 状态。保留第二阶段的 `status: pending` 兼容字段。
- `reply=null`：通过现有 `run_turn()` 执行指令快照，正常结束后提取最终文本，先保存 reply，再在终端展示。
- `reply` 已保存：直接展示，不调用模型；包括重启后的恢复。
- 展示输出刷新成功后，在同一事务中移除事件，并在适用时将一次性规则标记 completed。
- 若用户已修改为新的未来计划，不因旧事件完成而将新计划标记 completed；周期规则保留 active 状态。
- 模型、回复保存、展示或移除失败保留事件；错误存入事件 `last_error` 并返回结果。`attempts` 记录模型执行尝试次数；只重试展示不增加次数。
- 每次更新都在事务中重读最新事件列表，只修改选中的事件，不覆盖检查器追加的事件或用户配置。模型调用和终端确认期间不持有 SQLite 事务。
- Agent 运行期间暂停：保存已生成的回复，恢复后展示；取消则不恢复已经清除的事件。

每个事件使用独立 messages，不读取或写入当前聊天对话历史，也不继承当前活动邮件草稿 ID。
复用当前 FileTools、权限和 Temporary Memory；事件间及聊天仍共享既有 Temporary 事务。
Memory 合并、邮件发送继续要求原有 Runtime 确认，不默认批准。确认界面、工具日志照常输出；最终回复由消费者在持久化后输出。

## 验收

1. 按 [第二阶段流程](AUTOMATIONS-V2.md) 创建两分钟后的一次性“提醒检查申请材料”规则，等待检查器入队。
2. `/automation pending <编号>` 确认 reply 为 null；`/automation consume` 应生成并显示回复。
3. 再查 pending 应为空；`/automation get <编号>` 应为 completed；再次 consume 应显示处理 0 条。
4. 对周期规则做相同操作，成功后仍为 active，后续计划时间可继续入队。
5. 模型失败重试、展示故障后的跨进程恢复以及数据库故障回滚由下列离线测试验证，不需人为破坏真实数据库：

```text
python -m unittest discover -s tests -p "test_automation*.py" -v
python -m unittest discover -s tests -v
```

允许的崩溃边界：终端已经输出、事件尚未删除时崩溃，重启后可能重复展示一次；工具已执行、回复尚未保存时中断，重试可能再次执行工具。本阶段不提供工具副作用的严格去重。
