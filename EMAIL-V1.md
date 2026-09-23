# Personal Memory + Email V1

## 范围

完成 Phase A–F：统一 Memory Temporary Transaction、MIME parser、本地导入、起草、编辑学习。QQ IMAP 邮件目录索引可通过 `python -m agent.email_index` 或聊天输入 `/update_email` 同步。第二阶段支持 `/import_email <id>` 和 Agent `import_email(id)`，两种调用共用同一个 Tool，只下载指定邮件，随后与 `/email <path>` 共用 `process_eml()`。处理成功后标记 imported；失败不标记，已导入则跳过。配置、缓存校验、状态语义和限制见 README。未接入 SMTP；当前没有发送接口，草稿和最终正文均不会发送。

原有资料保留。新增的正式 memory 文件是协议、索引和空主题文件；测试所得事实只写到 `tmp/` 下，不写入正式个人资料。`email_test/`、`tmp/`、原始 `.eml` 和本地配置均已加入 Git 忽略规则。本阶段没有提交或上传资料。

## 使用

运行目录为项目根目录，Python 3.12+，无需额外安装依赖。模型仍读取 `config.local.json`。

```powershell
# 纯本地解析：不调用 LLM，不落盘正文
python -m agent.email_cli parse "email_test/某封邮件.eml"

# 默认创建/使用 tmp/test_memory，不复制真实个人事实
python -m agent.email_cli ingest "email_test/某封邮件.eml"

# 只有确实由你写作或认可的最终邮件才能指定此标记
python -m agent.email_cli ingest "tmp/my-final.eml" --authored-by-user

# 在普通聊天进程中导入邮件，导入轮会附加邮件专用规则，且后续对话连续
python -m agent.main --root memory
/email C:\path\to\email.eml

# 根据原邮件和意图起草，也可以省略 --email 主动写邮件
python -m agent.email_cli draft --email "email_test/某封邮件.eml" --request "确认收到，并询问具体截止时间" --output tmp/draft-1.json

# 将你编辑后的最终正文保存为 UTF-8 文本，再执行学习
python -m agent.email_cli learn --draft tmp/draft-1.json --final tmp/final-1.txt

# 人工检查测试结果后，才明确选择正式资料库
python -m agent.email_cli --root memory ingest "email_test/某封邮件.eml"
```

`ingest`、`draft` 和 `learn` 会把解码后的相关邮件/文本及检索到的 Memory 发送给你配置的 LLM 服务；`parse` 完全本地执行。输入真实私人邮件前请确认这个服务是你希望使用的处理方。

默认通过 raw SHA-256 去重。同一原文再次导入会跳过；如果前次因网络或模型错误中断，显式加 `--reprocess` 重新分析已归档邮件。重新分析继续使用当前 Temporary Transaction，模型读取完整工作副本以避免重复。原文永不因处理完成而删除。

草稿输出 JSON 包含 intent、to、subject、body 和零或一个 one-shot 路径；它是本地工作产物，不是 Memory schema。输出不覆盖已有文件。`learn` 会在原草稿旁保存 `.learning.json`，同时保留原始草稿与用户最终正文；同名学习记录已存在时须使用新草稿副本，避免覆盖审阅记录。

## 统一事务与检索

普通聊天、邮件导入、修改学习使用同一个 `run_turn` Agent、同一套 Memory Tools 与审批流程。推荐在 `agent.main` 中使用 `/email` 导入：邮件作为当轮用户内容进入同一个 `messages` 列表，系统提示附加邮件导入规则；`no` 后无需退出，可直接继续反馈修改。旧 `email_cli ingest` 仍保留为一次性命令，其导入轮使用独立 `messages = []`；两种方式共享同一 Memory root 的 Temporary Transaction。

新增、编辑、删除只写 Temporary；读取、搜索和目录导航使用完整 Temporary 工作副本。Agent 使用 `show_memory_changes` 查看 diff，使用 `commit_memory_changes` 申请提交。Runtime 展示全部变更，仅精确输入 `yes` 才批准；`self/` 之后继续额外审阅。`no` 保留 Temporary 供反馈；discard 需确认，`/cancel` 明确放弃。进程退出或命令异常结束保留 Temporary；新会话启动会用 Formal 完整覆盖 Temporary。

