"""MaiBot-MiMo-rollback 插件。

小米 MiMo 官方服务在检测到风险内容时，不会返回 HTTP 错误，而是把
``The request was rejected because it was considered high risk`` 这句话
当作模型的正常回答返回。如果直接使用内置的 ``openai`` client_type 接入，
这句话会被 MaiBot 当成正常回复发给用户。

本插件以 ``llm_provider`` 组件接入 MiMo 的 OpenAI 兼容接口，并在返回前做
拦截识别：

- ``raise``（默认）：抛出异常，主程序会按自己的重试 / 模型回退策略
  处理，这句拦截文本永远不会被当成回答发送；
- ``replace``：把命中文本替换为可配置的安全文案后继续回复；
- ``ignore``：保持旧行为，原样返回（仅用于排查问题）。

插件还提供 ``/mimo_status`` 命令，便于在聊天里查看风险命中统计。

设计说明：
- 只实现 ``response``（文本 / 工具调用）能力；MiMo 的 embedding、
  音频转写请求请交给其他 Provider。
- 鉴权信息来自模型配置的 ``api_providers``（``base_url``、``api_key``、
  ``auth_type`` 等），插件自身不保存密钥；也可用环境变量 ``MIMO_API_KEY``
  兜底。
"""

from __future__ import annotations

import ipaddress
import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from maibot_sdk import Command, Field, LLMProvider, LLMProviderBase, MaiBotPlugin, PluginConfigBase

DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
CHAT_COMPLETIONS_PATH = "/chat/completions"
DEFAULT_TIMEOUT_SECONDS = 60.0

# Host 1.3.15+ 的 Context Item 快照版本；插件返回 output_items 时需要原样声明。
ITEM_SCHEMA_VERSION = 1

# MiMo 在检测到风险内容时返回的拦截文本（大小写不敏感匹配）。
DEFAULT_RISK_PATTERNS = [
    "The request was rejected because it was considered high risk",
    "was rejected because it was considered high risk",
    "considered high risk",
]

DEFAULT_REPLACE_TEXT = "抱歉，这条回复涉及敏感内容，换个话题再聊吧。"


# ===== 配置模型 =====


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 MaiBot-MiMo-rollback 插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class RiskConfig(PluginConfigBase):
    """风险拦截内容处理配置。"""

    __ui_label__ = "风险内容处理"
    __ui_icon__ = "shield-alert"
    __ui_order__ = 1

    strategy: str = Field(
        default="raise",
        description="raise：按错误处理（触发主程序重试与模型回退）；replace：替换为安全文案后回复；ignore：原样返回",
        json_schema_extra={
            "label": "处理策略",
            "hint": "建议保持 raise：MiMo 的拦截文本永远不会被当成回答发给用户，改由主程序切换其他模型兜底",
        },
    )
    patterns: List[str] = Field(
        default_factory=lambda: list(DEFAULT_RISK_PATTERNS),
        description="风险拦截文本关键词列表，大小写不敏感",
        json_schema_extra={
            "label": "风险关键词",
            "hint": "默认覆盖 MiMo 的 The request was rejected because it was considered high risk；可按实际返回微调",
        },
    )
    match_mode: str = Field(
        default="contains",
        description="关键词匹配方式：contains（包含即命中）或 equals（内容完全一致才命中）",
        json_schema_extra={"label": "匹配方式", "hint": "contains 更稳妥；equals 可减少「模型讨论该句子」时的误判"},
    )
    treat_content_filter_as_risk: bool = Field(
        default=True,
        description="finish_reason 为 content_filter 时同样按风险内容处理",
        json_schema_extra={"label": "content_filter 视为风险"},
    )
    scan_reasoning: bool = Field(
        default=False,
        description="是否同时检查 reasoning_content 中的风险关键词",
        json_schema_extra={"label": "检查推理内容", "hint": "一般不需要；仅在 MiMo 把拦截文本放进推理字段时打开"},
    )
    replace_text: str = Field(
        default=DEFAULT_REPLACE_TEXT,
        description="strategy=replace 时替换使用的安全文案",
        json_schema_extra={"label": "替换文案"},
    )


