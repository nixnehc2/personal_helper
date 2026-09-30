# 2026-09-30：飞书接入第一阶段 — 双向文本通信验证

- 日期：2026-09-30（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

验证 personal_ai_helper 与飞书机器人之间的双向文本消息通信能力。
使用飞书长连接（WebSocket）接收事件，无需公网服务器。
本阶段不接入 Agent、Automation、Memory 等业务逻辑。

## 实际改动

### 新增文件

- `agent/feishu_test/__init__.py` — 包初始化
- `agent/feishu_test/config.py` — 凭据加载（config.local.json + 环境变量，Secret 不写日志/错误信息）
- `agent/feishu_test/receive.py` — 手机→PC：长连接监听 `im.message.receive_v1`，支持 `--echo` 自动回复
- `agent/feishu_test/send.py` — PC→手机：向指定 open_id 发送文本消息
- `tests/test_feishu_config.py` — 凭据加载单元测试（5 个用例）
- `docs/feishu_phase1_test.md` — 人工验收说明

### 修改文件

- `agent/llm.py` — `load_config()` 允许列表新增 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`
- `requirements.txt` — 新增 `lark-oapi>=1.7.0`

### 配置

- 飞书 App ID / App Secret 已保存至 `config.local.json`（已在 `.gitignore` 中）

## 验证

- `python -m pytest tests/test_feishu_config.py -v` — 5/5 通过
- `python -m pytest tests/test_agent.py -v` — 25/25 通过（未影响现有功能）
- `python -c "from agent.llm import load_config; load_config()"` — 正常加载含飞书键的配置
- 语法检查 `ast.parse` — config.py、receive.py、send.py 全部通过
- `git diff` 确认不含 App Secret 明文
- `rg` 扫描确认 `cli_aa36d66fe8789be0` 和 `td6d1A2n8s1eWg0cAU09ATTVALxzZhas` 未出现在任何 Git 跟踪文件中

## 已知问题与边界

- 只处理 `text` 类型消息；image/file/audio/card/post 等类型打印 `unsupported message type` 后忽略
- 不持久化任何 open_id / chat_id / message_id（仅打印）
- 不接入现有 Message / Agent / Automation 系统
- 长连接使用 SDK 默认自动重连，未实现自定义重连框架
- `lark-oapi` 的 protobuf 依赖会触发 `pkg_resources` 弃用警告（SDK 上游问题，非本项目代码）
- 手机→PC 和 PC→手机的实际端到端测试需要飞书后台凭据和网络，尚未在本次提交中执行
