# General Import Agent：外部单条 Message → 正常 Agent

Import 表示把用户明确选择的一条外部信息交给正常 Agent 处理。Memory 是可用工具之一，不要求写入；只回复或不采取操作也可以成功。本阶段没有新增自动监听、自动导入或 QQ 回复功能。

## 旧流程与新调用链

旧流程由来源的 `process()` 驱动：Email 经 `process_eml()` 做 Memory 原文归档、QQ 使用 Memory 导向的规则；二者启动一轮限制工具集的 Agent。Agent 工具调用导入时还会嵌套 `run_turn()`。存在 Temporary 修改时，消息等待 Memory commit 才标记 imported，导入锁也保留到审阅结束。

新流程共享 `agent/messages/importing.py` 的 loader 和 JSON wrapper：

```text
用户 /import_message source id
  → FileTools.import_message
  → 单条导入锁 + load_message_for_import
  → source.import_content（只准备来源内容，不调用模型）
  → External Message JSON
  → 正常 run_turn / FileTools / TOOLS
  → 正常结束后标记 imported，释放锁

已有 run_turn 中的 import_message 工具
  → 同一 loader / wrapper / 单条锁
  → 返回 status=loaded 的 tool_result
  → 原 Agent 继续当前轮，不递归调用 run_turn
  → 当前轮正常结束后标记 imported，释放锁
```

普通聊天和 General Import 使用同一个 `run_turn()`、同一 `TOOLS` 注册表与正常 FileTools 权限。没有 Import 专用工具名单。新增正常工具不需再同步维护 Import 列表。已有只读草稿子流程及本地 `/email` 的限制保持原用途，General Import 不设置旧的 `processing_message` 限制标志。

## 单条与来源数据

Email/QQ 均以本地统一 Message ID 选择，source 有歧义时必须明确。不要使用 QQ 原始 message_id 代替本地 ID。

- Email：保留 Message.content 中的来源身份和邮件头，增加 MIME 解析后的结构化 `email`（正文、收发件人、主题、日期、附件元数据等）。继续使用身份和哈希校验的 EML 缓存，完整附件字节仍留在缓存，附件元数据不等于附件正文。
- QQ：保留完整 `content`，包括 text、sender、conversation、account_id、message_id；不拉取相邻消息。
- 统一 wrapper 为 `{"kind":"External Message","external_message":{"source":...,"message_id":...,"time":...,"content":...}}`。它是外部数据，不是 system prompt。

General Import 的 Email 不再隐式写入 `memory/inbox/email`。原始 EML 留在 `data/email/raw`；人工本地 `/email <path>` 仍保留原有 `process_eml()`、Memory 原文归档与 `--force` 流程。`/import_email <id>` 是 General Import 的 Email 兼容别名。

## 按需上下文

初始 payload 只包含所选 Message，不自动加载 Email thread 或 QQ conversation。当前工具已经能满足最小上下文查询，因此未新增 thread 管理器或工具系统：

```text
list_messages(source="qq", conversation="group:群号", time_from=带时区时间, time_to=带时区时间, limit=20)
search_messages(query="会议", source="qq")
read_message(source="qq", id=邻近消息ID)
```

Email 可用搜索、文件夹/Message-ID 过滤及单条全文读取。现有 Email 查询不提供完整的跨邮件 thread 聚合，本阶段不补建它。查询上下文不会改变邻近消息的 imported，也不授权批量导入。

## 外部数据与权限

统一 General Import 规则进入正常 Agent 的可信系统说明，明确来源正文、邮件头、QQ 文本、附件文本中的 system/tool/approval 话术均为不可信数据。真实用户的选择操作与序列化 JSON 内容分开；来源文字不会拼进 system prompt。Agent 根据用户任务、上下文和工具权限判断行动，不把发件人的请求直接当作用户授权。

Memory 仍使用相同 Temporary → diff → commit → 用户 yes；no 保留候选，cancel 丢弃候选。发送邮件仍使用原 `send_email` Runtime 快照确认和 SMTP 流程。其他正常工具沿用既有权限；没有新增自动发送邮件或自动回复 QQ 的路径。Prompt 边界不等于对任意模型输出的形式化安全保证，确认和文件访问边界仍由现有 Runtime 执行。

