# 2026-09-28：QQ 稳定身份白名单与启动同步

- 日期：2026-09-28（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

以人工数字选择生成稳定 QQ 会话白名单，仅在长期 Automation Checker 启动时同步选定会话一次；保留 Email 调度与手动全量 QQ 同步，不重构 Message/Automation。

## 实际改动

- 新增 `agent/qq_conversations.py`，统一群聊/好友发现、名称选择、排序、临时编号与 `(type, str(id))` 身份；样本读取、selector、同步共同复用，保留同步时单类发现失败的隔离行为。
- 新增独立人工入口 `python -m agent.qq_sync_selector`：逗号/空格数字选择，非法输入重试，去重，展示结果并仅在 `yes` 后整体替换；空选择清空列表。临时文件写入并 flush/fsync 后原子 replace，失败保留旧配置。
- `update_qq` 增加可选稳定身份集合过滤；默认仍全量，永久 skip 优先。未找到会话警告，未选会话不进入消息/checkpoint 更新流程。
- checker 在取得循环锁后仅启动同步一次；缺失、空、非法配置及连接/同步失败均继续 Timer/Email 循环，不重试或回退全量。`--once` 不触发 QQ。
- 不新增 Agent 配置工具，不生成或修改真实白名单；配置继续由已有 `/data/qq/` 忽略规则保护。
- 新增合成数据测试，更新样本读取测试使用统一字段；新增 [使用说明](docs/QQ-STARTUP-SYNC.md)，更新完整说明和索引。

## 验证

- 本次执行 `python -m pytest tests/test_qq_whitelist.py tests/test_qq_sync.py tests/test_qq_progress.py tests/test_qq_sample_reader.py tests/test_automation_checker.py tests/test_automation_email.py -q`：最终 **121 passed**，16.62 秒。
- 覆盖稳定身份保存、改名/插入会话后编号变化、只访问白名单历史、未选消息/checkpoint 保留、永久 skip 优先、手动全量回归、确认取消/覆盖、原子写入及 replace 失败、缺失/空/非法名单、失联会话、连接失败继续循环、锁顺序/锁失败、进程启动一次、`--once` 零次，以及现有 Email 间隔检查与 QQ 进度/回滚测试。
- 本次 `git diff --check`：通过。
- 以上均为本任务实际运行结果，未引用历史通过记录。

## 已知问题与边界

- 测试产生 156 条现有 `agent/tools.py:231` 的 `PurePath.is_reserved()` 弃用警告，本阶段未修改该代码。
- 未连接真实 NapCat、未运行交互式人工配置或真实 QQ 历史同步；真实服务验收由用户按使用说明操作，不触碰个人名单和 Message。
- 未运行全仓库测试；本次运行与改动相关的六个测试文件。白名单变更下次 checker 启动生效，QQ 不成为 Automation 事件源。
