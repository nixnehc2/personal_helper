# 2026-10-03：移除邮件下载的 RFC822.SIZE 硬校验

- 日期：2026-10-03（Asia/Shanghai）
- 对应提交：与本日志同一提交

## 目的

IMAP 成功返回邮件 BODY 时，不因 RFC822.SIZE 与实际正文长度不同而拒绝下载。

## 实际改动

- `agent/email_import.py`：移除 RFC822.SIZE 缺失或与正文长度不同导致失败的判断，保留正文 bytes 类型检查和现有后续流程；保留 SIZE 获取逻辑。
- `tests/test_email_import.py`：新增 SIZE=100 与实际长度不一致时原始字节返回、缓存及导入成功的回归测试，以及大小相等时原始字节返回测试；从原拒绝测试移除 SIZE 情况，保留其他身份校验测试。
- 更新本目录索引。未修改 Automation、邮件索引、缓存格式及其他功能。

## 验证

以下均为本次执行结果：

- `python -m unittest discover -s tests -p test_email_import.py`：22 项通过。
- `python -m unittest discover -s tests -p test_automation_email.py`：12 项通过。
- 搜索 `agent` 中 RFC822.SIZE 和正文长度比较：SIZE 仅保留 FETCH 请求及元数据提取，不再参与下载成功判定。
- `git diff --check`：通过。

## 已知问题与边界

- 测试输出包含现有 pathlib 弃用提示、子进程 ResourceWarning 和 QQ 白名单未配置提示；没有测试失败。
- 本次使用模拟 IMAP 回归验证，修改后未执行真实邮箱导入。
- 工作区既有未跟踪的 `data/` 与 `patch_tick.py` 不纳入提交。
