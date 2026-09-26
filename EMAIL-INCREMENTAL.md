# IMAP UID 增量同步（Automation V4 优化）

聊天 `/update_email`、Agent `update_email()`、独立 `python -m agent.email_index` 和自动化检查器都复用 `update_email_index()`。不新增任务系统、数据库、常驻 IMAP 连接或第五阶段功能。

## 索引与同步

仍保存 `data/email/index.json`（本地私有文件，不提交 Git），保留原本的本地 ID、imported/imported_at 和邮件头字段。在 version 1 索引上增加：

- `sync_progress`：以 JSON 编码的 `[host, account, folder]` 为键，主机和账号统一小写；值包含 `uidvalidity`、`max_uid`、UTC `completed_at`。
- `sync_failures`：追加失败记录，每条包含 host/account/folder、uidvalidity、imap_uid、UTC failed_at、error_type。错误类型使用固定标签，不保存服务器原始错误或授权码。

同步进度控制网络读取范围；Automation 的每条规则 cursor 控制哪些本地邮件已检查，二者互不替代。imported 只表示原有邮件导入流程完成，也不参与触发去重。

首次同步、没有可信进度或 UIDVALIDITY 改变时，执行 `UID SEARCH ALL`，分批获取邮件头。**旧索引绝不直接以已有最大 UID 初始化进度**：先全量获取，补齐 In-Reply-To、References，再建立进度。

后续执行 `UID SEARCH UID <max_uid+1>:*`，客户端再次严格过滤 `UID > max_uid`。这是为了排除服务器对反向范围 `N:*` 返回旧最大 UID 的情况。无新 UID 时不执行 FETCH。每批最多 100 个 UID，仅获取 BODY.PEEK 邮件头，不下载正文或附件。

UIDVALIDITY 改变后仍按同范围 UID 身份、非空 Message-ID 的既有规则合并，尽量保留本地 ID 和导入状态；无 Message-ID 的邮件仍无法跨 UIDVALIDITY 可靠识别。合并使用字典查找，避免对每封新邮件遍历全部索引。远端删除仍不删除本地历史记录。

## 失败策略：记录、跳过、不自动补取

- 单封未返回邮件头：记录 `missing_header`；解析失败：记录 `parse_error`。
- 批量 FETCH 返回失败或抛出传输错误：该批全部记录 `fetch_failed`，继续后续批次。
- 成功完成 UID 查询后，本轮最大 UID 包含失败 UID。已跳过的 UID 下一轮不会自动获取，不新增重试队列；未进入索引的失败邮件也不会触发规则。
- 连接、登录、选择文件夹、UIDVALIDITY 获取或 UID 查询失败时，无法确认本轮范围，整个同步报错，不写新进度。
- 索引、失败记录、同步进度在同一个 JSON 中，使用临时文件、fsync、原子替换一同提交。保存失败保留完整旧文件，不提交新进度。
- 同步从读取进度到提交均持有原有索引文件锁，其他同步入口不能覆盖进度。异常退出遗留 index.lock 时，确认没有进程使用索引后才可手工删除锁。

部分失败不再使 V4 整轮检查失败：成功邮件正常匹配入队，各规则 cursor 正常推进。整体失败仍不推进规则 cursor。首次基线、暂停恢复、同一轮按 scope 共享同步、消费后的事件去重保持不变。

UIDVALIDITY 改变或缺失可信进度会触发全量同步，可能再次遇到以前跳过的 UID；正常增量轮次不会主动重试。消费阶段已入队事件的正文读取或 Agent 失败重试不受此策略影响。

## 统计与验收

同步返回并显示成功数、跳过数及失败记录位置 `data/email/index.json#sync_failures`。另返回：

- `mode`：full / incremental。
- `elapsed_seconds`：调用耗时，含网络、保存及结果格式化。
- `queried_uid_count`：服务器查询实际返回的 UID 个数，包含随后过滤的旧边界 UID。
- `eligible_uid_count`：过滤去重后的新增 UID 个数。
- `fetched_header_count`：实际收到的所请求邮件头数量，包含收到但解析失败的邮件头。
- `success_count`、`skipped_count`：成功解析数、记录并跳过数。

```text
python -m unittest discover -s tests -p "test_email_incremental.py" -v
python -m unittest discover -s tests -v
```

模拟测试涵盖无新邮件不 FETCH、只取新增、重启、单封/整批失败跳过、整体失败、原子保存失败、UIDVALIDITY 变化、旧索引升级和 V4 部分成功。真实邮箱可连续执行两次同步；第二次无新邮件时 fetched_header_count 应为 0，即使查询返回一个旧边界 UID。

本次真实邮箱连续同步验收（不含邮件内容及账号）：

| 次数 | 模式 | 耗时（秒） | 查询返回 UID 数 | 实际获取邮件头数 | 成功数 | 跳过数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | full，旧索引升级 | 138.368 | 1742 | 1742 | 1742 | 0 |
| 2 | incremental | 0.711 | 0 | 0 | 0 | 0 |

自动测试：增量专项 8 项、全量回归 197 项通过。真实邮箱耗时是本次测量结果，不保证后续网络延迟。