class RequestConfig(PluginConfigBase):
    """HTTP 请求配置。"""

    __ui_label__ = "请求"
    __ui_icon__ = "send"
    __ui_order__ = 2

    base_url_override: str = Field(
        default="",
        description="覆盖模型配置中的 base_url；留空时使用 api_providers 的 base_url，仍为空则用 https://api.xiaomimimo.com/v1",
        json_schema_extra={"label": "Base URL 覆盖", "hint": "一般留空即可，在模型配置的 api_providers 里填写更直观"},
    )
    timeout_seconds: int = Field(
        default=0,
        ge=0,
        description="请求超时秒数；0 表示沿用 api_providers 的 timeout（默认 60 秒）",
        json_schema_extra={"label": "超时(秒)"},
    )
    extra_headers: Dict[str, str] = Field(
        default_factory=dict,
        description="附加到每个请求的 HTTP Header（可选，例如组织标识）",
        json_schema_extra={"label": "附加 Header"},
    )


class DiagConfig(PluginConfigBase):
    """诊断日志配置。"""

    __ui_label__ = "诊断"
    __ui_icon__ = "bug"
    __ui_order__ = 3

    log_risk_hits: bool = Field(default=True, description="命中风险关键词时向日志输出警告")
    include_raw_data: bool = Field(
        default=True,
        description="把 MiMo 原始响应放进 raw_data，便于排查解析问题（会增加内存占用）",
    )


