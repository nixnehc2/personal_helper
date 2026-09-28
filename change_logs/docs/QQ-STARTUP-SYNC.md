# QQ 启动同步与人工白名单

Email 继续由现有 Automation 的 `next_check_at` 和 `check_interval_seconds` 控制同步频率。QQ 只在长期 `automation_checker` 取得循环锁后同步一次，然后进入原有 Timer/Email 检查循环；进程内不重试 QQ，不增加 QQ 事件来源或监听框架。

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

启动日志展示匹配的数量及会话，之后只运行原有 Automation 检查。修改白名单后需重新启动 checker 才生效。`python -m agent.automation_checker --once` 和 `/automation check` 均不触发 QQ 同步。

## 边界

- 缺失、空或损坏的白名单：跳过 QQ，继续 checker，绝不回退到全量同步。
- 未找到配置的会话：告警并继续其他匹配会话，不删除或重写白名单。
- NapCat 连接或同步失败：记录失败，继续 checker；下次进程启动才再次尝试。
- `conversation_states=skip` 优先于白名单；未选会话的 Message、checkpoint 和 skip 状态不变。
- 手动 `/update_qq` 及 Agent 的同名工具不读取此白名单，仍同步所有未永久跳过的会话，保留进度、Enter skip、rollback 和 checkpoint。
- 白名单仅由人工 selector 配置，不新增 Agent 修改工具，checker 不修改它。现有 Agent 文件工具限于 Memory 根目录。
- 白名单属于当前本地 QQ 配置；checkpoint 继续使用账号 + 类型 + ID 的既有 scope，没有新增 UUID 或映射数据库。

自动化验证全部使用虚构会话和模拟 NapCat；真实连接、实际列表显示与交互保存仍需在本机运行上述命令验收。
