# 第四阶段：邮件来源

继续使用一张 `automations` 表与 JSON `pending_events`，旧规则无需重建。
检查器只检查和入队；用户通过 `/automation consume` 手动调用 Agent 并展示回复。
没有 QQ 聊天消息、系统通知、自动消费或开机自启。

## 使用方式

沿用 `config.local.json` 中的 `EMAIL_ACCOUNT`、`EMAIL_AUTH_CODE`、`EMAIL_IMAP_HOST`、`EMAIL_IMAP_PORT`。
配置文件优先于环境变量；默认服务器仍为 imap.qq.com，端口 993。规则账号必须对应本地配置，不要把授权码写入规则。

在 `python -m agent.main` 中创建规则，替换下面的账号及发件人：

```text
/automation create {"name":"申请回复总结","trigger_type":"event","source":"email","mode":"continuous","content":"总结这封新邮件关于申请材料的要求，并在终端展示；不要发送邮件。","trigger_config":{"scope":{"account_id":"你的本地邮箱账号","folder":"INBOX"},"match":{"from_addresses":["teacher@example.com"],"subject_contains":"申请"},"check_interval_seconds":30}}
/automation check
/automation get <规则编号>
```

第一次成功检查报告“邮件基线已建立”，不会触发邮箱内已有邮件。**从基线成功建立后开始监控**；创建、恢复到首次成功同步之间的邮件也纳入基线而跳过。失败时不建立基线，不会在重试时把历史邮件全部触发。

之后可另开终端运行：

```text
python -m agent.automation_checker --interval 10
```

发送一封同时满足发件人和标题条件的新邮件，等待规则的检查间隔后在聊天中执行：

```text
/automation pending <规则编号>
/automation consume
/automation pending <规则编号>
```

也可以不用循环，间隔到期后手动 `/automation check`。consume 处理本轮已有的所有启用规则事件。

## 匹配与进度

- `from_addresses`：解析 From 地址后精确匹配，忽略大小写；列表内任一地址可匹配，显示名不参与判断。
- `subject_contains`：对解码后的标题进行字面包含匹配，区分大小写。
- `reply_to_message_id`：例如 `<original@example.com>`，必须在 In-Reply-To 或 References 中作为完整 Message-ID 出现。不会依据 Re: 或同标题触发，也不会把原邮件自身当作回复。
- 多个条件之间 AND，至少填写一个条件；不调用模型判断。
- 同一检查轮中，相同账号/文件夹 scope 共用一次同步（不同文件夹分别同步），包括失败结果。遵守各规则 `check_interval_seconds`，未到期不发起同步。
- `cursor` 是 JSON，保存源标识及最后检查的本地索引 ID；`next_check_at` 是下一次允许轮询的时间，`last_checked_at` 是成功检查时间。与索引的 imported/imported_at 完全独立。
- 不匹配的邮件同样推进 cursor；同一邮件可触发不同规则。事件 ID 为 `email:<规则编号>:<本地邮件ID>`，消费后 cursor 仍保留，重复同步及重启不重新入队。
- 同一批事件追加与 cursor 更新在同一 SQLite 事务中保存；整体同步失败不推进进度，错误记入 last_error；部分邮件头失败记录并永久跳过，成功邮件继续匹配和推进规则进度。邮件失败不阻断时间来源。详见 [UID 增量同步](EMAIL-INCREMENTAL.md)。
- 暂停保留事件；恢复后重新同步建立基线，跳过暂停期间邮件。修改匹配条件保留 cursor，不回扫已检查邮件；修改邮箱 scope 重新建立基线。
- IMAP UIDVALIDITY 更换时保守地为新一代邮箱建立基线，避免无 Message-ID 的历史邮件被重新编号后误触发；该次同步中的邮件都跳过。现有索引会尽量通过 Message-ID 保留本地 ID。
- `mode=once` 只为首封匹配邮件入队，等待其消费成功后 completed；`continuous` 为所有新匹配邮件入队。

## 正文与消费

检查阶段仅通过 BODY.PEEK 获取邮件头，补充 In-Reply-To、References；不下载正文或附件，不导入 Memory。
事件保存指令快照、账号/主机/文件夹、本地 ID、UIDVALIDITY、UID、Message-ID、发件人、标题和邮件 Date。occurred_at/detected_at 为检测时间，原始邮件时间另保存在 data.date。

无 reply 时，消费函数用稳定标识定位邮件，复用现有 EML 下载/缓存和 MIME 解析，仅将可读正文交给 run_turn；消费阶段完整 EML 下载可能包含附件，但不会自动解压、导入或执行附件。校验或读取失败保留事件等待下次重试。
正文最多向模型提供既有解析器的 30000 字符，超出时带截断标记；HTML 不加载外部资源。
已有 reply 时，直接展示，不再读取正文或调用模型；展示成功移除，失败保留。
邮件头与正文明确作为不可信外部资料，不能覆盖规则指令或充当授权；Memory 合并和发送邮件仍需原有 Runtime 确认。

## 验收

1. 创建前放一封符合条件的邮件，首次成功 check 后 pending 应为空。
2. 基线后发送一封匹配、一封不匹配邮件；间隔到期 check 后只有匹配邮件入队，consume 应展示总结。
3. 再次 check/consume 并重启，已处理邮件不再出现。
4. 使用 `reply_to_message_id` 创建规则并建立基线；真正回复应匹配，普通同标题邮件不匹配。
5. 同步失败、正文失败、跨进程回复恢复、事务回滚和共享同步使用离线模拟验证，不修改真实邮箱：

```text
python -m unittest discover -s tests -p "test_automation*.py" -v
python -m unittest discover -s tests -v
```

第三阶段的崩溃边界不变：终端输出后、删除前崩溃可能重复展示；工具执行后、回复保存前中断可能再次执行工具。
