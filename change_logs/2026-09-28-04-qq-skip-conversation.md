# 2026-09-28：QQ 同步跳过会话功能

- 日期：2026-09-28（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

在 `/update_qq` 人工同步过程中，允许用户按 Enter 跳过当前正在加载的长会话或垃圾会话，并将该会话持久化标记为以后不再同步。

## 实际改动

- `agent/qq_sync.py`
  - `QQStore.connect()` 新增 `conversation_states` 表（`scope TEXT PRIMARY KEY, state TEXT NOT NULL`），持久化保存会话跳过状态。
  - `update_qq()` 新增 `skip_event` 参数（`threading.Event`），在每个会话开始前检查持久化 skip 标记，跳过已标记会话。
  - 在分页循环的多个位置（页请求前、网络返回后、消息处理中）检查 `skip_event`，确保能尽快中止。
  - 用户触发 skip 时：回滚本次会话的写入（`db.rollback()`），在单独事务中写入 skip 标记，报告 `conversation_skipped_user` 事件。
  - 持久化 skip 的会话报告 `conversation_skipped` 事件。

- `agent/qq_progress.py`
  - 新增 `conversation_skipped` 事件处理：显示为 "已跳过（已标记忽略）"。
  - 新增 `conversation_skipped_user` 事件处理：显示为 "已跳过，并标记为以后不再同步"。
  - 在 `conversation_start` 事件中，当 `interactive=True` 时显示提示 "按 Enter 可永久跳过当前会话"。

- `agent/tools.py`
  - `update_qq()` 新增 `interactive` 参数（默认 `False`）。
  - 当 `interactive=True` 且为 Windows TTY 时：创建事件兼容的控制台轮询对象，用户直接按 Enter（空行）时设置事件；在同步检查点非阻塞读取，不启动后台 stdin 线程。
  - 当 `interactive=False`（Agent 调用）时：`skip_event=None`，不读取 stdin。

- `agent/main.py`
  - 直接命令路径（`/update_qq`）改为调用 `files.update_qq(interactive=True)`，启用交互式 skip 输入。
  - Agent 工具调用路径仍通过 `files.execute()` 走默认 `interactive=False`。

- `tests/test_qq_sync.py` 新增 8 个测试：
  - `test_persistent_skip_state_skips_conversation`：验证持久化 skip 标记使会话被跳过。
  - `test_skip_event_stops_and_rolls_back_current_conversation`：验证 skip 时回滚并写入 skip 标记。
  - `test_skip_event_continues_to_next_conversation`：验证跳过后继续同步下一个会话。
  - `test_skip_state_survives_reconnection`：验证 skip 状态跨连接持久化。
  - `test_skip_does_not_count_as_error`：验证 skip 不计入失败。
  - `test_skip_preserves_old_messages`：验证已同步的旧消息不被删除。
  - `test_no_skip_event_means_no_skip`：验证 Agent 调用时无 skip_event 正常同步。
  - `test_skip_event_cleared_between_conversations`：验证 skip_event 在会话间被清除。

- `tests/test_qq_progress.py` 新增 2 个测试：
  - `test_pre_marked_skip_shows_skipped_label`：验证预标记 skip 在进度中正确显示。
  - `test_user_skip_event_visible_in_progress`：验证用户 skip 事件在进度中可见。

## 验证

以下为原有草稿记录的历史结果，本次未重新运行该 pytest 命令；本次实测见 07 号日志。

- 运行 `python -m pytest tests/test_qq_sync.py tests/test_qq_progress.py -v`：33 项全部通过。
- 原有 11 项 qq_sync 测试 + 12 项 progress 测试无回归。
- 新增 8 项 skip 功能测试 + 2 项 skip 进度测试。

## 已知问题与边界

- 提交前复查已移除原草稿中的守护 stdin 线程，避免同步结束后吞掉后续命令；现在只在同步检查点轮询 Windows 控制台。网络请求进行中需等待其返回才能处理跳过。
- 非 Windows 或重定向 stdin 时禁用 Enter 跳过，正常同步和进度仍可用。
- 在 Agent 调用路径中（`interactive=False`），不创建 stdin 线程，不会读取 stdin。
- skip 标记保存在 `conversation_states` 表中，与 `messages` 和 `checkpoints` 表共享同一 SQLite 数据库文件。
- 暂不提供取消 skip 标记的 UI（可通过直接操作数据库实现）。