class MiMoPluginConfig(PluginConfigBase):
    """MaiBot-MiMo-rollback 插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    request: RequestConfig = Field(default_factory=RequestConfig)
    diag: DiagConfig = Field(default_factory=DiagConfig)


# ===== 异常 =====


class MiMoProviderError(RuntimeError):
    """MiMo Provider 调用失败。"""


class MiMoRiskContentError(MiMoProviderError):
    """MiMo 以正常回答形式返回了风险内容拦截文本。"""


# ===== 纯函数工具（便于单元测试） =====


def _as_dict(value: Any) -> Dict[str, Any]:
    """把任意值安全地当作字典读取。"""

    return value if isinstance(value, dict) else {}


def _truncate(text: str, limit: int) -> str:
    """截断长文本，避免错误信息过长。"""

    normalized = str(text or "")
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "…"


def _is_private_or_metadata_host(host: str) -> bool:
    """判断主机是否为回环、内网、链路本地或云元数据地址。"""

    normalized = str(host or "").strip().strip("[]").lower()
    if not normalized:
        return False
    if normalized in {"localhost", "metadata.google.internal"}:
        return True
    if normalized.endswith((".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def inspect_base_url(url: str) -> List[str]:
    """校验 base_url 并返回安全提醒列表。

    Raises:
        MiMoProviderError: 当 URL 不是合法的 http(s) 地址时抛出。
    """

    raw_url = str(url or "").strip()
    try:
        parsed = urlparse(raw_url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise MiMoProviderError(f"base_url 无法解析: {raw_url!r}") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise MiMoProviderError(f"base_url 必须是包含主机名的 http(s) 地址: {raw_url!r}")

    warnings: List[str] = []
    if parsed.scheme.lower() == "http":
        warnings.append("base_url 使用 http 明文传输，api_key 与聊天内容可能被窃听")
    if _is_private_or_metadata_host(hostname):
        warnings.append(f"base_url 指向内网/回环/云元数据地址（{hostname}），请确认这是可信代理")
    return warnings


def detect_risk_hit(
    *,
    content: Optional[str],
    reasoning: Optional[str],
    finish_reason: Optional[str],
    patterns: List[str],
    match_mode: str,
    treat_content_filter: bool,
    scan_reasoning: bool,
) -> Optional[str]:
    """判断本次响应是否是 MiMo 的风险内容拦截。

    Returns:
        命中说明；未命中时返回 ``None``。
    """

    candidates: List[Tuple[str, str]] = []
    if isinstance(content, str) and content.strip():
        candidates.append(("content", content))
    if scan_reasoning and isinstance(reasoning, str) and reasoning.strip():
        candidates.append(("reasoning_content", reasoning))

    normalized_mode = str(match_mode or "contains").strip().lower() or "contains"
    normalized_patterns = [str(pattern or "").strip() for pattern in patterns or []]
    normalized_patterns = [pattern for pattern in normalized_patterns if pattern]

    for field_name, text in candidates:
        normalized_text = text.strip().casefold()
        for pattern in normalized_patterns:
            normalized_pattern = pattern.casefold()
            if normalized_mode == "equals":
                matched = normalized_text == normalized_pattern
            else:
                matched = normalized_pattern in normalized_text
            if matched:
                return f"{field_name} 命中风险关键词：{pattern}"

    if treat_content_filter and isinstance(finish_reason, str) and finish_reason.strip():
        if finish_reason.strip().lower() == "content_filter":
            return "finish_reason=content_filter"

    return None


def _normalize_content(raw_content: Any) -> Optional[str]:
    """把 MiMo 返回的 content（字符串或内容数组）统一为字符串。"""

    if isinstance(raw_content, str):
        return raw_content
    if isinstance(raw_content, list):
        texts: List[str] = []
        for part in raw_content:
            if isinstance(part, dict) and str(part.get("type") or "").lower() == "text":
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "".join(texts) if texts else None
    return None


def _build_message_content(parts: Any) -> Any:
    """把内部消息 parts 转换为 OpenAI 消息 content。"""

    if not isinstance(parts, list):
        return None

    text_parts: List[str] = []
    content_parts: List[Dict[str, Any]] = []
    has_media = False

    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "").strip().lower()

        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
                content_parts.append({"type": "text", "text": text})
            continue

        if part_type == "refusal":
            refusal = part.get("refusal")
            if isinstance(refusal, str) and refusal:
                text_parts.append(refusal)
                content_parts.append({"type": "text", "text": refusal})
            continue

        if part_type == "image":
            image_base64 = part.get("image_base64")
            if isinstance(image_base64, str) and image_base64:
                image_format = str(part.get("image_format") or "png").strip().lower() or "png"
                if image_format == "jpg":
                    image_format = "jpeg"
                has_media = True
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/{image_format};base64,{image_base64}"},
                    }
                )

    if has_media:
        return content_parts
    if text_parts:
        # 片段本身就是按顺序切分的连续文本，直接拼接，不额外插入分隔符。
        return "".join(text_parts)
    return None


def _convert_tool_calls(raw_tool_calls: Any) -> List[Dict[str, Any]]:
    """把内部 assistant 消息里的 tool_calls 转换为 OpenAI 请求格式。"""

    if not isinstance(raw_tool_calls, list):
        return []

    converted: List[Dict[str, Any]] = []
    for index, raw_call in enumerate(raw_tool_calls):
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function")
        function = function if isinstance(function, dict) else {}
        name = str(function.get("name") or "").strip()
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, str):
            arguments = raw_arguments
        elif isinstance(raw_arguments, dict):
            arguments = json.dumps(raw_arguments, ensure_ascii=False)
        else:
            arguments = "{}"

        call_id = raw_call.get("id") or raw_call.get("call_id") or f"mimo_tool_call_{index}"
        entry: Dict[str, Any] = {
            "id": str(call_id),
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        if isinstance(raw_call.get("extra_content"), dict):
            entry["extra_content"] = raw_call["extra_content"]
        converted.append(entry)
    return converted


def _convert_messages(raw_messages: Any) -> List[Dict[str, Any]]:
    """把 Host 序列化的 message_list 转换为 OpenAI messages。"""

    if not isinstance(raw_messages, list):
        return []

    converted: List[Dict[str, Any]] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            continue
        role = str(raw_message.get("role") or "").strip().lower()
        if not role:
            continue

        tool_calls = _convert_tool_calls(raw_message.get("tool_calls")) if role == "assistant" else []
        content = _build_message_content(raw_message.get("parts"))

        item: Dict[str, Any] = {"role": role}
        if content is not None:
            item["content"] = content
        if tool_calls:
            item["tool_calls"] = tool_calls
        if role == "tool":
            tool_call_id = raw_message.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                item["tool_call_id"] = tool_call_id
            tool_name = raw_message.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                item["name"] = tool_name
        converted.append(item)

    return converted


# Context Item 组内可折叠为单条 assistant 消息的类型集合（与 Host 1.3.15 的投影规则一致）。
_ASSISTANT_GROUP_TYPES = {
    "ReasoningItem",
    "AssistantMessageItem",
    "FunctionCallItem",
    "ProviderActivityItem",
    "ProviderOpaqueItem",
}


def _convert_context_items(raw_items: Any) -> List[Dict[str, Any]]:
    """把 Host 1.3.15+ 序列化的 context_items 转换为 OpenAI messages。

    投影规则对齐 Host 内置 OpenAI 客户端的 ``_convert_messages``：

    - System/User/FunctionCallOutput 各自单独成消息；
    - 相邻的 Reasoning/Assistant/FunctionCall/Provider 类 Item 折叠为一条 assistant 消息；
    - Reasoning 与 Provider 类 Item 没有可移植的 Chat 投影，只透传 assistant 文本与工具调用。
    """

    if not isinstance(raw_items, list):
        return []

    converted: List[Dict[str, Any]] = []
    index = 0
    total = len(raw_items)

    def _item_type(entry: Any) -> str:
        if not isinstance(entry, dict):
            return ""
        return str(entry.get("item_type") or "").strip()

    while index < total:
        current = raw_items[index]
        item_type = _item_type(current)

        if item_type in {"SystemMessageItem", "UserMessageItem"}:
            role = "system" if item_type == "SystemMessageItem" else "user"
            content = _build_message_content(current.get("parts"))
            item: Dict[str, Any] = {"role": role}
            if content is not None:
                item["content"] = content
            converted.append(item)
            index += 1
            continue

        if item_type == "FunctionCallOutputItem":
            call_id = current.get("call_id")
            if isinstance(call_id, str) and call_id:
                item = {"role": "tool", "content": str(current.get("output") or ""), "tool_call_id": call_id}
                tool_name = current.get("tool_name")
                if isinstance(tool_name, str) and tool_name:
                    item["name"] = tool_name
                converted.append(item)
            index += 1
            continue

        if item_type in _ASSISTANT_GROUP_TYPES:
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            while index < total:
                entry = raw_items[index]
                entry_type = _item_type(entry)
                if entry_type not in _ASSISTANT_GROUP_TYPES:
                    break
                if entry_type == "AssistantMessageItem":
                    content = _build_message_content(entry.get("parts"))
                    if isinstance(content, str):
                        text_parts.append(content)
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and str(part.get("type") or "") == "text":
                                text_parts.append(str(part.get("text") or ""))
                elif entry_type == "FunctionCallItem":
                    tool_call = _as_dict(entry.get("tool_call"))
                    call_id = str(tool_call.get("call_id") or "").strip()
                    function_name = str(tool_call.get("func_name") or "").strip()
                    arguments = _as_dict(tool_call.get("args"))
                    if call_id and function_name:
                        tool_calls.append(
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": function_name,
                                    "arguments": json.dumps(arguments, ensure_ascii=False),
                                },
                            }
                        )
                index += 1

            content_text = "".join(text_parts)
            assistant_item: Dict[str, Any] = {"role": "assistant"}
            if content_text:
                assistant_item["content"] = content_text
            if tool_calls:
                assistant_item["tool_calls"] = tool_calls
            if content_text or tool_calls:
                converted.append(assistant_item)
            continue

        index += 1

    return converted


def _convert_tools(raw_tools: Any) -> List[Dict[str, Any]]:
    """过滤并透传 Host 序列化好的 OpenAI 工具定义。"""

    if not isinstance(raw_tools, list):
        return []

    tools: List[Dict[str, Any]] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict):
            continue
        if str(raw_tool.get("type") or "").strip().lower() != "function":
            continue
        function = raw_tool.get("function")
        if not isinstance(function, dict) or not str(function.get("name") or "").strip():
            continue
        tools.append(raw_tool)
    return tools


def _convert_response_format(raw_format: Any) -> Optional[Dict[str, Any]]:
    """把内部响应格式转换为 OpenAI response_format。"""

    if not isinstance(raw_format, dict):
        return None

    format_type = str(raw_format.get("format_type") or "").strip().lower()
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type == "json_schema":
        schema_wrapper = _as_dict(raw_format.get("schema"))
        json_schema: Dict[str, Any] = {
            "name": str(schema_wrapper.get("name") or "response"),
            "schema": _as_dict(schema_wrapper.get("schema")) or {"type": "object", "properties": {}},
        }
        if isinstance(schema_wrapper.get("description"), str) and schema_wrapper["description"].strip():
            json_schema["description"] = schema_wrapper["description"]
        if isinstance(schema_wrapper.get("strict"), bool):
            json_schema["strict"] = schema_wrapper["strict"]
        return {"type": "json_schema", "json_schema": json_schema}
    return None


def _build_request_body(*, model_info: Dict[str, Any], request: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构造 MiMo chat/completions 请求体。"""

    model_name = str(model_info.get("model_identifier") or model_info.get("name") or "").strip()
    body: Dict[str, Any] = {"model": model_name, "messages": messages}

    temperature = request.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        body["temperature"] = float(temperature)

    max_tokens = request.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        body["max_tokens"] = max_tokens

    tools = _convert_tools(request.get("tool_options"))
    if tools:
        body["tools"] = tools

    tool_choice = request.get("tool_choice")
    if tool_choice is not None and tools:
        body["tool_choice"] = tool_choice

    response_format = _convert_response_format(request.get("response_format"))
    if response_format is not None:
        body["response_format"] = response_format

    extra_params: Dict[str, Any] = {}
    extra_params.update(_as_dict(model_info.get("extra_params")))
    extra_params.update(_as_dict(request.get("extra_params")))

    protected_keys = {"model", "messages", "stream", "stream_options"}
    for key, value in extra_params.items():
        normalized_key = str(key)
        if normalized_key in protected_keys or value is None:
            continue
        body[normalized_key] = value

    return body


