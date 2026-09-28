# 2026-09-29：允许空邮件匹配条件

- 日期：2026-09-29（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

用户希望邮件 automation 规则允许空 `match` 条件（即不指定任何筛选条件），使其匹配所有收到的邮件，而不是必须至少指定一个条件。

## 实际改动

- `agent/automation_triggers.py`：移除 `if not match: raise ValueError("邮件规则至少需要一个匹配条件")` 校验，空 `match={}` 不再被拒绝。`object_fields` 仍确保只允许已知字段。
- `tests/test_automations.py`：从 `test_email_validation_no_network` 中移除 `("match", {})` 作为无效配置用例。
- `tests/test_automation_email.py`：
  - 修复 `create` 辅助方法，使用 `is None` 显式判断代替 `or`，避免空字典被当作 falsy 走默认值。
  - 新增 `test_empty_match_matches_all_emails` 测试，验证空条件匹配所有新邮件。

## 验证

- 代码审查：`matches_email` 函数中所有字段检查均为可选（`if key in match`），空 `match` 直接返回 `True`，逻辑正确。
- 环境缺少 `croniter` 依赖，无法在沙箱中执行测试。需在本地运行 `python -m unittest tests.test_automations tests.test_automation_email -v` 确认。

## 已知问题与边界

- 测试未在沙箱中执行，需本地验证。
- `from_addresses` 仍要求为非空列表（若提供）；空列表 `[]` 仍会报错，仅允许省略该字段。
