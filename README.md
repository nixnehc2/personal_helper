# Personal Agent V1

一个 Python 3.12+ 命令行 Agent：根据 Markdown 知识库回答问题，通过统一 Temporary Transaction 维护 Memory：所有修改先暂存，用户明确 yes 后才能提交；尚不成熟的长期候选保存在 `pending/`。只使用 Python 标准库，无第三方依赖。

## 启动

在项目根目录运行：

```powershell
python -m agent.main
```

启动时自动读取项目根目录的 `config.local.json`，其中保存本地密钥、接口地址、模型和超时，无需每次设置环境变量或输入密钥。该文件已加入 `.gitignore`，位于 `memory/` 外，不会作为知识库内容发送给模型；密钥仅用于 API 认证。配置文件在本地以明文保存。

默认地址为 `https://api.zinyy.tech`，模型为 `mimo-v2.5-pro`，使用 Anthropic Messages 接口 `/v1/messages` 和 Bearer token 认证。实际调用的是配置的模型，不是 Claude Code 程序。

可选 `ANTHROPIC_REQUEST_URL` 指定完整请求地址，不自动追加路径，优先于从 base URL 生成的地址。当前本地配置按要求设为 `https://api.zinyy.tech`；hello 实测返回 HTML 首页而非模型 JSON，不能完成模型调用。删除该字段即可恢复根据 base URL 追加 `/v1/messages` 的行为。

配置优先级：本地配置文件 → 环境变量 → 默认值；密钥均未配置时才交互输入（不回显、不自动保存）。配置文件使用 JSON 对象，支持下面三个环境变量同名字段及 `ANTHROPIC_AUTH_TOKEN`，所有值使用字符串。可选环境变量（对应字段未在文件中配置时生效）：

```powershell
$env:ANTHROPIC_BASE_URL = 'https://api.zinyy.tech'
$env:ANTHROPIC_MODEL = 'mimo-v2.5-pro'
$env:AGENT_TIMEOUT_SECONDS = '120'
python -m agent.main
```

不要把真实密钥写进代码或提交到 Git。程序不加载 Claude Code 配置，不加载 `.env`，不保存聊天。请求会将必要的对话及读取到的资料发送至所配置的 LLM 服务。

指定另一份知识库：`python -m agent.main --root C:\path\to\memory`。该目录必须已经存在并包含 `AGENT.md`。

输入 `/exit` 退出但保留未提交 Temporary，`/clear` 仅清空进程内对话（Temporary 保留），`/cancel` 明确放弃整个 Temporary Transaction。每次启动新 Agent 进程时都会用 Formal Memory 完整覆盖 Temporary，因此会话开始时 Temporary 与 Formal Memory 一致。

在普通聊天进程中导入邮件：

```powershell
python -m agent.main --root memory
```

然后输入：

```text
/email C:\path\to\email.eml
```

可选项与原邮件命令一致：`--authored-by-user` 仅用于你本人写作或认可的邮件，`--reprocess` 用于前次模型处理失败后的显式重试。邮件导入作为同一轮对话运行：解码邮件附加专门的导入规则，但消息列表与后续提问连续保留；输入 `no` 后可以直接继续反馈，让 Agent 修改 Temporary，再重新申请 commit。

所有新增、修改和删除均先进入 Temporary。Agent 会主动把可能有长期价值的信息（例如偏好、目标、项目状态和持续关注主题）写成 Temporary 候选，不要求用户先说“记住”；普通知识问答、随机闲聊和无依据推测不机械写入。可能长期有用但还不够稳定或明确的信息先进入 `pending/`，后续优先更新已有候选、合并重复项或删除被否定项；成熟后迁移到正式分类并删除原 Pending。Agent 认为本批修改完成后调用 `commit_memory_changes` 进入 review，Runtime 展示完整 diff。只有精确输入 `yes` 才批准，`no` 或其他输入均保留 Temporary，随后可以继续反馈修改；只有明确 discard/cancel 才放弃。消息结束、异常中止或进程退出都不会清理 Temporary；下一次 Agent 进程启动时统一从 Formal 重新初始化。

`self/` 在统一确认后还有额外审阅。全部 self 修改均获准才提交整笔事务；部分接受或拒绝不落盘，整个 Temporary 保留。审阅时编辑的内容先保存在 Temporary，需要再次 commit 确认新 diff。

