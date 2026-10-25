# MaiBot-MiMo-rollback

作者：myxxr ｜ 许可证：MIT ｜ 插件 ID：`myxxr.mimo-rollback`

MaiBot 第三方插件：把**小米 MiMo**（OpenAI 兼容接口）接入为 `mimo.provider` 这个 `client_type`，并在 Provider 层识别 MiMo 的「风险内容拦截」返回。

## 兼容性

- MaiBot **1.1.0 ~ 1.3.99**（`host_application` 声明区间；实测 1.1.4、1.3.15）：
  - **1.1.x / 1.2.x**：Provider 请求为 `message_list`，插件按旧协议解析并返回 `content` / `tool_calls`。
  - **1.3.x（实测 1.3.15）**：Provider 请求为 `context_items`，插件会先把 Context Item 投影为 OpenAI messages，并在返回时附带原生 `output_items`（`item_schema_version=1`），不再走旧版兼容入口。
- 1.4+ 尚未验证，不在声明的兼容区间内；确认可用后再放宽 `max_version`。
- SDK：`2.0.0` ~ `2.99.99`（已在 2.7.1 与 2.16.0 上验证）。
- 未实现 `embedding` / `audio_transcription`，这两类请求会抛 `NotImplementedError`，请交给其他 Provider。

## 背景

MiMo 官方服务在检测到风险内容时，**不会返回 HTTP 错误**，而是把下面这句话当作模型的正常回答返回：

```
The request was rejected because it was considered high risk
```

如果直接用内置 `openai` client_type 接入，这句话会被 MaiBot 当成正常回复发给用户。本插件把它识别为错误（默认策略），让主程序按自己的重试 / 模型回退流程处理，因此这句话不会再出现在聊天里；如果「模型管理 → 功能分配」里该功能还配置了其他模型，会自动改用其他模型继续回答。

## 功能

- `llm_provider` 组件：`client_type = mimo.provider`，支持文本对话与 OpenAI 风格工具调用。
- 风险拦截识别：关键词匹配（大小写不敏感）+ `finish_reason=content_filter` 兜底。
- 三种处理策略：
  - `raise`（默认）：抛错 → 主程序重试当前模型，失败后切换任务里的下一个模型；
  - `replace`：把命中文本替换为可配置的安全文案后继续回复；
  - `ignore`：保持旧行为（仅用于排查）。
- `/mimo_status` 命令：查看当前策略、已处理响应数、风险命中次数。**受保护命令（`permission="operator"`）**，MaiBot 1.3.15+ 下仅管理员可用。
- 鉴权取自模型配置里的 `api_providers`（`api_key` / `base_url` / `auth_type` 等），插件不硬编码密钥；未配置时可用环境变量 `MIMO_API_KEY` 兜底。

## 命令权限

`/mimo_status` 在代码里声明为 `permission="operator"`：

- **MaiBot 1.3.15+**：在 WebUI「bot 配置 → 命令权限」里能看到它标记为「受保护」。需要在同一页面把执行人加入「全局管理员」（格式 `平台:用户ID`，例如 `qq:123456789`），或用「仅为此命令放行用户 / 放行聊天」单独放行；没有权限的用户执行会收到提示（可勾选「不显示无权限提示」改为静默拦截）。
- **MaiBot 1.1.x / 1.2.x**：宿主没有命令权限机制，`permission` 元数据会被忽略，命令仍是公开的；如需限制，只建议在可信环境中部署。


## 安装

把插件目录（包含 `_manifest.json` 与 `plugin.py`）放进 MaiBot 的 `plugins/` 下，两种方式任选：

- 直接复制目录：
  ```powershell
  Copy-Item -Recurse -Force "<插件目录>" "C:\...\MaiBot-1.1.4\plugins\maibot-mimo-rollback"
  ```
- 或解压发布包 `maibot-mimo-rollback-v1.0.2.zip`，把其中的 `maibot-mimo-rollback` 文件夹放进 `plugins/`。

