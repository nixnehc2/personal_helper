# 原始邮件

保存导入邮件的原始 `.eml`，按原始字节 SHA-256 去重。这里是不可变原始归档，处理后也不删除或覆盖。

邮件正文先经 MIME parser 解码，不向模型直接加载原始 Base64。普通检索不扫描原始邮件；压缩历史见 [email_threads](../../history/email_threads/_INDEX.md)。
