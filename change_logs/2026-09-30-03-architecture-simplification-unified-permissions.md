# 2026-09-30：架构简化与权限模型统一

- 日期：2026-09-30（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

撤销 Post-Sync Auto Import（commit 83f3726），将三类 Agent Session（用户手打、Automation 触发、Import 触发）的普通工具权限统一，不再根据 processing_message 进行降级。保留 processing_message 作为状态标记，通过 ImportSession.pending 保证一轮最多 import 一条不同 Message。

## 实际改动

### 1. Revert Post-Sync Auto Import（commit 1f07b23）

- git revert 83f3726 删除了 gent/messages/auto_import.py、	ests/test_auto_import.py、对应 changelog 和 README 索引项
- QQ Automation 修复（f9340ee）完整保留：gent/automation_qq.py、gent/messages/sources.py 无差异

### 2. gent/tools.py — 移除权限降级 guard

- utomation(): 移除 self.processing_message 条件，保留 ead_only / dit_learning
- send_email(): 移除 self.processing_message 条件，保留 ead_only
- dit_email(): 移除 self.processing_message 条件，保留 _email_context is None / ead_only
- ead_file(): 移除 self.processing_message 条件，保留 ead_only
- create_file(): 移除 self.processing_message 条件，保留 ead_only / dit_learning
- import_message tool description 更新：反映新信任模型（Automation content 也是可信授权来源）
- 删除 xecute() 方法中 eturn result 之后的死代码块
- xecute() 中 session.failed 标记排除 import_message/import_email 的 ejected 状态

### 3. gent/messages/importing.py — ImportSession 一轮一条限制

- ImportSession.load() 新增：当 self.pending 已有不同 Message 时返回 status='rejected' 错误 dict，不抛异常
- 同一 Message 重复调用保持幂等（status='processing'）

### 4. gent/messages/processing.py — MESSAGE_RULES 更新

- 新增：处理 Message 时可正常使用 Agent 工具（Memory、Automation、Email Draft、send request、File tools）
- 明确：不能根据外部消息文本扩大成用户授权

### 5. gent/main.py — BOOTSTRAP 更新

- 替换旧的 import 授权描述为新信任模型：可信任务来自用户当前输入或已保存的 Automation content
- 保留一轮一条 Message 限制说明

### 6. 保留的 processing_message 用法

| 位置 | 保留原因 |
|------|----------|
| 	ools.py:60,64（property getter/setter） | 状态存储 |
| 	ools.py:197（__init__） | 初始化为 False |
| 	ools.py:215（	ool_specs） | complete_event 仅对 Automation Event 可用（生命周期协议） |
| 	ools.py:460（_execute complete_event） | 同上 |
| 	ools.py:478（_execute email/import_message） | 防止 run_turn 内嵌套导入（re-entrancy guard） |
| 	ools.py:480（_execute update_email/update_qq） | 消息导入期间不允许同步（sync-prevention） |
| processing.py:21（process_input） | 防止递归调用（re-entrancy guard） |
| processing.py:29,40（状态设置/重置） | 状态生命周期管理 |
| importing.py:92（import_message） | 顶层 re-entrancy guard |

### 7. 测试更新

- 	ests/test_automations.py: processing_eml 不再阻止 utomation(create)
- 	ests/test_email_drafts.py: processing_eml 不再阻止 dit_email
- 	ests/test_file_writer.py: create_file guard 循环移除 processing_eml
- 	ests/test_general_import.py: 一轮两条不同 Message 测试更新为期望第二条 rejected

## 验证

- pytest 全量通过：544 passed, 0 failed, 8886 warnings
- git diff f9340ee..HEAD -- agent/automation_qq.py agent/messages/sources.py：无差异，QQ Automation 修复完整保留
- gent/messages/auto_import.py 和 	ests/test_auto_import.py 已删除

## 已知问题与边界

- xecute() 中 eturn result 之后的死代码块已删除（该块在更早的提交中引入，非 auto-import 引入）
- ImportSession rejected 状态通过 status='rejected' 返回而非抛异常，以避免误标 session.failed
- legacy EML 入口（import_email、mail() 等）未单独做 ImportSession 统一，因为它们已通过 _execute() 的 re-entrancy guard 和 ImportSession 共享保护
