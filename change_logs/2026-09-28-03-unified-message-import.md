# 2026-09-28：统一 Message 查询、查看与选择导入（第三阶段）

- 日期：2026-09-28（Asia/Shanghai）
- 对应提交：与本日志同一提交。

## 目的

在 QQ 纯文字同步之上，让用户和 Agent 查看并选择性导入单条消息；抽取 Email 与 QQ 共用的 Message 业务流程，保留来源特有的处理与旧入口。

## 实际改动

- 梳理 EmailIndex → Adapter → Message → ID 选择 → EML 下载/校验/缓存/原文归档 → run_turn → Temporary/review 链路。梳理与模块分工见 [第三阶段说明](docs/MESSAGE-V3.md)。
- 增加来源 registry；统一查询支持 source、conversation、带时区时间范围、imported、数量限制，时间降序展示；QQ 摘要有会话、发送者与正文预览，Email 摘要保留主题/发件人。
- 新增 `list_messages`、`read_message`、`import_message` 工具和同名斜杠命令。来源可在单条查看/导入时省略，但跨来源 ID 歧义时报错。纯查看不导入；导入仅针对用户选中的单条消息。
- 将格式化、单条锁、共用 run_turn 调用及工具失败检测抽取到 `agent/messages/`。QQ 输入携带来源、时间、会话、发送者、完整文字，不带 checkpoint 或哈希实现信息。公共导入协调层无 QQ/Email 条件分支。
- `/import_email` 转发公共导入；EML MIME/附件元数据、IMAP 校验、原文归档和本地 EML 入口保留。移除旧导入函数临时修改 email_index 全局路径的做法。
- 统一 imported 语义：待审阅 false，正式提交后 true；没有派生修改的正常处理可完成。显式放弃、异常和工具失败不标记。Memory 提供来源无关的会话内完成回调，pending 导入保留跨进程锁；QQ 状态更新核对原记录内容并事务保存。
- 将处理标志与上下文泛化为 processing_message、incoming_message、message_context，旧名称保留兼容别名；来源导入期间拒绝递归导入、同步、发送及 Automation 等副作用。
- 修复上一阶段发现的邮件草稿上下文作用域错误：edit_email 工具执行回到上下文管理器内部，使用户最初的写作要求保留在后续编辑上下文。
- 新增统一查询/导入测试；修改原先将待审阅 Email 视为已导入的三个测试，并增加提交/取消断言。更新使用说明与索引。

## 验证

- 完整回归 `python -m unittest discover -s tests`：393 项全部通过，包含 Email、Agent、Memory、Automation、QQ 与文件读写；上次已有邮件草稿失败项已通过。
- 随后新增 2 项测试（未知时间/来源 ID 歧义、同步保留 imported）并完成通用命名与 Email 查看格式收尾；最终专项：Message 85 项、Email 87 项、QQ 57 项全部通过。这些专项有重叠，不相加作为总测试数；未再重复完整套件。
- 覆盖：完整 QQ 上下文进入真实 run_turn、命令/Agent 共用入口及嵌套 transcript、无修改完成、重复导入、pending 后提交/取消、导入内提交/放弃、self review 拒绝、模型异常、提交异常、状态保存异常、记录变化、多个 pending、跨会话锁、registry 扩展与 Email 兼容。
- 文档本地链接检查与 `git diff --check` 通过。没有在线调用 NapCat、IMAP 或真实 LLM，测试使用隔离合成数据及模拟服务。

## 已知问题与边界

- Memory 与来源状态不构成跨存储原子事务：提交后状态写入失败或进程异常退出时，保守保留未导入；需要检查已提交 Memory 再重试。异常退出可能留下单条导入锁，确认无运行中的导入后手工移除。
- 待审阅完成回调随当前会话存在；正常退出取消未提交事务并释放锁，重启不自动导入。旧版已标记的历史 Email 不自动重置。
- 不新增非文字处理、自动选择/批量导入、QQ Automation、后台同步、实时监听、联系人画像或群聊摘要。
- 已有 `prompt.md` 用户修改保留，不包含于本次提交。