邮件功能见 [EMAIL-V1.md](EMAIL-V1.md)：提供本地 `.eml` 解析、导入、起草与编辑学习，以及 IMAP 邮件头索引和按本地 ID 导入单封邮件，不发送邮件。

## 邮箱目录同步（第一阶段）

无需模型或 Memory 即可运行：

```powershell
python -m agent.email_index
```

聊天中输入 `update_email`、`update_email()` 或 `/update_email` 可直接同步；Agent 也可调用无参数工具 `update_email()`。返回 ID、Subject、From、Date、已导入/未导入状态。仅获取邮件头，不下载完整 EML、正文或附件，不调用导入流程，不修改 Memory。

三个入口共用 `agent.email_index.update_email_index()`：CLI 直接调用核心函数，`/update_email`（及兼容写法）通过 `FileTools.execute("update_email", {})` 显式调用同一个 Agent Tool，Tool 再调用核心函数并限制展示为 100 条。旧 Python 函数 `update_email()` 仅作为兼容转发。IMAP 与索引写入只在核心层执行；Tool 不接收 ID、imported 或索引路径等参数，Agent 无法通过该接口自行覆盖状态。

私有 `config.local.json` 新增字符串字段：`EMAIL_ACCOUNT`、`EMAIL_AUTH_CODE`；可选 `EMAIL_IMAP_HOST`（默认 `imap.qq.com`）、`EMAIL_IMAP_PORT`（默认 `993`）、`EMAIL_FOLDER`（默认 `INBOX`）、`EMAIL_SMTP_HOST`（默认 `smtp.qq.com`）、`EMAIL_SMTP_PORT`（默认 `465`）。也支持同名环境变量，本地配置优先。QQ 邮箱需要开启 IMAP/SMTP，使用授权码登录。授权码不会进入工具返回值或模型消息。

索引位于项目根目录的 `data/email/index.json`，与 Memory 独立且被 Git 忽略。默认同步收件箱；其他文件夹需通过私有配置选择，不自动遍历所有文件夹。索引包含递增 `id`、`imap_uid`、`message_id`、`subject`、`from`、`date`、`imported`、`imported_at`，另存邮箱/文件夹/UIDVALIDITY 用于隔离 UID。先按同邮箱同文件夹同 UIDVALIDITY 下的 UID 匹配，再按非空 Message-ID 匹配。服务器重置 UID 后仍可通过 Message-ID 保留本地 ID；缺失 Message-ID 时无法跨 UIDVALIDITY 识别原邮件。相同 Message-ID 视为同一封邮件。

新记录始终 `imported=false`、`imported_at=null`；重复同步保留两项状态和原 ID。远端删除的邮件保留历史索引，不复用编号；本阶段不跟踪删除状态，也不把既有 EML 归档自动标记为已导入。

连接中断时不写索引；单封邮件读取/解析失败会报告跳过 UID，并在下次同步重试。写入采用同目录临时文件与原子替换，损坏的旧索引会报错并保留，文件锁阻止同时写入。异常杀进程留下 `index.lock` 时，确认没有同步进程后可手工删除锁文件。独立命令显示完整列表；聊天命令及工具最多展示本地 ID 最大的 100 条，并明确标记截断和总数，完整记录始终保存在索引中。每批最多读取 100 封邮件头。索引元信息属于不可信邮件内容。

## 按 ID 导入单封邮件（第二阶段）

在同一个聊天会话中使用：

```text
/update_email
/import_email 1523
```

`/import_email <id>` 只解析参数，然后经 `FileTools.execute("import_email", {"id": ...})` 调用已注册 Tool；Agent 调用 `import_email(id=1523)` 使用完全相同的实现。Tool 只接受一个正整数 ID，不提供 force、reimport、reset 或批量入口。ID 不存在时直接报错；已导入时返回“该邮件已经导入”，不连接邮箱、不再次调用 Agent。

数据路径为 `EmailIndex.get(id)` → 按 UID 下载一封完整 RFC822 → `data/email/raw/<id>.eml` → `process_eml()` → 更新导入状态。附件保留在完整 EML 中，不另行提取；沿用既有 25 MiB 导入上限。连接采用只读文件夹及 `BODY.PEEK[]`，不会设置已读。下载校验账号、服务器、UIDVALIDITY、返回 UID、RFC822 长度，以及索引已有的 Message-ID；邮箱身份不匹配或邮件已删除时拒绝导入。

