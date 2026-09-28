# 第三阶段：统一 Message 查询与选择导入

> 当前导入行为已由 [General Import Agent](GENERAL-IMPORT.md) 更新（2026-09-29）：完整正常工具、单条外部消息、本轮处理完成即 imported=true，不再等待 Memory commit。QQ 文字提取范围已扩展，见 [QQ 可读消息](QQ-READABLE-MESSAGES.md)。本文查询/分页说明仍有效，下方旧导入实现与状态表作为历史记录。

## 用法

在已有 QQ 同步和 Email 索引基础上：

```text
/list_messages
/list_messages qq
/list_messages qq 20 20
/list_messages {"limit":20,"offset":20}
/list_messages qq {"imported":false,"limit":20}
/list_messages email {"conversation":"INBOX","limit":10}
/list_messages qq {"conversation":"group:123456","time_from":"2026-09-01T00:00:00+08:00","time_to":"2026-09-30T23:59:59+08:00"}
/read_message qq <Message ID>
/import_message qq <Message ID>
/import_message email <Message ID>
```

将 `<Message ID>` 替换为列表中的完整整数。不要使用 QQ 原始 message_id 或自行缩短 ID。ID 跨来源唯一时可省略 source，如 `/import_message 1523`；有歧义会报错，要求明确来源。

用户命令和 Agent 共用 `list_messages`、`read_message`、`import_message` 工具。仅查询或查看不导入，不修改 imported 或 Memory；用户明确选择一条消息并要求导入后，才运行导入流程。Email 查看完整正文可能下载并缓存 EML，但不会写 Memory 原文归档。

列表按消息时间降序，时间相同按 ID 降序，未知时间最后；时间过滤使用带时区 ISO 8601，起止边界均包含，未知时间不会通过时间范围筛选。工具支持 `source=None, limit=20, offset=0`，省略 source 查询所有来源；limit 必须为 1~100 的整数，offset 为非负整数（SQLite 上限 9223372036854775807）。非法参数返回错误。分页按此顺序跳过 offset 条后返回至多 limit 条，结果显示本页条数及 offset，不查询总数。QQ conversation 支持会话 ID、`private:ID`/`group:ID` 或精确名称；Email 支持文件夹名或原始 Message-ID。推荐用带类型的 QQ 会话 ID 避免同名会话混淆。

QQ 摘要包含 Message ID、时间、会话名称及 ID、发送者名称及 QQ、文字预览、imported。完整查看和导入都保留完整文字及空格/换行。Email 摘要保留主题、发件人及文件夹。

## 历史：链路梳理与抽取

原有 Email 链路：EmailIndex 行 → Email Adapter → Message → 用户选择 ID → 下载/缓存 EML → MIME 解析及原文归档 → `run_turn()` → Temporary Memory → review。此前最后的 imported 标记只检查模型正常结束，可能早于 Memory 提交。

真正属于 Email 的部分继续保留：IMAP host/account/folder/UIDVALIDITY/UID、缓存哈希、Message-ID 校验、MIME/附件元数据、subject/from/to、不可变 EML 归档、邮件写作及发送。

通用部分已抽取：

| 模块 | 职责 |
| --- | --- |
| `agent/messages/__init__.py` | 统一读取、会话/时间/imported/limit 过滤、摘要查询及全文查看 |
| `agent/messages/sources.py` | 来源 registry，适配已有存储、来源准备、摘要及标记接口 |
| `agent/messages/formatters.py` | QQ 上下文文字、Email 原有结构化输入与各自摘要 |
| `agent/messages/importing.py` | 选择 ID、来源解析、导入锁、重复保护、公共完成状态 |
| `agent/messages/processing.py` | 公共输入 → `run_turn()` → 工具失败检测与 Temporary 结果 |
| `agent/messages/locking.py` | 跨进程单条导入锁，Email 旧模块继续导出兼容名称 |

公共查询和导入协调层没有 QQ/Email 分支。新来源通过 registry 提供存储/准备适配及 formatter，复用选择、查询、Agent 执行和完成状态。公共 Message 顶层未增加字段。

Email 的 `process_eml()` 保留为来源准备入口，并调用公共 `process_input()`；本地 EML CLI、原文归档、附件元数据及旧 `/import_email` 均保留。后者现在转发到公共 `import_message`。`message_context` / `processing_message` 为通用名称，旧 `email_context` / `processing_eml` 提供兼容别名。

QQ 导入输入包含来源、时间、会话、发送者和完整文字，不包含 checkpoint、账号存储字段或 ID 哈希实现。它使用同一个 `run_turn()` 和 FileTools，不建立 QQ 专用 Agent。来源内容均标记为不可信数据，不能当作授权、工具指令或用户本人的写作证据。

## 历史：imported 与 Memory 的统一边界（已替换）

