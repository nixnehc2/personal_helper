# 飞书接入第一阶段：双向通信验证

本阶段目标：手机飞书 ↔ personal_ai_helper 本地程序 双向发送纯文本消息。

## 前置条件

- 已创建飞书自建应用 `personal_helper`
- 已启用机器人、订阅 `im.message.receive_v1`
- 已开通权限：`im:message.p2p_msg:readonly`、`im:message:send_as_bot`
- 已使用**长连接**接收事件（无需公网服务器）
- 应用已发布

## 配置

凭据已保存在项目根目录 `config.local.json`（已加入 `.gitignore`）。

如需通过环境变量覆盖：

```powershell
$env:FEISHU_APP_ID="cli_xxx"
$env:FEISHU_APP_SECRET="xxx"
```

优先级：环境变量 > `config.local.json`。

> App Secret 绝不写入日志、数据库或 Git。

## 安装依赖

```bash
pip install lark-oapi>=1.7.0
```

## 测试手机 → PC（接收）

```bash
python -m agent.feishu_test.receive
```

启动后建立飞书长连接，监听 `im.message.receive_v1` 事件。

手机给机器人发送任意文本，例如 `hello pc`，PC 端预期输出：

```
[feishu] message received
sender_open_id: ou_xxxxx
message_id: om_xxxxx
chat_id: oc_xxxxx
chat_type: p2p
message_type: text
text: hello pc
```

## 测试 PC → 手机（发送）

使用上一步获得的 `sender_open_id`：

```bash
python -m agent.feishu_test.send --open-id ou_xxxxx
```

预期手机收到：

```
Hello from personal_ai_helper
```

终端输出：

```
[feishu] send success
message_id: om_xxxxx
```

## Echo 模式（可选）

一次验证双向链路：

```bash
python -m agent.feishu_test.receive --echo
```

手机发送 `hello` → PC 打印收到 → 自动回复 `收到：hello` → 手机收到回复。

## 验收标准

| 场景 | 验证方法 |
|------|----------|
| A. 手机 → PC | 手机发送 `hello pc`，PC 终端打印 `sender_open_id`、`message_id`、`chat_id`、`text: hello pc` |
| B. PC → 手机 | 执行 `send --open-id <open_id>`，手机收到 `Hello from personal_ai_helper` |
| C. Echo（可选） | 手机发送 `hello`，手机收到 `收到：hello` |