然后重启 MaiBot（或在 WebUI 插件管理中启用/重载）。插件首次加载后会生成 `config.toml`。

## 模型配置

在 WebUI「模型配置」里新增一条 API Provider，并让目标模型任务使用它：

- `name`：随便起，例如 `mimo`
- `client_type`：`mimo.provider`（必须与插件声明一致）
- `base_url`：`https://api.xiaomimimo.com/v1`
- `api_key`：你的 MiMo API Key
- `auth_type`：`bearer`（默认）或 `header`
- `timeout`：建议 `60`

对应模型条目的 `model_identifier` 填 MiMo 的真实模型名，例如 `mimo-v2.6-flash`、`mimo-v2.6-pro`。

### 安全注意事项

- **优先使用 `https` 与可信域名**。`base_url` 配成 `http://` 或内网/回环/链路本地地址（含 `169.254.169.254` 这类云元数据地址）时，`api_key` 与聊天内容会明文传输或发往非预期主机；插件会在首次使用时输出告警，但不会替你判断该地址是否可信。
- **不要使用 `auth_type=query`**：它会把 `api_key` 放进 URL 查询参数，容易被代理、网关或访问日志记录下来；建议用 `bearer` 或 `header`。插件在该模式下会输出一次性告警。
- **插件不跟随任何 HTTP 重定向**：遇到 `3xx` 会直接报错。这是为了避免重定向时 `Authorization` 被 httpx 剥掉、但附加 Header 与请求体（含聊天内容）仍被带到重定向目标。请把 `base_url` 直接配置为最终地址。
- 不要把密钥写进 `base_url` 或 `request.extra_headers` 之外的任何配置文本。


## 回退模型放在哪里

回退模型就是**「模型管理 → 功能分配」里同一个功能配置的其他模型**，不需要额外配置：

1. 在「模型管理 → 功能分配」里选中对应功能（如 `replyer`），把 MiMo 模型和至少一个其他模型都加进该功能的模型列表；
2. 模型列表的 `selection_strategy` 决定切换顺序：
   - `sequential`：按列表顺序优先，失败后切到列表里的下一个（回退顺序最可控）；
   - `balance`（默认）：按负载 / 失败惩罚选择，失败模型会被排除；
   - `random`：随机选择；
3. 主程序的调度逻辑是 `max_attempts = len(该功能的 model_list)`：当前模型失败后换下一个，直到列表耗尽才报错。

因此 `strategy=raise` 时：MiMo 返回拦截文本 → 插件抛错 → 主程序先在 MiMo 上重试（次数为 Provider 的 `max_retry`）→ 仍未成功则自动切换到「功能分配」里的其他模型。若该功能只配了 MiMo 一个模型，则会直接失败、不发送拦截文本。

> 想让回退更快，可以把 MiMo 这条 API Provider 的 `max_retry` 调小（例如 `1`）；默认 `3` 意味着一次拦截会先在 MiMo 上多试几次。


## 插件配置（config.toml，WebUI 可编辑）

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 是否启用插件 |
| `risk.strategy` | `raise` | `raise` / `replace` / `ignore` |
| `risk.patterns` | 见下 | 风险关键词列表，大小写不敏感 |
| `risk.match_mode` | `contains` | `contains` 包含即命中；`equals` 完全一致才命中 |
| `risk.treat_content_filter_as_risk` | `true` | `finish_reason=content_filter` 同样按风险处理 |
| `risk.scan_reasoning` | `false` | 是否检查 `reasoning_content` |
| `risk.replace_text` | `抱歉，这条回复涉及敏感内容，换个话题再聊吧。` | `replace` 策略的替换文案 |
| `request.base_url_override` | 空 | 覆盖 `api_providers` 的 `base_url`，一般留空 |
| `request.timeout_seconds` | `0` | `0` 表示沿用 `api_providers.timeout`（默认 60 秒） |
| `request.extra_headers` | `{}` | 附加 HTTP Header |
| `diag.log_risk_hits` | `true` | 命中时输出警告日志 |
| `diag.include_raw_data` | `true` | 在返回值中带上 MiMo 原始响应 |