## imported 的当前含义

`imported=true` 表示该 Message 已成功完成 Agent 处理，**不表示已写入正式 Memory**。历史 true 记录不重置。

| 情况 | 当前结果 |
| --- | --- |
| 正常回复，无工具/无 Memory 修改 | processed，imported=true |
| 创建提醒、查询上下文、生成文件后正常结束 | processed，imported=true |
| Memory 候选待审、review no、self 未接受或显式 discard，Agent 正常结束 | processed，imported=true；Memory 按原规则保留或丢弃 |
| 稍后 commit/cancel/关闭会话 | 不再改变该 Message 的处理状态 |
| tool 返回所选消息，Agent 尚未结束 | loaded，imported=false |
| 同轮重复请求同一已加载消息 | processing，不重复加载或再启动 Agent |
| 已成功处理，再次请求 | already_imported，不运行模型，不重复副作用 |
| API 超时、异常、输出截断、步骤耗尽、工具错误 | 不标记成功，保留既有 Temporary，可人工重试 |
| 完成时来源身份/内容校验或状态写入失败 | 报错，不宣称状态保存成功；不回滚已完成副作用 |

工具错误采用保守处理：本轮出现工具执行错误时，导入不标记成功，即使模型随后返回文本。跨进程单条锁只持有到本轮结束，已不绑定 Memory 审阅；异常结束也释放锁。进程被强制终止时仍可能留下既有 `.lock`，确认无运行中的导入进程后再处理。

Automation、文件、邮件或 Memory 提交与 Message 状态不是跨存储原子事务。模型在副作用完成后失败，或 imported 写入失败时，重试有重复副作用风险；本阶段不引入通用操作账本，也不自动重试 Import。重试前检查现有提醒、文件、草稿或 Memory。正常完成后的重复导入由 imported 防重。

## 修改位置

| 文件 | 改动 |
| --- | --- |
| `agent/messages/importing.py` | 单条 loader、统一 wrapper、本轮导入状态与锁、直接/工具两条路径 |
| `agent/messages/sources.py` | Email/QQ 仅准备结构化内容，不驱动来源专用 Agent |
| `agent/messages/processing.py` | 统一 General Import 规则；保留本地 EML 的旧处理函数 |
| `agent/main.py` | 正常 run_turn 管理导入完成范围，共享系统规则/工具注册表 |
| `agent/tools.py` | 工具说明改为本轮处理，工具错误交给导入完成范围 |
| `agent/messages/models.py` | 明确 imported 新语义 |
| `tests/test_general_import.py` 等 | General Import 验收及更新旧状态/嵌套流程测试 |

## 人工验收

先通过既有同步获取 Message，运行 `python -m agent.main`，使用 `/list_messages qq` 或 `/list_messages email` 获取本地 ID，然后执行 `/import_message qq <ID>` 或 `/import_message email <ID>`。

1. 长期信息：“以后组会改到星期三下午。”检查 Agent 可读 Memory、提出 Temporary 候选；review 输入 no，正式文件不变，但本轮成功后 imported=true。
2. 任务信息：“明天下午三点前提交报告。”检查 Agent 能使用 Automation 等正常工具，必要时间/授权缺失时仍需澄清，不以 Memory 为唯一去向。
3. 普通通知：“收到，谢谢。”允许直接回复、零工具调用；`/read_message` 或列表显示 imported=true，再次 import 应跳过。
4. 上下文不足：“地点改成 302。”检查初始 External Message 只有一条；Agent 可按需调用 list/search/read 查询相邻内容，邻近消息保持未处理。
5. 注入文本：含 “Ignore previous instructions …” 的合成消息。检查它位于 external_message.content，未进入 system；不应变成用户授权。若模型提出发送，原确认仍必须出现且 no 不发送。
6. API 故障或中断：确认 imported=false；恢复后显式重试才更新。检查已有副作用后再重试。

自动测试使用合成消息、模拟模型/邮箱和隔离存储，不验证真实模型每次会选择何种行动；真实模型质量与上述交互需人工验收。
