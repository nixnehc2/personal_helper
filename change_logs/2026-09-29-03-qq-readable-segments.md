# 2026-09-29：QQ 混合消息可读内容适配

- 日期：2026-09-29（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

将 QQ 消息过滤从“必须全部为 text”改为“存在 text/@/reply 等可读内容即保存”，避免混合消息被整条丢弃。本阶段不解析 JSON/share 卡片或媒体内容。

## 实际改动

- `agent/messages/qq_adapter.py` 逐段处理 text、at、reply：保持 text 原始空格/换行，@ 使用已有 QQ ID 或全体成员标记，reply 使用已有消息 ID；按原顺序直接拼接，无额外 API 请求。
- image/file/record/video/face/mface/forward/json/share 及其他合法未知段忽略，不否决同条可读部分；完全没有可读内容仍返回 None。损坏的段或已支持段缺失必要字段报错。
- 增加最小 CQ 兼容：识别 @/reply，忽略其他完整 CQ 段并保留周围普通文字，保留转义解码且不把转义后的字面 CQ 再解析。
- 在 QQ `content.segments` 保存原始输入深拷贝（array 为数组、CQ 为字符串），不新增统一顶层字段，不改变 Message ID 或 schema。
- `agent/qq_sync.py` 仅更换最终显示文案；`agent/qq_progress.py` 显示可读消息及无文字内容跳过，`agent/tools.py` 更新工具说明。同步分页、增量、去重、checkpoint、rollback、persistent skip、白名单及周期调度均不变。
- 更新 `tests/test_qq_sync.py` 中旧的混合消息跳过断言，新增 `tests/test_qq_adapter.py` 的转换、CQ、损坏数据及同步级验证；更新根说明、完整说明、索引和 [QQ 可读消息文档](docs/QQ-READABLE-MESSAGES.md)。

## 验证

- 本次执行 `python -m pytest tests -k qq -q --tb=short`：**114 passed，90 subtests passed**，372 deselected，3.55 秒。
- 本次执行完整测试集 `python -m pytest tests -q --tb=short`：**486 passed，193 subtests passed**，100.83 秒。
- 覆盖 text 多段及空白保留、at/text 前后顺序、all、reply/text 前后顺序、媒体/JSON/share 与 text 混合、纯媒体跳过、合法未知段、损坏字段、CQ 转义/混合、segments 深拷贝、稳定 ID、消息查询和导入 loader。
- 同步级 8 条历史案例验证 scanned=8、text=5、added=5、skipped=3、failed=0；同时验证 checkpoint 推进、重复去重、后续增量、分页、网络异常回滚、坏段不推进 checkpoint 及恢复重试。
- 本次 `git diff --check`：通过。以上均为本任务实际运行结果，未将历史结果当作本次验证。

## 已知问题与边界

- 完整测试输出 8448 条现有 `PurePath.is_reserved()` 弃用警告，来自 tools/file_reader/file_writer；本阶段未扩展修改该代码。
- 未连接真实 NapCat、未读取或改写个人 QQ 消息；测试仅使用合成数据与临时 SQLite。
- 不解析 JSON/share/forward 或媒体，不查询昵称及回复正文。CQ 仅处理最小完整语法，不完整 CQ 样式字符串作为普通文字保留。
- 旧记录没有 segments 仍兼容；去重不会重写旧行，checkpoint 之前曾被旧规则跳过的历史消息不自动回补。
- 现有全字段搜索可匹配保存在 segments 中的原始字段，这不表示本阶段提取了卡片或媒体正文。skipped 延续既有内部统计语义，包括永久跳过会话时的计数。
