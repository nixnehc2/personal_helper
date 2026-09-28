# 2026-09-28：Message 通用关键词搜索 V1

- 日期：2026-09-28（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

输入关键词，在统一 Message 已存储的所有字段值中找到最近最多 100 条摘要，支持 QQ、Email 及 registry 后续来源。

## 实际改动

- 复用 pagination 的 ATTACH、listing_sql projection、跨来源 UNION 与时间排序；JSON1 json_tree 仅匹配非 null 叶子值，另覆盖 id/source/stamp/imported。布尔文本为 true/false，不搜索 JSON key，不维护 payload 字段名单。
- LIKE 参数绑定并转义百分号、下划线和反斜杠；SQL 查询 101 条判定 truncated，仅解码前 100 条。空字符串、纯空白、非字符串及非法来源拒绝。
- 新增 search_messages(query, source=None)、FileTools 方法和统一 Agent schema；复用来源 summarize 和既有摘要显示。搜索不导入、不修改 imported/Memory。
- 新增终端 /search_messages [--source qq|email] <关键词>，支持引号中的空格与字面反斜杠；关键词 qq 不被当作来源参数。
- 新增 tests/test_message_search.py；更新 MESSAGE-V3 使用说明及本日志索引。原有未提交 QQ 进度功能改动未纳入本次提交。

## 验证

以下均为本次实际运行，不引用历史通过结果：

- `python -m unittest discover -s tests -p test_message_search.py`：13 项通过（0.603s）；子用例覆盖 QQ 六类字段、Email 字段、动态嵌套值、非 key 搜索、数字/布尔、来源隔离、跨来源排序、特殊字符与注入文本、参数拒绝、摘要上限和入口分派。
- 大数据用例：10,005 条 QQ 消息，仅解码返回的 100 条；禁止旧全量 list/read 路径，验证 truncated；另测恰好 100 条不截断。未来测试来源验证 projection 公共字段和 registry 扩展。真实终端入口禁止调用 Agent，检查 Memory 文档未改变；数据库字节及 imported 状态保持不变。
- unittest discover 模式 `test_message*.py`、`test_qq*.py`、`test_email*.py` 合并串行执行：278 项通过（21.723s）。
- `python -m unittest discover -s tests`：444 项通过（70.892s）。
- `git diff --check`：通过。
- 开发期间终端测试夹具曾缺少 client.model，随后又误把正常会话历史日志当作 Memory 文档变化；均已修正并通过重跑。一次并行回归遇到共享 runtime execution.json 的 WinError 32，改为单独运行后 278 项通过。全量测试包含既有弃用提示与预期失败场景诊断输出，最终无失败。

## 已知问题与边界

- 没有全文索引；SQLite 仍扫描候选 payload 并排序，耗时随数据量和 payload 大小增长。100 条是返回/解码上限，不是扫描上限；未提供生产数据性能承诺。
- 遵循 SQLite LIKE 大小写语义（ASCII 不区分大小写，非 ASCII 不做 Unicode 折叠）。null、空对象和空数组不匹配；数字采用 SQLite 文本表示。
- 只搜索已存储 payload，Email 未缓存正文不会被下载或搜索；沿用首次访问旧 Email 索引的迁移机制。未知时间最后，同时间沿用 ID/source 排序。
- 没有字段选择、DSL、用户 SQL、offset、FTS、向量/语义/正则搜索、QQ 上下文或批量导入。