| 情况 | 结果 |
| --- | --- |
| 正常处理，无待提交 Memory 修改 | processed，imported=true |
| 正常处理，仍有 Temporary 修改 | pending_review，imported=false |
| review 回答 no / self 审阅未完成 | Temporary 保留，imported=false |
| 后续 /commit 正式提交 | 提交后回调来源存储，imported=true |
| /cancel、显式 discard、正常退出 | 取消待导入完成回调，imported=false，释放导入锁 |
| 模型异常、工具失败、Memory 冲突/提交失败 | imported=false；既有 Temporary 按原行为保留 |
| 模型已提交 Memory 但最终回答失败 | imported=false，允许用户检查后重试 |
| 记录已 imported | already_imported，不再次调用模型 |
| 同一会话重复导入待审阅记录 | pending_review，不再次调用模型 |

MemoryPolicy 仅提供通用的会话内完成回调，不认识 QQ/Email。待审阅消息的导入锁保留到提交或取消，其他 Memory 会话不能同时导入同一条记录。多个待审阅消息在同一 Temporary 事务中提交时分别标记。标记前再次核对来源身份，QQ 还核对完整内容，防止导入期间记录变化。

Memory 提交与 Email JSON / QQ SQLite 不是跨存储原子事务。若正式提交后状态写入失败，保留已提交 Memory、消息保持未导入，commit 返回的 `message_imports` 包含错误；重试前应检查已有 Memory，避免重复候选。若进程异常终止，未完成标记保持 false；新进程按原 Memory 规则重置未提交 Temporary。异常退出可能留下单条 `.lock`，确认无导入进程后方可手工移除。此保守策略允许少量重试，不会仅因发送给模型就标记成功。

历史版本已经标记 imported=true 的 Email 不自动重置；不能从现有数据推断其派生 Memory 是否最终获批。

## 范围

仅处理已经同步的 QQ 纯文字 Message。无 QQ 图片/文件/语音支持、无批量自动导入、无联系人画像、无群聊摘要；不增加后台同步、实时监听或 QQ Automation。原有其他来源 Automation 流程保留。

测试使用隔离的合成消息、模拟模型/邮箱和真实 Memory 事务，不需要在线 NapCat 或真实 LLM。

## 数据库分页与 Email 迁移

QQ 继续使用 `data/qq/messages.sqlite3`；Email 使用独立的 `data/email/index.sqlite3`。首次访问旧 Email 索引时，事务性迁移同目录的 `index.json`，保留所有 ID、导入状态、同步进度与失败记录。旧 JSON 原样保留作迁移前备份；迁移成功后不再读取或更新它，后续数据以 SQLite 为准。仍接受旧 `.json` 路径参数，并定位到同名 `.sqlite3`。迁移失败会回滚，修复旧文件后可重试；首次迁移需要读取旧文件的全部内容。

列表的来源选择、conversation/imported/时间筛选、排序及 `LIMIT ? OFFSET ?` 均在 SQLite 完成。省略来源时通过 `ATTACH` 查询两个独立存储，`UNION ALL` 后执行全局排序分页，不把各来源的前 offset 条加载到 Python。仅返回本页 payload 后才进行 JSON 解码和 Message 构造。

完整正整数 ID 保持不变，SQL 使用十进制文本长度与字典序实现精确 ID 排序；时间标量函数只规范化日期字段，保留时区及微秒语义，不解析整条消息。跨来源时间和 ID 均相同的新情况按 source 名称升序稳定排列。未增加搜索索引，因此 SQLite 仍可能扫描、排序匹配记录，深 offset 也有扫描成本；本次保证限制消息载入量，不承诺查询耗时与总量无关。

Email 同步与单条读取保留原快照接口；这次没有改变 `read_message` 的 ID 查找方式，也没有优化同步或单条读取的全量快照行为。邮件草稿仍保存为原来的 JSON 文件。


## Message 通用关键词搜索 V1

`search_messages(query, source=None)` 搜索存储 payload 的所有非 null 叶子值及统一 projection 的 id/source/stamp/imported，不搜索 JSON key。无需维护字段名单；新来源复用 registry 的 listing projection 即可。数字按 SQLite 文本形式，布尔按 true/false 搜索。空白关键词拒绝，其他关键词保留原样。

```text
/search_messages MaxRL
/search_messages --source qq "MaxRL paper"
/search_messages --source email 作业
```

source 省略查询所有来源；只读返回摘要，按标准化 UTC 时间倒序，最多 100 条。内部 SQL LIMIT 101 检测 truncated，仅解码前 100 条。全文使用 read_message，用户明确选择后才能 import_message；搜索不会修改 imported 或 Memory。

V1 使用 JSON1 + 参数绑定 LIKE，百分号、下划线、反斜杠均按字面子串处理。沿用 SQLite LIKE 大小写规则（ASCII 不区分大小写，非 ASCII 不进行 Unicode 折叠）。没有全文索引，每次查询仍需扫描候选来源的 payload 并排序，耗时随数据量和 payload 大小增长；返回上限不代表扫描上限。null、空对象和空数组不作为可搜索值。只搜索已存储 payload，不下载 Email 原文，未缓存邮件正文不会命中。未知时间排在最后，同时间沿用 ID/source 稳定排序。沿用 Email 首次访问的旧索引迁移机制，不更新消息导入状态。没有字段选择、DSL、任意 SQL、深分页、语义搜索或批量导入。