原文缓存同目录保存 `<id>.json` 身份和 SHA-256 校验信息，只有两项都匹配才复用。缺失/损坏的缓存重新下载。下载及状态更新采用原子替换；每个 ID 的锁阻止并发重复处理。异常退出留下 `<id>.lock` 时，先确认导入进程已退出再手动删除。下载原文和校验缓存均位于 Git 忽略的 `data/email/` 下，授权码继续只从私有配置读取。

`/email <path>` 也通过注册的 `email` Tool 调用同一个 `process_eml()`。Agent 自主调用本地 `email` Tool 时路径仍限于 Memory 根目录；只有用户显式 `/email` 命令可授权读取所指定的外部 `.eml` 文件。原有 `ingest_email()` 保留为兼容转发，原本地 EML CLI 继续可用。MIME 解析、Agent、Temporary Memory 和 review 使用原有流程。Agent 在工具调用中触发 EML 处理时使用独立消息列表，避免向模型发送尚未配对的 tool_use；客户端、Memory 工具和 Temporary Transaction 仍是同一份。处理邮件期间禁止再次调用导入工具，避免递归导入。

仅 `process_eml()` 正常完成且没有工具错误/Memory 冲突时写入 `imported=true` 和 UTC `imported_at`。仅下载成功、原文已归档、模型/Memory/索引写入失败均不算成功，保持未导入；失败后再次使用同一命令会复用可信 EML，并重新完成处理，不因原文已归档而跳过。处理过程中出现过工具错误时保守地视为失败，即使模型随后结束回答也不标记成功。

原始 EML 在处理开始时立即归档至 `memory/inbox/email/<sha256>.eml`，并同步到 Temporary 的镜像；它是源文件留存，不是 Agent 的记忆修改，不计入待审文件数、不展示 diff、不要求用户确认。只有派生记忆才进入 Temporary review。仅归档原文的邮件不会提示“Temporary 保留 1 个文件”。拒绝提交、取消 Temporary 或重启均保留原文，模型失败也不会删除已归档原文。启动时会保留旧版仅存于 Temporary 中且 SHA-256 文件名校验通过的 EML；这不提交旧的派生记忆。

**imported 表示 EML 已处理，不表示 Formal Memory 已提交。** 用户 review 回答 `no` 是现有流程的正常结果：Temporary 保留，导入仍可完成；后续提交/取消/新进程重置 Temporary 不会回滚 Email Index 的 imported 状态。索引与 Memory 不是跨文件事务：如果 Memory 处理成功后索引写入失败，索引保持未导入并报错，重试会再次处理原文。现有 `/email --force/--reprocess` 兼容行为只属于本地 EML 路径，不是 `import_email` 的参数。

本阶段没有新增自动导入或自动回复功能。测试使用模拟 IMAP、SMTP 和模型及临时目录，不导入真实个人邮件。

每次工具调用会显示简短参数：读取的文件与行范围、搜索关键词与范围、列出的目录，以及写入目标文件。长参数会截短，换行等字符会转义，避免日志刷屏；完整修改仍在确认 diff 中展示。例如：

```text
[tool] read_file | 文件="self/_INDEX.md" | 起始行=1 | 最多行数=200
[result] success
[tool] search_files | 关键词="沟通偏好" | 范围="self"
[result] success
[tool] list_directory | 目录="projects"
[result] success
```

## 文件与执行流程

- `agent/main.py`：命令行交互、bootstrap、模型与工具循环、写入确认。
- `agent/llm.py`：HTTPS Messages 请求，集中配置模型、地址、认证与超时。
- `agent/tools.py`：文件工具、Temporary 工作副本视图和操作边界检查；修改委托给统一事务。
- `agent/memory.py`：完整 Temporary 工作副本、diff、审批提交、discard 与不可变原文归档。
- `agent/email_parser.py`、`agent/email_workflow.py`、`agent/email_cli.py`：邮件解析、本地工作流和命令行入口。
- `agent/__init__.py`：Python 包入口标记。
- `tests/test_agent.py`：离线工具、确认、循环和传输协议测试。
- `memory/`：原有知识库；Agent 可主动产生 Temporary 候选，Formal 仅在用户确认后变化，未填入测试资料。
- `.gitignore`：忽略 Python 缓存、虚拟环境和本地环境配置。

