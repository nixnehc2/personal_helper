# 2026-10-01：complete_event 参数校验拆分与 message 别名兼容

- 日期：2026-10-01（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

Automation 独立事件会话中，`complete_event` 的所有错误条件（参数结构、字段名、类型、空值、Memory 事务、待发邮件）被统一用同一条错误信息返回，导致 Agent 将参数错误误判为 Memory/Email 状态问题，进而错误操作无关资源。

## 实际改动

- **`agent/tools.py`**（`_execute` 方法中 `complete_event` 分支）：
  - 拆分原来单一复合 `if` 为按顺序的独立校验，每种错误返回精确信息：
    1. `arguments` 不是 dict → 提示"参数必须是对象"
    2. 参数字段不是 `reply` → 提示"只接受 reply 参数"及正确格式
    3. `reply` 不是字符串 → 提示"必须是字符串"
    4. `reply` 为空 → 提示"不能为空"
    5. Memory 事务 active → 提示"请先提交或取消 Memory 事务"
    6. `pending_email_send` 存在 → 提示"请先处理该请求"
  - 新增 `message → reply` 兼容层：当参数严格为 `{"message": 非空字符串}` 时自动映射为 `{"reply": ...}`，仅此一种别名，不做泛化。
- **`tests/test_complete_event.py`**（新增）：11 个回归测试覆盖全部校验分支。

## 验证

- `python -m pytest tests/test_complete_event.py -q`：11 passed。
- `python -c "import py_compile; py_compile.compile('agent/tools.py', doraise=True)"`：语法 OK。
- 完整测试套件因 `data/runtime` 中大量残留锁文件导致 `run_turn` 类测试超时，属预存在问题，非本次修改引入。

## 已知问题与边界

- `data/runtime` 目录存在大量历史锁文件，使用 `run_turn` 的测试在当前环境下会阻塞于调度器锁获取，需要清理或改进测试隔离，不在本次修复范围内。
- `message → reply` 兼容仅限严格 `{"message": 非空字符串}`，其他别名（如 `text`、`content`）不做映射。
- 本次未修改 Memory Transaction、Automation、QQ Import、Email Draft 或 `call_for_user` 整体架构。