def _parse_choice(data: Dict[str, Any]) -> Tuple[Optional[str], str, Optional[str], List[Dict[str, Any]]]:
    """从 MiMo 响应中提取 content、推理内容、finish_reason 与工具调用。"""

    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = _as_dict(choice.get("message"))

    content = _normalize_content(message.get("content"))
    reasoning = message.get("reasoning_content")
    if not isinstance(reasoning, str):
        reasoning = ""

    finish_reason = choice.get("finish_reason")
    if not isinstance(finish_reason, str):
        finish_reason = None

    raw_tool_calls = message.get("tool_calls")
    tool_calls = _normalize_response_tool_calls(raw_tool_calls)

    return content, reasoning, finish_reason, tool_calls


def _normalize_response_tool_calls(raw_tool_calls: Any) -> List[Dict[str, Any]]:
    """把 MiMo 返回的 tool_calls 转换为 Host 快照格式（arguments 为字典）。"""

    if not isinstance(raw_tool_calls, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for index, raw_call in enumerate(raw_tool_calls):
        if not isinstance(raw_call, dict):
            continue
        function = _as_dict(raw_call.get("function"))
        name = str(function.get("name") or "").strip()
        raw_arguments = function.get("arguments")
        arguments: Dict[str, Any] = {}
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        elif isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
                arguments = parsed if isinstance(parsed, dict) else {}
            except (ValueError, TypeError):
                arguments = {}

        call_id = raw_call.get("id") or raw_call.get("call_id") or f"mimo_tool_call_{index}"
        entry: Dict[str, Any] = {"id": str(call_id), "function": {"name": name, "arguments": arguments}}
        if isinstance(raw_call.get("extra_content"), dict):
            entry["extra_content"] = raw_call["extra_content"]
        normalized.append(entry)
    return normalized


def _build_usage(data: Dict[str, Any], model_name: str, api_provider: Dict[str, Any]) -> Dict[str, Any]:
    """把 MiMo 的 usage 转换为 Host 的 UsageRecord 字段。"""

    usage = _as_dict(data.get("usage"))

    def _to_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    prompt_tokens = _to_int(usage.get("prompt_tokens"))
    completion_tokens = _to_int(usage.get("completion_tokens"))
    total_tokens = _to_int(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)
    cached_tokens = _to_int(_as_dict(usage.get("prompt_tokens_details")).get("cached_tokens"))

    return {
        "model_name": str(data.get("model") or model_name),
        "provider_name": str(api_provider.get("name") or "mimo"),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "prompt_cache_hit_tokens": cached_tokens,
        "prompt_cache_miss_tokens": max(prompt_tokens - cached_tokens, 0),
    }


def _build_output_items(
    content: Optional[str],
    reasoning: str,
    tool_calls: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """把 MiMo 响应转换为 Host 1.3.15+ 的 Context Item 快照。

    Host 会在落盘前自动为每个 Item 注入 meta（item_id / logical_turn_id / timestamp），
    因此这里只返回 Item 类型与内容字段。
    """

    items: List[Dict[str, Any]] = []

    reasoning_text = str(reasoning or "").strip()
    if reasoning_text:
        items.append(
            {
                "item_type": "ReasoningItem",
                "representation": "raw_text",
                "summary_parts": [],
                "text_parts": [reasoning_text],
            }
        )

    content_text = str(content or "").strip()
    if content_text:
        items.append(
            {
                "item_type": "AssistantMessageItem",
                "parts": [{"type": "text", "text": content_text}],
            }
        )

    for tool_call in tool_calls or []:
        function = _as_dict(tool_call.get("function"))
        call_id = str(tool_call.get("id") or "").strip()
        function_name = str(function.get("name") or "").strip()
        if not call_id or not function_name:
            continue
        tool_call_payload: Dict[str, Any] = {
            "call_id": call_id,
            "func_name": function_name,
            "args": _as_dict(function.get("arguments")),
        }
        if isinstance(tool_call.get("extra_content"), dict):
            tool_call_payload["extra_content"] = tool_call["extra_content"]
        items.append({"item_type": "FunctionCallItem", "tool_call": tool_call_payload})

    return items


# ===== LLM Provider 实现 =====


class MiMoResponseProvider(LLMProviderBase):
    """通过 MiMo OpenAI 兼容接口生成文本 / 工具调用响应。"""

    def __init__(self, plugin: "MiMoProviderPlugin") -> None:
        """保存所属插件实例，用于读取配置与日志器。"""

        self._plugin = plugin

    async def get_response(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """处理 ``response`` 操作。"""

        model_info = _as_dict(request.get("model_info"))
        api_provider = _as_dict(request.get("api_provider"))

        # Host 1.3.15+ 使用 context_items；1.1.x / 1.2.x 使用 message_list。两者都支持。
        if isinstance(request.get("context_items"), list):
            protocol = "context_items"
            messages = _convert_context_items(request.get("context_items"))
        else:
            protocol = "message_list"
            messages = _convert_messages(request.get("message_list"))
        if not messages:
            raise MiMoProviderError("MiMo 请求缺少可用消息")

        model_name = str(model_info.get("model_identifier") or model_info.get("name") or "").strip()
        if not model_name:
            raise MiMoProviderError("MiMo 请求缺少模型名称（model_info.model_identifier）")

        body = _build_request_body(model_info=model_info, request=request, messages=messages)
        data = await self._request_chat_completions(body, api_provider)

        content, reasoning, finish_reason, tool_calls = _parse_choice(data)
        self._plugin._note_response()

        risk_config = self._plugin.config.risk
        risk_reason = detect_risk_hit(
            content=content,
            reasoning=reasoning,
            finish_reason=finish_reason,
            patterns=list(risk_config.patterns or []),
            match_mode=str(risk_config.match_mode or "contains"),
            treat_content_filter=bool(risk_config.treat_content_filter_as_risk),
            scan_reasoning=bool(risk_config.scan_reasoning),
        )

        if risk_reason is not None:
            content, reasoning = self._plugin.apply_risk_strategy(risk_reason, content, reasoning, tool_calls)

        result: Dict[str, Any] = {
            "content": content or "",
            "reasoning_content": reasoning or "",
            "usage": _build_usage(data, model_name, api_provider),
        }
        if self._plugin.config.diag.include_raw_data:
            result["raw_data"] = data
        if tool_calls:
            result["tool_calls"] = tool_calls

        if protocol == "context_items":
            # 1.3.15+ 的原生输出协议；旧版 Host 不会读取这两个字段。
            result["item_schema_version"] = ITEM_SCHEMA_VERSION
            result["output_items"] = _build_output_items(content, reasoning, tool_calls)

        return result

    async def _request_chat_completions(
        self,
        body: Dict[str, Any],
        api_provider: Dict[str, Any],
    ) -> Dict[str, Any]:
        """向 MiMo 发送 chat/completions 请求并返回 JSON。"""

        plugin = self._plugin
        url, headers, params = plugin._build_http_target(api_provider)
        timeout = plugin._resolve_timeout(api_provider)
        client = plugin._get_http_client()

        try:
            response = await client.post(url, headers=headers, params=params, json=body, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise MiMoProviderError(f"MiMo 请求超时（{timeout:.0f} 秒）: {exc}") from exc
        except httpx.HTTPError as exc:
            raise MiMoProviderError(f"MiMo 网络请求失败: {exc}") from exc

        if 300 <= response.status_code < 400:
            location = _truncate(str(response.headers.get("location") or ""), 200)
            raise MiMoProviderError(
                f"MiMo 接口返回重定向 HTTP {response.status_code}（Location: {location or '空'}）。"
                "插件不跟随重定向，避免 api_key、附加 Header 与聊天内容被带到重定向目标；"
                "请把 base_url 直接配置为最终地址。"
            )

        if response.status_code >= 400:
            snippet = _truncate(response.text, 300)
            raise MiMoProviderError(f"MiMo 接口返回 HTTP {response.status_code}: {snippet}")

        try:
            data = response.json()
        except ValueError as exc:
            raise MiMoProviderError("MiMo 返回内容不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise MiMoProviderError("MiMo 返回内容结构异常（不是 JSON 对象）")
        return data


# ===== 插件主体 =====


class MiMoProviderPlugin(MaiBotPlugin):
    """MaiBot-MiMo-rollback 插件。"""

    config_model = MiMoPluginConfig

    def __init__(self) -> None:
        """初始化插件状态。"""

        super().__init__()
        self._provider = MiMoResponseProvider(self)
        self._http_client: Optional[httpx.AsyncClient] = None
        self._total_responses = 0
        self._risk_hits = 0
        self._last_risk_reason = ""
        self._warned_base_urls: set[str] = set()
        self._warned_query_auth = False

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        """插件加载完成。"""

        self.ctx.logger.info(
            f"MaiBot-MiMo-rollback 已加载：client_type=mimo.provider，风险策略={self._risk_strategy()}"
        )

    async def on_unload(self) -> None:
        """插件卸载前释放 HTTP 连接。"""

        client = self._http_client
        self._http_client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - 卸载阶段只记录，不阻断
                self.ctx.logger.warning(f"关闭 MiMo HTTP 客户端失败: {exc}")
        self.ctx.logger.info("MaiBot-MiMo-rollback 已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新。"""

        del scope, config_data, version
        self.ctx.logger.info(f"MaiBot-MiMo-rollback 配置已更新，风险策略={self._risk_strategy()}")

    # ---- 风险处理 ----

    def _risk_strategy(self) -> str:
        """读取并规范化风险处理策略。"""

        raw_strategy = str(getattr(self.config.risk, "strategy", "raise") or "raise").strip().lower()
        if raw_strategy not in {"raise", "replace", "ignore"}:
            logger = self.ctx.logger if self._ctx is not None else None
            if logger is not None:
                logger.warning(f"未知的 risk.strategy={raw_strategy}，已回退为 raise")
            return "raise"
        return raw_strategy

    def _note_response(self) -> None:
        """记录一次成功解析的响应。"""

        self._total_responses += 1

    def apply_risk_strategy(
        self,
        reason: str,
        content: Optional[str],
        reasoning: str,
        tool_calls: List[Dict[str, Any]],
    ) -> Tuple[Optional[str], str]:
        """按配置处理一次风险命中。

        Returns:
            ``(content, reasoning)``：replace / ignore 策略下要使用的内容。

        Raises:
            MiMoRiskContentError: ``strategy=raise`` 时抛出，交由主程序重试 / 切换模型。
        """

        self._risk_hits += 1
        self._last_risk_reason = reason
        strategy = self._risk_strategy()
        if bool(self.config.diag.log_risk_hits):
            self.ctx.logger.warning(f"MiMo 返回风险拦截内容（{reason}），当前处理策略={strategy}")

        if strategy == "ignore":
            return content, reasoning

        if strategy == "replace" and not tool_calls:
            replace_text = str(getattr(self.config.risk, "replace_text", "") or "").strip() or DEFAULT_REPLACE_TEXT
            return replace_text, ""

        message = (
            "MiMo 服务以正常回答的形式返回了风险内容拦截文本"
            f"（{reason}），已按错误处理，交由主程序重试或切换模型。"
        )
        raise MiMoRiskContentError(message)

    # ---- HTTP 细节 ----

    def _resolve_base_url(self, api_provider: Dict[str, Any]) -> str:
        """解析 MiMo API 基础地址。"""

        for candidate in (self.config.request.base_url_override, api_provider.get("base_url")):
            text = str(candidate or "").strip()
            if text:
                return text
        return DEFAULT_BASE_URL

    def _check_base_url(self, base_url: str) -> None:
        """校验 base_url，并对不安全配置输出一次性告警。"""

        warnings = inspect_base_url(base_url)
        if not warnings or base_url in self._warned_base_urls:
            return
        self._warned_base_urls.add(base_url)
        for warning in warnings:
            self.ctx.logger.warning(f"MiMo Provider 安全提醒：{warning}（base_url={base_url}）")

    def _build_http_target(self, api_provider: Dict[str, Any]) -> Tuple[str, Dict[str, str], Dict[str, str]]:
        """构造请求 URL、Header 与查询参数。"""

        base_url = self._resolve_base_url(api_provider)
        self._check_base_url(base_url)
        normalized_base = base_url.rstrip("/")
        if normalized_base.lower().endswith(CHAT_COMPLETIONS_PATH):
            url = normalized_base
        else:
            url = normalized_base + CHAT_COMPLETIONS_PATH

        headers: Dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        for key, value in _as_dict(api_provider.get("default_headers")).items():
            headers[str(key)] = str(value)
        for key, value in (self.config.request.extra_headers or {}).items():
            headers[str(key)] = str(value)

        params: Dict[str, str] = {}
        for key, value in _as_dict(api_provider.get("default_query")).items():
            params[str(key)] = str(value)

        api_key = str(api_provider.get("api_key") or os.environ.get("MIMO_API_KEY", "") or "").strip()
        auth_type = str(api_provider.get("auth_type") or "bearer").strip().lower() or "bearer"

        if api_key:
            if auth_type == "bearer" or auth_type == "":
                headers.setdefault("Authorization", f"Bearer {api_key}")
            elif auth_type == "header":
                header_name = str(api_provider.get("auth_header_name") or "Authorization").strip() or "Authorization"
                prefix = str(api_provider.get("auth_header_prefix") or "").strip()
                headers.setdefault(header_name, f"{prefix} {api_key}".strip() if prefix else api_key)
            elif auth_type == "query":
                query_name = str(api_provider.get("auth_query_name") or "api_key").strip() or "api_key"
                params[query_name] = api_key
                if not self._warned_query_auth:
                    self._warned_query_auth = True
                    self.ctx.logger.warning(
                        "MiMo Provider 安全提醒：auth_type=query 会把 api_key 放进 URL 查询参数，"
                        "可能被代理或访问日志记录；建议改用 bearer 或 header 鉴权。"
                    )

        return url, headers, params

    def _resolve_timeout(self, api_provider: Dict[str, Any]) -> float:
        """解析请求超时秒数。"""

        configured = int(getattr(self.config.request, "timeout_seconds", 0) or 0)
        if configured > 0:
            return float(configured)

        try:
            provider_timeout = float(api_provider.get("timeout") or 0)
        except (TypeError, ValueError):
            provider_timeout = 0.0
        if provider_timeout > 0:
            return provider_timeout
        return DEFAULT_TIMEOUT_SECONDS

    def _get_http_client(self) -> httpx.AsyncClient:
        """获取（惰性创建）共享的 HTTP 客户端。

        明确关闭重定向跟随：跨域重定向时 httpx 会剥掉 Authorization，但附加 Header
        与请求体（含聊天内容）仍会被发到重定向目标，存在泄露风险。
        """

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(follow_redirects=False)
        return self._http_client

    # ---- 状态展示 ----

    def _build_status_text(self) -> str:
        """构造 ``/mimo_status`` 的回复文本。"""

        strategy = self._risk_strategy()
        pattern_count = len([pattern for pattern in (self.config.risk.patterns or []) if str(pattern).strip()])
        lines = [
            "MaiBot-MiMo-rollback 状态",
            "- client_type: mimo.provider",
            f"- 风险处理策略: {strategy}",
            f"- 风险关键词数量: {pattern_count}",
            f"- 已处理响应: {self._total_responses} 次",
            f"- 风险命中: {self._risk_hits} 次",
        ]
        if self._last_risk_reason:
            lines.append(f"- 最近一次命中: {self._last_risk_reason}")
        return "\n".join(lines)

    # ---- 组件 ----

    @Command(
        "mimo_status",
        description="查看 MaiBot-MiMo-rollback 的风险拦截统计",
        pattern=r"^/mimo_status$",
        permission="operator",
    )
    async def mimo_status(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        """输出当前风险拦截统计（仅管理员）。"""

        del kwargs
        text = self._build_status_text()
        await self.ctx.send.text(text, stream_id)
        return True, text, 2

    @LLMProvider(
        "mimo.provider",
        name="Xiaomi MiMo Provider",
        description="小米 MiMo OpenAI 兼容接口；把 high risk 拦截文本识别为错误并触发重试 / 模型回退",
        version="1.0.0",
    )
    async def handle_llm(self, operation: str, request: Dict[str, Any]) -> Dict[str, Any]:
        """处理 Host 转发来的 LLM 请求。"""

        return await self._provider.dispatch(operation, request)


def create_plugin() -> MiMoProviderPlugin:
    """创建插件实例。"""

    return MiMoProviderPlugin()