Temporary 在同一对话进程内持久化。推荐使用 `agent.main` 的 `/email` 导入并在 `no` 后继续反馈；一次性 `email_cli ingest` 结束后不能继续反馈，下一次命令会从 Formal Memory 重新初始化。事务生命周期详见 README。

原始 `.eml` 归档属于保留的解析/归档流程，不是 Agent 的 Memory 修改：导入时仍立即追加不可变原文；即使 Memory 提交被拒绝，原文也保留。派生的历史、个人资料、索引、风格和案例全部经过 Temporary 与用户审批。根协议与根索引仍由人维护。

## 邮件处理

- `agent/email_parser.py`：解码 MIME、Base64、quoted-printable、声明 charset、multipart，保留纯文本/HTML和附件元信息；只有 HTML 时转可读文本，丢弃 script/style 内容，不访问远程链接、不提取附件。
- `agent/email_workflow.py`：导入先将原字节追加到不可变归档，再给模型解码结果；模型按协议判断有价值的历史、语义事实和案例，在 Temporary 中提出修改。使用 Message-ID/References/In-Reply-To 与上下文检索已有 thread，不靠主题单独判定。
- `incoming_email` 只标记来源，不限制 Memory 或 self 权限。邮件正文、引用文字和附件元信息属于外部资料，不能充当用户指令或确认；他人措辞仍不能被当作用户写作风格的证据。
- `DraftTools` 强制先记录意图，起草全程只读，先读取 email style（区分 Temporary 与 Formal）才能提交草稿；最多读取一个 one-shot 正文，不能用全文搜索批量加载案例。
- 修改学习把 Agent 原稿与用户终稿明确分开，区分事实修正、一次性措辞、弱风格、明确稳定风格和其他长期信息。语义分类由 LLM 根据协议判断，离线脚本测试只验证路由执行，不能证明模型永远判断正确。

编辑学习的终稿尚未发送，因此保留工具层禁止该操作写入 `history/email_threads/` 的限制，避免把草稿记录成已发送回复。其余事实和风格修改统一进入 Temporary，记录来源和不确定性，不依据一次编辑推导稳定偏好。

大小边界：每封 raw 最大 25 MiB；传给模型的正文最大 30000 字符，明确标记是否截断；附件仅元信息，嵌套邮件不当作主体正文。无法识别的编码会带警告并替代解码。没有每封邮件的 parsed Markdown、邮件数据库、自动发送、自动 consolidation 或向量库。

## 验证

```powershell
python -m unittest discover -s tests -v
```

本轮统一事务离线测试覆盖聊天与邮件共享审批、incoming self 修改、no 后继续编辑、discard、Temporary 恢复和异常回滚、注入文本不能替代用户确认。保留 MIME、去重、不可变归档、只读草稿与未发送历史限制测试。脚本模型验证执行边界，不证明真实模型的语义判断质量。

以下是旧版本在线探测记录；本轮未调用远端模型，不将旧结果视为新事务的在线验收：

本地 `email_test/` 的真实 `.eml` 已完成确定性解析检查。用户随后明确授权将真实邮件交给配置的 LLM，并重新精选了 20 封作为导入样本。本轮仅测试导入，固定快照与逐封记录在 `tmp/selected-email-ingestion-20260920-215917/`：20 封已解析、归档并校验哈希；1 封完成语义判断，4 封尝试超时，15 封尚未继续请求。30 秒与 180 秒极简模型探测均读取超时，故语义验收未完成；首封存在遗漏可保留学业证据的倾向。详见该目录的 `review.md`，服务恢复后可用 `tmp/resume_selected_ingestion.py` 续跑。此前合成邮件的端到端探测记录在 `tmp/live-email-report.json`，与本次真实精选样本结果分开。

实际合成测试结果：广告只保存 raw；项目邮件写入 project 和 thread；后续邮件复用原 thread；草稿成功生成且不使用无关 one-shot；当时版本的编辑学习仅写入旧风格候选文件，未改变历史或 self；该候选机制现已移除。复测结果见 `tmp/learning-check-report.json`。这些结果是有限样例验证，不表示全部真实邮件的路由都已人工验收。