默认风险关键词：

```text
The request was rejected because it was considered high risk
was rejected because it was considered high risk
considered high risk
```

## 验证

1. 启动 MaiBot，确认插件加载成功（日志里出现「MaiBot-MiMo-rollback 已加载」）。
2. 在聊天里发送 `/mimo_status`，应返回策略与统计信息。
3. 把 `mimo.provider` 配给某个模型任务，正常对话应来自 MiMo。
4. 构造一条会触发 MiMo 拦截的输入（例如让模型复述一段风险内容），观察日志出现「MiMo 返回风险拦截内容」；`raise` 策略下这句话不会出现在聊天里，而是触发重试 / 回退。

## 常见问题

- **插件加载失败：Manifest 校验错误** — 确认 `manifest_version` 为 `2`，`id` 小写并用点号/横线分隔，`version` 为三段式，`llm_providers[].client_type` 与 `@LLMProvider("mimo.provider")` 完全一致。
- **加载时报「未注册的能力」** — `capabilities` 与代码要一致：本插件只用了 `ctx.send.text`，所以声明了 `send.text`。
- **模型配置里找不到 `mimo.provider`** — 确认插件已启用且加载成功；`client_type` 冲突时（两个插件声明同一个值）双方都会被阻止加载。
- **⚠️ 所有回复都变成「抱歉，这条回复涉及敏感内容」** — 说明 `risk.match_mode=contains` 且模型回答里恰好包含风险关键词；把 `match_mode` 改成 `equals`，或从 `risk.patterns` 删掉过宽的关键词。
- **不想要「替换文案」，只想触发模型回退** — 保持默认 `strategy=raise`，并在「模型管理 → 功能分配」里给该功能配置多个模型，其余模型就是回退目标。
- **日志出现「模型 'xxx' 尝试失败，切换到下一个模型」** — 正常现象：`raise` 策略正是靠这条链路把请求交给功能分配里的其他模型。
- **请求返回 HTTP 401** — 检查 `api_providers` 的 `api_key` 与 `auth_type`，或设置环境变量 `MIMO_API_KEY`。

## 已知限制

- 插件 Provider 只实现 `response`（文本 / 工具调用）；`embedding`、`audio_transcription` 会抛 `NotImplementedError`，请用其他 Provider 承担对应任务。
- 暂不支持 Host 侧流式处理器；流式输出需在插件内部完成，这里直接返回最终结果。
- 只识别关键词与 `content_filter`。MiMo 的拦截文案若发生变化，请在 `risk.patterns` 中补充。

## 开发与自测

```powershell
# 在插件所在目录（plugins/ 的上一级）执行：
& <MaiBot 的 python> plugins\maibot-mimo-rollback\tests\selftest.py `
    --sdk "C:\path\to\maibot-plugin-sdk" `
    --maibot-ref "C:\path\to\MaiBot-1.3.15\源码目录"
```

- `--sdk`：`maibot_sdk` 所在目录；不传时依次尝试 `sdk-ref/maibot-plugin-sdk-main`、与插件目录并列的 `MaiBot-*/maibot-plugin-sdk-*`，也可用环境变量 `MAIBOT_SDK_PATH`。
- `--maibot-ref`（可选）：MaiBot 1.3.15 源码目录。提供后会直接加载宿主的 Context Item 纯数据模块，
  用宿主自己的反序列化与关系校验验证插件返回的 `output_items`。

自测脚本会检查：清单与 `@LLMProvider` 声明一致、两种请求协议的转换、工具/响应格式转换、
风险关键词识别、三种策略行为，以及用 `httpx.MockTransport` 模拟 MiMo 接口跑通完整 `get_response`。

> 发布包 `maibot-mimo-rollback-v1.0.2.zip` 只包含运行所需文件（`_manifest.json`、`plugin.py`、`README.md`、`CHANGELOG.md`、`.gitignore`），不含 `tests/`，自测保留在仓库里。
