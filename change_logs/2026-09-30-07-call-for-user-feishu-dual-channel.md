# 2026-09-30: call_for_user 飞书双通道接入

- 日期：2026-09-30（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

为 `call_for_user` 增加飞书手机响应通道。Automation Agent 调用 `call_for_user` 时，电脑事件终端和手机飞书同时显示问题，任意一端先提交有效回答即生效，另一端立即失效。

## 实际改动

### 新增文件

- `agent/user_requests.py`：SQLite 持久化的 UserRequestManager，管理 call_for_user 请求的生命周期（waiting → answered/invalid/cancelled）。实现原子 `submit_answer()` 确保 first-response-wins。
- `agent/feishu_client.py`：Feishu 双通道客户端，通过 lark_oapi 发送问题到飞书、通过 WebSocket 长连接接收回答。支持消息回复匹配、请求失效通知。
- `tests/test_user_requests.py`：UserRequestManager 单元测试（17 项），覆盖创建请求、终端/飞书优先回答、并发提交、不串线、Agent 退出、取消事件、重试。
- `tests/test_feishu_dual_channel.py`：双通道流程集成测试（17 项），覆盖完整流程、FeishuClient 初始化、`_wait_for_answer`、终端输入线程。

### 修改文件

- `agent/event_runtime.py`：
  - 新增 `import queue as _queue`。
  - 新增 `_create_feishu_client()`、`_start_terminal_input_thread()`、`_wait_for_answer()` 辅助函数。
  - `run_event()` 增加 `feishu_client` 和 `request_manager` 参数。
  - `call_for_user` 处理从单终端改为双通道：创建 UserRequest、发送飞书通知、终端/飞书同时等待、first-response-wins。
  - finally 块增加请求失效清理：Agent 退出时标记 waiting 请求为 invalid 并通知飞书。
  - 增加 `active_user_call_id` 和 `active_feishu_client` 跟踪变量。

## 验证

- `python -m pytest tests/test_user_requests.py -v`：17 passed（本次运行）。
- `python -m pytest tests/test_feishu_dual_channel.py -v`：17 passed（本次运行）。
- `python -m pytest tests/test_call_for_user.py tests/test_waiting_for_user.py tests/test_automations.py -v`：31 passed（本次运行，确认已有功能未受影响）。
- `python -m pytest tests/test_feishu_config.py -v`：5 passed（本次运行）。
- `compile(content, ...)` 语法检查：`event_runtime.py`、`feishu_client.py`、`user_requests.py` 均通过。

## 已知问题与边界

- 飞书配置需要在 `config.local.json` 中添加 `FEISHU_USER_OPEN_ID`（用户 open_id）。
- 飞书 WebSocket 长连接在事件子进程内按需创建，不与 daemon 进程共享。如果多个事件进程同时运行，可能创建多个 WebSocket 连接。
- 邮件确认（`pending_email_send`）目前仅支持终端通道，飞书端暂不支持。
- `send_invalidated` 发送纯文本通知，未使用飞书消息卡片。
- 未实现：高风险操作审批、手机主动创建 Agent 会话、GUI、多轮飞书聊天、自动话题管理。