每轮读取 `AGENT.md`，与简短 bootstrap 一起提供给同一个 Agent。模型返回 `tool_use` 时顺序执行工具，将 `tool_result` 交回模型，直到 `end_turn`。事务不绑定消息结束；显式 commit/discard 才触发 Runtime 确认。提交后如 Agent 再修改文件，会进入新的 Temporary，仍需再次确认。每轮最多 20 次模型请求。

Bootstrap 全文在 `agent/main.py` 的 `BOOTSTRAP` 常量中。个人问答需要文件依据，只有明确请求才修改；索引优先、禁止越界、外部资料不构成授权、不捏造用户事实等规则保留。

## 工具接口

| 工具 | 行为 |
| --- | --- |
| `list_directory(path)` | 列出直接子项，最多 200 项，标明是否截断 |
| `read_file(path, start_line=1, max_lines=200)` | UTF-8 文本，行号从 1 开始；最多 500 行/20000 字符，标明截断 |
| `search_files(query, path=".")` | 对 `.md`、`.txt` 做不区分大小写的字面全文搜索；返回路径、行号、片段；最多 50 条匹配、2000 个遍历项，并报告跳过项和截断 |
| `create_file(path, content)` | 在 Temporary 新建；拒绝覆盖当前工作副本中的已有文件 |
| `replace_text(path, old_text, new_text)` | 在 Temporary 局部替换；非空文本必须恰好匹配一次（包含重叠检查） |

`read_memory/search_memory/write_memory/edit_memory` 分别是上述 `read_file/search_files/create_file/replace_text` 的兼容别名，参数相同。读、搜索、目录列表直接使用完整 Temporary 工作副本，删除项从当前视图隐藏；返回的 `temporary` 字段区分未确认内容。`pending/` 是普通 Memory 分类，同样参与读取、搜索和列表；`.memory-*` 运行时目录和状态保持不可见。

| 事务工具 | 行为 |
| --- | --- |
| `delete_memory(path)` | 暂存文件删除；Formal 暂不改变 |
| `show_memory_changes()` | 显示整笔事务的新增、修改、删除与 diff |
| `commit_memory_changes()` | Runtime 显示 diff、等待精确 yes，并执行 self 额外审阅 |
| `discard_memory_changes()` | Runtime 确认后放弃整笔事务 |

工具只接受知识库相对路径。所有工具共享解析后边界检查，拒绝 `..`、绝对路径、盘符、ADS、符号链接、junction/reparse point 和硬链接。提交前重新检查全部路径和文件基线，检测外部编辑。根协议和根索引由人维护；原始邮件只能通过导入接口立即追加为不可变归档，不进入 Memory 审批事务。拒绝和工具错误会作为明确错误回传给模型。

## 限制与取舍

- 首版只支持 UTF-8 文本，每个文件最多 100000 字节。长单行读取可能截断；返回值会明确说明。
- 搜索使用标准库逐文件扫描，避免依赖 ripgrep，保留相同根目录检查；没有 shell、数据库、向量搜索或后台任务。
- Pending 使用与正式分类相同的文件大小和事务限制；不提供次数阈值、自动衰减、定时扫描或复杂置信度评分。成熟判断由 Agent 在对话中基于语义完成。
- 对话超过约 250000 个序列化字符时停止，提示 `/clear`；不自动压缩或持久化。
- HTTP 超时或错误不自动重试；响应超过上限或模型输出截断时不执行该响应的工具调用。程序不自动跟随 HTTP 重定向。
- Temporary 是所选 Memory 根目录内 Runtime 私有的 `.memory-temporary/` 完整工作副本；首次修改时标记事务开启。同一对话进程内跨轮保留；新进程启动总是执行 Formal → Temporary 完整同步。`.memory-*` 运行时目录与状态不进入 Agent 检索，并被 Git 忽略。
- 提交前做全量 diff 与 Formal 基线校验；用户 yes 后执行 Temporary → Formal 完整同步。discard 与新进程启动均执行 Formal → Temporary 完整覆盖，不做局部删除或逐文件回滚。
- 普通文件系统不能让多个 Markdown 对外部读取者瞬间一起切换；硬中断到重启之间，外部程序可能看到部分已同步内容。不承诺断电下的物理磁盘持久性或防御恶意本地进程。协作实例通过文件锁与状态校验阻止相互覆盖；过期实例需重新打开根目录。
- 支持文件删除，不提供目录移动工具。根协议、根索引仍由人维护，原始邮件保持既有不可变归档流程。

## 邮件草稿编辑（第三阶段）

在现有 Agent 对话中使用：

