# 更新日志

## 1.0.2 - 2026-10-25

- manifest 增加 `plugin_type: "provider"`。
- 安全加固：
  - HTTP 客户端不再跟随重定向，`3xx` 直接报错，避免附加 Header 与请求体（含聊天内容）被带到重定向目标；
  - `base_url` / `base_url_override` 增加协议与主机校验：非 `http(s)` 直接报错，`http` 明文与内网/回环/云元数据地址输出告警；
  - `auth_type=query` 首次使用时输出告警（key 会进入 URL 查询参数，可能被访问日志记录）。

## 1.0.1 - 2026-10-24

- `/mimo_status` 改为受保护命令（`permission="operator"`）：MaiBot 1.3.15+ 下仅管理员/被放行的用户或聊天可执行。
- `host_application` 兼容区间收敛为 `1.1.0 ~ 1.3.99`（与实测的 1.1.4、1.3.15 对齐，不再声称兼容未验证的 1.x 未来版本）。

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
