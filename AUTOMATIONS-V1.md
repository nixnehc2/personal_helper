# 自动化规则第一阶段

仅持久化和管理规则。没有调度器、邮箱同步、事件匹配、游标推进、Agent 自动调用或通知。
无论 active、paused 还是 cancelled，都不会执行。规则独立于 Memory 和聊天记录；/clear、/cancel 和退出不会删除规则。

## 存储与配置

SQLite：项目 `data/automations.sqlite3`，数据库及日志文件已排除 Git。
稳定自增编号；取消保留记录；预留运行字段为空，pending_events 为 []。
实际时间（包括 at/start_at、created_at/updated_at）存为 UTC。cron 时区和展示时区属于配置，保留 IANA 名称。
可在现有 config.local.json 中增加 `"AGENT_TIMEZONE": "Asia/Shanghai"`，也支持同名环境变量；配置文件优先，未配置默认 Asia/Shanghai。
每个时间规则可传 timezone 覆盖默认展示时区。创建时固化默认时区，后续修改全局配置不会改变已有规则。
邮箱 scope.account_id 使用现有 EMAIL_ACCOUNT（配置文件优先于环境变量），folder 必填，例如 INBOX；只检查本地配置，不连接邮箱。

## 时间语义

采用 [croniter 6.0.0](https://github.com/pallets-eco/croniter) 和 tzdata。
第一版限定数字 Unix 五字段：分 时 日 月 星期；支持星号、列表、范围和步长（* , - /）。
星期 0/7 为周日，1 为周一；日和星期均有限定时使用 OR。拒绝秒/年份字段、宏、名称、L/W/# 等扩展；不能准确表达的需求应说明限制，不能近似替换。
cron 使用带时区 datetime，由 croniter 计算（夏令时行为遵循该固定版本）；未来八年无有效时间则明确报错。
interval 从 start_at 锚定，按 UTC 实际经过秒数推进，不按整点重新对齐；返回当前时刻及之后最多三次。cron 返回当前时刻之后三次。
once 返回未来指定时刻一次；已经过去则预览为空并说明，不改变 active 状态。
missed_policy=latest/skip 必填，仅保存以后补最近一次或跳过的意图；本阶段预览只展示未来时间，不补执行，不修改进度。
once 强制 mode=once，interval/cron 强制 continuous。修改 schedule_type 且未指定 mode 时自动采用对应模式，显式冲突则报错。
邮件 mode 可为 once 或 continuous；match 条件之间 AND，from_addresses 内 OR。未填写的匹配字段不参与约束。

## 聊天命令

在原有 `python -m agent.main` 聊天中使用，命令和 Agent 均调用 automation 工具。

```text
/automation list
/automation get 1
/automation create {"name":"申请提醒","trigger_type":"schedule","trigger_config":{"schedule_type":"interval","start_at":"2026-09-28T08:00:00+08:00","interval_seconds":5400,"missed_policy":"latest"},"content":"提醒用户检查申请材料是否齐全并提交申请。"}
/automation update 1 {"name":"材料检查","content":"提醒用户检查申请材料中的成绩单和推荐信。"}
/automation pause 1
/automation resume 1
/automation cancel 1
```

create 接受 name、trigger_type、source、trigger_config、content、mode；update 只接受 name、trigger_config、content、mode。
trigger_config 修改采用整体替换，避免遗留旧字段。trigger_type/source 不可修改，跨类型转换请新建。
get/list 包括取消的记录；重复暂停、恢复或取消返回当前状态且不刷新修改时间。取消后恢复报错，需重新创建。
已取消记录仍允许修改描述或配置，但不会恢复。completed 仅预留，不会自动产生。

## 人工验收（无需真实邮箱操作）

1. `python -m pip install -r requirements.txt`，启动现有聊天。用上面 create 命令创建间隔规则，记下实际编号；检查说明、时区、完整指令和三次时间预览。
2. 自然语言请求“保存一个 2030 年 9 月 28 日北京时间上午八点提醒我检查申请材料的一次性规则”；核对工具调用和“规则已保存，自动检查和提醒尚未接入”。不需要恢复旧对话。
3. 请求“每周一、三、五晚上八点提醒我检查申请进度，北京时间”；核对 Cron `0 20 * * 1,3,5` 和预览星期。
4. 使用真实本地 EMAIL_ACCOUNT 值作为 account_id（不要猜测），创建 event/email 规则，trigger_config 为 `{"scope":{"account_id":"本地账号","folder":"INBOX"},"match":{"from_addresses":["teacher@example.com"],"subject_contains":"申请"},"check_interval_seconds":300}`，content 写完整处理指令。核对无邮箱连接。
5. 用 list/get 查询四种规则。对实际编号 update、pause、resume、cancel；每步退出并重启，再查询，确认持久化。取消后 resume 应报错。
6. 用 update 提交非法 Cron、无效时区、零或负间隔；查询确认原记录未变。查询不存在的编号确认明确报错。/clear 和 Memory /cancel 后再次查询规则。
7. 进行普通聊天、Memory 临时修改和 review，以及原有邮件草稿操作，确认原入口工作正常。

自动测试：`python -m unittest discover -s tests -p test_automations.py -v`。
全量回归：`python -m unittest discover -s tests -v`。测试使用临时数据库、假 Agent，不连接邮箱。