```text
/edit_email 帮我给张老师写封邮件，告诉他周五下午可以参加讨论
再简短一点
第一句话改得正式一些
/edit_email 1 第二段再简短一点
```

`/edit_email <要求>` 新建草稿；`/edit_email <ID> <要求>` 修改指定草稿。普通聊天的写邮件请求也由 Agent 调用同一个 `edit_email(instruction, draft_id?)` 工具。省略 ID 总是新建，后续自然语言修改由 Agent 使用会话中的 `active_email_draft_id` 指向原草稿。`/clear` 和重启清空当前草稿指向，已保存草稿仍可按 ID 修改。

每份草稿保存在项目目录 `data/email/drafts/<ID>.json`，字段为 `id/to/subject/body/status/created_at/updated_at`；发送成功后增加 `sent_at/sent_message_id`，状态变为 `sent`。每次成功修改覆盖当前版本，保留创建时间；模型失败、输出不完整或并发修改冲突不会覆盖原草稿。草稿目录已被 Git 忽略，不上传个人邮件。

固定邮件写作 Prompt 位于 `agent/email_drafts.py`。写作复用当前模型客户端、完整有效对话上下文和现有 Memory 读取工具，可以参考 Temporary 候选内容，但不能当作已确认事实。内部流程只允许读取，不写 Memory；普通对话中的 Memory 学习仍走原 Temporary 流程。未知邮箱保持空值，用户当前要求优先于历史习惯。

每次编辑成功都返回并展示完整草稿。发送使用 `/send_email <草稿 ID>` 或 Agent 调用同一个 `send_email(draft_id)` 工具；工具只接受已保存草稿 ID，不接收临时正文。Runtime 展示完整快照并要求输入 `yes` 才继续，`no` 或中断不会连接 SMTP。确认期间发送的就是展示的快照；SMTP 成功后才写入 `sent` 状态，失败保持 `draft` 可重试，已发送草稿会被拒绝且不再弹出确认。V1 仅支持纯文本 To/Subject/Body，不支持附件、HTML、CC/BCC、回复/转发、定时或批量发送。自动测试使用模拟模型和 SMTP，验证存储、入口、上下文和权限边界；实际行文质量、模型判断和真实 QQ SMTP 行为仍需人工验收。

草稿输出接受纯 JSON 或完整的 JSON 代码围栏。若模型返回普通正文、解释文字或不完整字段，Runtime 会提示并请求模型纠正一次；仍不合规则报错，校验成功前不保存草稿。网络错误和被截断的输出不触发这次格式纠正。

## 回归验证

离线测试覆盖完整 Temporary 工作副本、Pending 可见与迁移、同进程跨轮保留、yes/no/discard、新会话基线重置、self 额外审阅、外部编辑冲突、邮件与聊天共享事务、注入审批边界，以及原有 MIME/草稿限制。

## Pending 流程

```text
普通对话
    ↓
可能有长期价值但还不确定
    ↓
memory/pending/
    ↓
后续对话持续更新 / 合并 / 删除
    ↓
Agent 语义判断已经成熟
    ↓
迁移到 self / history / 其他正式分类
```

如果当前存在 Temporary Transaction，Pending 的新增、修改、删除和迁移同样先发生在该事务的完整 Temporary 工作副本中，最后统一 diff、确认、commit 或 discard；Pending 不会绕过事务直接修改 Formal Memory。

```powershell
python -m unittest discover -s tests -v
```

手动验证可在副本上要求修改 project 和 self，先回答 `no`，继续反馈，再申请 commit；检查 Formal 只在用户确认且 self 审阅通过后改变。邮件导入建议直接在 `python -m agent.main --root memory` 中使用 `/email`，导入后未提交的 Temporary 仍在同一对话中继续修改；进程退出后不能续接，下一次启动会从 Formal 重新初始化。

协议参考：[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)、[工具调用往返](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)。第三方网关的实际兼容性以实测为准。

## 单封邮件导入测试

`test_single_email.ps1` 默认使用 `tmp/email-test/memory`。运行 `./test_single_email.ps1 -List` 查看编号，运行 `./test_single_email.ps1 -Number 1 -Reprocess -ContinueChat` 导入一封后继续反馈；不带参数可交互选择。`-Email` 支持指定文件路径，`-ParseOnly` 仅本地解析，`-DryRun` 仅检查参数。提交仍需 Runtime 的用户 yes；详细说明见 `tmp/email-test/README.md`。
