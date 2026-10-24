# 更新日志

## 1.0.0 - 2026-10-24

- 首个版本。
- 以 `mimo.provider` 接入小米 MiMo 的 OpenAI 兼容接口（文本 + 工具调用）。
- 兼容两代 Provider 协议：MaiBot 1.1.x/1.2.x 的 `message_list`，以及 1.3.15+ 的 `context_items` / `output_items`。
- 识别 MiMo 的「The request was rejected because it was considered high risk」拦截文本：
  - `raise`：按错误处理，触发主程序重试与模型回退；
  - `replace`：替换为安全文案；
  - `ignore`：原样返回。
- 识别 `finish_reason=content_filter`。
- 提供 `/mimo_status` 命令查看命中统计。
- 支持通过 `httpx.MockTransport` 自测，并提供与宿主 1.3.15 Context Item 协议的交叉校验。
