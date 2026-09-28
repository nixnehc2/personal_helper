# QQ 可读消息：文字、@、回复与混合内容

QQ Adapter 逐段提取可读内容，不再要求整条消息全部为 text。转换结果非空时创建统一 Message；没有可读内容时返回 None；结构损坏时报错，由现有同步流程处理失败与 checkpoint。

## 提取规则

| segment | 提取结果 |
| --- | --- |
| text | 原样保留 text，中文、英文、空格、换行均不裁剪 |
| at，qq=123 | `@123` |
| at，qq=all | `@全体成员` |
| reply，id=456 | `[回复:456]` |
| image/file/record/video/face/mface/forward/json/share | 不贡献正文，不否决同条消息的其他文字 |
| 其他结构合法的未知段 | 忽略，不作为同步错误 |

按原 segment 顺序直接拼接，不额外插入空格。只含 @ 或回复标记也属于可读消息。空字符串和空数组跳过；仅含空格/换行的 text 保持既有行为，不做 strip。段必须有字符串 type 和对象 data；text 必须为字符串，at/reply 必须有非空字符串或整数标识。缺失/损坏字段报错。

| 原消息 | content.text |
| --- | --- |
| at(123) + text(" 你看一下") | `@123 你看一下` |
| text("你好 ") + at(123) | `你好 @123` |
| reply(456) + text("好的") | `[回复:456]好的` |
| image + text("看这个") | `看这个` |
| text("看这个") + video/file | `看这个` |
| at(123) + image + text(" 后面的文字") | `@123 后面的文字` |
| reply(456) + text("好的") + image | `[回复:456]好的` |
| json/share + text("看看这个链接") | `看看这个链接` |
| 纯 image/video/file/record/face/mface/forward/json/share | 跳过 |

本阶段不展开回复和转发，不查询 @ 昵称，不读取媒体内容、不做 OCR/语音识别，不提取 JSON/share、小程序或音乐卡片内容，也不增加 QQ API 请求。

## 最小 CQ 字符串兼容

识别完整的 `[CQ:at,qq=123]`、`[CQ:at,qq=all]` 和 `[CQ:reply,id=456]`，输出与 array 段一致；其他完整 CQ 段忽略，前后普通文字继续保留。例如 `看看这个[CQ:image,file=xxx]` 得到 `看看这个`。

先识别 CQ 段，再对普通文字解码 `&#91;`、`&#93;`、`&amp;`，避免将转义后的字面 `[CQ:...]` 二次解析。array 的 text 不做 CQ 解码。CQ 参数另支持 `&#44;`。这不是完整 CQ parser；不完整或不符合最小语法的 CQ 样式字符串仍作为普通文字保留。

## 存储与兼容

- `Message` 顶层、稳定 ID 算法和 SQLite schema 不变。
- `content.text` 是摘要、全文显示及通用导入的主要可读正文。
- 新写入记录的 `content.segments` 保存原始 `message` 的独立深拷贝：array 输入存数组，CQ string 输入存原字符串。媒体与卡片只保留源结构，不提取其内容。纯媒体消息因跳过而不会单独落库。
- 现有全字段搜索仍可匹配 source-specific 原始字段；保留 segments 不等于已解析媒体或卡片。General Import 的结构化内容同样保留它们，均按外部不可信数据处理。
- 老记录没有 segments 仍可照常查询/导入；既有 `INSERT OR IGNORE` 去重不会改写老记录。checkpoint 之前已被旧规则跳过的历史消息不会自动回补，本阶段不重置 checkpoint 或修改同步范围。
- 分页、增量、checkpoint、persistent skip、rollback、自动周期及白名单逻辑均不变。只更新显示为“可读消息”“无文字内容跳过”，内部仍使用 text/skipped。skipped 延续既有计数行为，包括永久跳过会话时的计数。

验证使用合成 NapCat 数据和临时 SQLite；未读取真实 QQ 消息。人工可对含 @、回复、图文、JSON+文字以及纯 JSON 的会话运行既有同步，再用 `/list_messages qq`、`/read_message qq <ID>` 查看提取结果。
