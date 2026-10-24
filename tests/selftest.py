"""MaiBot-MiMo-rollback 自测脚本。

不联网、不启动 MaiBot，只验证：

- `_manifest.json` 与 `@LLMProvider` 声明一致；
- 配置模型默认值；
- message / tool / response_format 转换；
- 风险关键词识别；
- 三种风险处理策略的实际行为。

运行方式（用带 pydantic/httpx 的 Python，例如 MaiBot 的 .venv）：

    python tests/selftest.py [--sdk <maibot-plugin-sdk 目录>]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import sys
import types
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。"""

    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name}{': ' + detail if detail else ''}")


def resolve_sdk_dir(override: str = "") -> Path:
    """找到 maibot_sdk 所在目录。"""

    candidates = []
    if override:
        candidates.append(Path(override))
    env_sdk = os.environ.get("MAIBOT_SDK_PATH", "")
    if env_sdk:
        candidates.append(Path(env_sdk))
    candidates.append(REPO_ROOT / "sdk-ref" / "maibot-plugin-sdk-main")
    # 常见布局：仓库与 MaiBot 主程序并列，SDK 直接放在主程序根目录。
    candidates.append(REPO_ROOT.parent / "MaiBot-1.1.4" / "maibot-plugin-sdk-2.7.1")
    env_root = os.environ.get("MAIBOT_ROOT", "")
    if env_root:
        candidates.append(Path(env_root) / "maibot-plugin-sdk-2.7.1")
        candidates.append(Path(env_root))

    for candidate in candidates:
        if (candidate / "maibot_sdk").is_dir():
            return candidate.resolve()
    raise SystemExit(
        "找不到 maibot-plugin-sdk 目录；请用 --sdk 指定，或设置环境变量 MAIBOT_SDK_PATH。"
    )


def load_plugin_module():
    """按文件路径导入 plugin.py。"""

    spec = importlib.util.spec_from_file_location("mimo_plugin_under_test", PLUGIN_DIR / "plugin.py")
    if spec is None or spec.loader is None:
        raise SystemExit("无法加载 plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_stub_ctx():
    """构造不依赖 Runner 的最小上下文。"""

    logger = logging.getLogger("mimo_selftest")
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
    return SimpleNamespace(logger=logger, send=SimpleNamespace())


def test_manifest(module) -> None:
    """校验 manifest 与装饰器声明一致。"""

    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    check("manifest_version == 2", manifest.get("manifest_version") == 2)
    check("id 合法", manifest.get("id") == "myxxr.mimo-rollback")

    providers = manifest.get("llm_providers") or []
    check("manifest 声明了 1 个 llm_provider", len(providers) == 1)
    declared_client_type = providers[0].get("client_type") if providers else ""
    check("client_type == mimo.provider", declared_client_type == "mimo.provider")

    instance = module.create_plugin()
    collected = instance.get_llm_providers()
    check("@LLMProvider 收集到 1 个 provider", len(collected) == 1)
    collected_client_type = collected[0].get("client_type") if collected else ""
    check("装饰器 client_type 与 manifest 一致", collected_client_type == declared_client_type)

    check("插件类声明了 config_model", module.MiMoProviderPlugin.config_model is module.MiMoPluginConfig)


def test_config_defaults(module) -> None:
    """校验配置模型默认值。"""

    config = module.MiMoPluginConfig()
    check("默认 strategy=raise", config.risk.strategy == "raise")
    check("默认 patterns 非空", len(config.risk.patterns) > 0)
    check("默认 match_mode=contains", config.risk.match_mode == "contains")
    check("默认 treat_content_filter_as_risk=True", config.risk.treat_content_filter_as_risk is True)
    check("默认 enabled=True", config.plugin.enabled is True)


def test_risk_detection(module) -> None:
    """校验风险关键词识别。"""

    patterns = list(module.DEFAULT_RISK_PATTERNS)

    hit = module.detect_risk_hit(
        content="The request was rejected because it was considered high risk",
        reasoning="",
        finish_reason="stop",
        patterns=patterns,
        match_mode="contains",
        treat_content_filter=True,
        scan_reasoning=False,
    )
    check("英文拦截文本命中", hit is not None, str(hit))

    hit_case = module.detect_risk_hit(
        content="  the request was rejected because it was considered high risk  ",
        reasoning="",
        finish_reason="stop",
        patterns=patterns,
        match_mode="contains",
        treat_content_filter=True,
        scan_reasoning=False,
    )
    check("大小写 / 空白不敏感", hit_case is not None, str(hit_case))

    normal = module.detect_risk_hit(
        content="今天天气不错，我们一起去公园吧。",
        reasoning="",
        finish_reason="stop",
        patterns=patterns,
        match_mode="contains",
        treat_content_filter=True,
        scan_reasoning=False,
    )
    check("正常文本不误判", normal is None, str(normal))

    content_filter = module.detect_risk_hit(
        content="",
        reasoning="",
        finish_reason="content_filter",
        patterns=patterns,
        match_mode="contains",
        treat_content_filter=True,
        scan_reasoning=False,
    )
    check("content_filter 命中", content_filter is not None, str(content_filter))

    no_content_filter = module.detect_risk_hit(
        content="",
        reasoning="",
        finish_reason="content_filter",
        patterns=patterns,
        match_mode="contains",
        treat_content_filter=False,
        scan_reasoning=False,
    )
    check("关闭 content_filter 识别后不命中", no_content_filter is None, str(no_content_filter))

    reasoning_hit_off = module.detect_risk_hit(
        content="正常回答",
        reasoning="The request was rejected because it was considered high risk",
        finish_reason="stop",
        patterns=patterns,
        match_mode="contains",
        treat_content_filter=True,
        scan_reasoning=False,
    )
    check("默认不检查 reasoning", reasoning_hit_off is None, str(reasoning_hit_off))

    reasoning_hit_on = module.detect_risk_hit(
        content="正常回答",
        reasoning="The request was rejected because it was considered high risk",
        finish_reason="stop",
        patterns=patterns,
        match_mode="contains",
        treat_content_filter=True,
        scan_reasoning=True,
    )
    check("开启后检查 reasoning", reasoning_hit_on is not None, str(reasoning_hit_on))

    equals_miss = module.detect_risk_hit(
        content="前面还有一些话 The request was rejected because it was considered high risk",
        reasoning="",
        finish_reason="stop",
        patterns=["The request was rejected because it was considered high risk"],
        match_mode="equals",
        treat_content_filter=False,
        scan_reasoning=False,
    )
    check("equals 模式下额外文本不命中", equals_miss is None, str(equals_miss))


def test_message_conversion(module) -> None:
    """校验消息转换。"""

    raw_messages = [
        {"role": "system", "parts": [{"type": "text", "text": "你是麦麦"}]},
        {
            "role": "user",
            "parts": [
                {"type": "text", "text": "看看这张图"},
                {"type": "image", "image_base64": "AAAA", "image_format": "png"},
            ],
        },
        {
            "role": "assistant",
            "parts": [{"type": "text", "text": "我来调用工具"}],
            "tool_calls": [{"id": "call_1", "function": {"name": "get_weather", "arguments": {"city": "北京"}}}],
        },
        {
            "role": "tool",
            "parts": [{"type": "text", "text": "晴，25 度"}],
            "tool_call_id": "call_1",
            "tool_name": "get_weather",
        },
    ]

    converted = module._convert_messages(raw_messages)
    check("消息数量一致", len(converted) == 4, str(len(converted)))
    check("system 文本消息", converted[0] == {"role": "system", "content": "你是麦麦"}, str(converted[0]))

    user = converted[1]
    check("用户消息同时含文本与图片", isinstance(user.get("content"), list) and len(user["content"]) == 2, str(user))
    image_part = user["content"][1] if isinstance(user.get("content"), list) else {}
    check(
        "图片转换为 data URI",
        image_part.get("type") == "image_url"
        and image_part.get("image_url", {}).get("url", "").startswith("data:image/png;base64,AAAA"),
        str(image_part),
    )

    assistant = converted[2]
    check("assistant 保留文本", assistant.get("content") == "我来调用工具", str(assistant))
    tool_calls = assistant.get("tool_calls") or []
    check("tool_calls 转换为 OpenAI 格式", len(tool_calls) == 1, str(tool_calls))
    if tool_calls:
        function = tool_calls[0].get("function", {})
        check(
            "tool_calls arguments 序列化为字符串",
            isinstance(function.get("arguments"), str) and "北京" in function.get("arguments", ""),
            str(function),
        )

    tool = converted[3]
    check("tool 消息保留 tool_call_id", tool.get("tool_call_id") == "call_1", str(tool))
    check("tool 消息保留 name", tool.get("name") == "get_weather", str(tool))


def test_context_items_conversion(module) -> None:
    """校验 Host 1.3.15+ 的 context_items 转换。"""

    raw_items = [
        {
            "item_type": "SystemMessageItem",
            "parts": [{"type": "text", "text": "你是麦麦"}],
        },
        {
            "item_type": "UserMessageItem",
            "parts": [
                {"type": "text", "text": "看看这张图"},
                {"type": "image", "image_base64": "AAAA", "image_format": "png"},
            ],
        },
        {
            "item_type": "ReasoningItem",
            "representation": "raw_text",
            "summary_parts": [],
            "text_parts": ["先想想"],
        },
        {
            "item_type": "AssistantMessageItem",
            "parts": [{"type": "text", "text": "我来查一下"}],
        },
        {
            "item_type": "FunctionCallItem",
            "tool_call": {"call_id": "call_1", "func_name": "get_weather", "args": {"city": "北京"}},
        },
        {
            "item_type": "FunctionCallOutputItem",
            "call_id": "call_1",
            "output": "晴，25 度",
            "success": True,
            "tool_name": "get_weather",
        },
    ]

    converted = module._convert_context_items(raw_items)
    check("context_items 转换消息数", len(converted) == 4, str(len(converted)))
    if len(converted) < 4:
        return

    check("context system 消息", converted[0] == {"role": "system", "content": "你是麦麦"}, str(converted[0]))

    user = converted[1]
    check(
        "context 用户消息含图片",
        isinstance(user.get("content"), list)
        and len(user["content"]) == 2
        and user["content"][1].get("type") == "image_url",
        str(user),
    )

    assistant = converted[2]
    check("context assistant 折叠文本", assistant.get("content") == "我来查一下", str(assistant))
    tool_calls = assistant.get("tool_calls") or []
    check("context assistant 折叠工具调用", len(tool_calls) == 1, str(tool_calls))
    if tool_calls:
        check(
            "context 工具参数转换为 JSON 字符串",
            tool_calls[0].get("function", {}).get("arguments") == json.dumps({"city": "北京"}, ensure_ascii=False),
            str(tool_calls[0]),
        )

    tool = converted[3]
    check("context 工具结果消息", tool.get("tool_call_id") == "call_1" and tool.get("content") == "晴，25 度", str(tool))


def test_conversions(module) -> None:
    """校验工具、响应格式与返回解析。"""

    raw_tools = [
        {"type": "function", "function": {"name": "get_weather", "description": "查天气", "parameters": {}}},
        {"type": "not_function", "function": {"name": "ignored"}},
        {"type": "function", "function": {"name": ""}},
    ]
    tools = module._convert_tools(raw_tools)
    check("工具定义过滤非法项", len(tools) == 1, str(tools))

    check("json_object 转换", module._convert_response_format({"format_type": "json_object"}) == {"type": "json_object"})
    json_schema = module._convert_response_format(
        {
            "format_type": "json_schema",
            "schema": {"name": "Answer", "description": "回答", "schema": {"type": "object"}, "strict": True},
        }
    )
    check(
        "json_schema 转换",
        json_schema == {
            "type": "json_schema",
            "json_schema": {
                "name": "Answer",
                "description": "回答",
                "schema": {"type": "object"},
                "strict": True,
            },
        },
        str(json_schema),
    )
    check("text 格式不发送 response_format", module._convert_response_format({"format_type": "text"}) is None)

    data = {
        "model": "mimo-v2.6-flash",
        "choices": [
            {
                "message": {
                    "content": "你好",
                    "reasoning_content": "先想一想",
                    "tool_calls": [
                        {"id": "call_9", "function": {"name": "ping", "arguments": "{\"a\": 1}"}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 4},
        },
    }
    content, reasoning, finish_reason, tool_calls = module._parse_choice(data)
    check("解析 content", content == "你好", str(content))
    check("解析 reasoning_content", reasoning == "先想一想", str(reasoning))
    check("解析 finish_reason", finish_reason == "tool_calls", str(finish_reason))
    check("解析 tool_calls 数量", len(tool_calls) == 1, str(tool_calls))
    if tool_calls:
        check(
            "tool_calls arguments 还原为字典",
            tool_calls[0]["function"]["arguments"] == {"a": 1},
            str(tool_calls[0]),
        )

    usage = module._build_usage(data, "fallback-model", {"name": "mimo"})
    check("usage 总 token", usage["total_tokens"] == 15, str(usage))
    check("usage 缓存命中 token", usage["prompt_cache_hit_tokens"] == 4, str(usage))
    check("usage provider 名称", usage["provider_name"] == "mimo", str(usage))


def make_plugin(module):
    """构造带默认配置与桩上下文的插件实例。"""

    plugin = module.MiMoProviderPlugin()
    plugin.set_plugin_config(plugin.build_default_config())
    plugin._ctx = make_stub_ctx()
    return plugin


def test_risk_strategies(module) -> None:
    """校验三种风险处理策略。"""

    plugin = make_plugin(module)
    raised = False
    try:
        plugin.apply_risk_strategy("content 命中风险关键词", "sentinel", "", [])
    except module.MiMoRiskContentError:
        raised = True
    check("raise 策略抛出 MiMoRiskContentError", raised)
    check("raise 策略计次", plugin._risk_hits == 1, str(plugin._risk_hits))
    check("raise 策略记录最近原因", plugin._last_risk_reason != "", plugin._last_risk_reason)

    plugin = make_plugin(module)
    plugin.config.risk.strategy = "replace"
    content, reasoning = plugin.apply_risk_strategy("content 命中风险关键词", "sentinel", "思考", [])
    check("replace 策略使用替换文案", content == module.DEFAULT_REPLACE_TEXT, str(content))
    check("replace 策略清空推理", reasoning == "", str(reasoning))

    plugin = make_plugin(module)
    plugin.config.risk.strategy = "ignore"
    content, reasoning = plugin.apply_risk_strategy("content 命中风险关键词", "sentinel", "思考", [])
    check("ignore 策略原样返回", (content, reasoning) == ("sentinel", "思考"), str((content, reasoning)))

    plugin = make_plugin(module)
    plugin.config.risk.strategy = "replace"
    raised = False
    try:
        plugin.apply_risk_strategy("content 命中风险关键词", "sentinel", "", [{"id": "x"}])
    except module.MiMoRiskContentError:
        raised = True
    check("replace 策略下带 tool_calls 仍按错误处理", raised)


def _build_provider_request() -> dict:
    """构造一份与 Host 序列化结果同构的 response 请求。"""

    return {
        "model_info": {
            "api_provider": "mimo",
            "extra_params": {},
            "force_stream_mode": False,
            "max_tokens": None,
            "model_identifier": "mimo-v2.6-flash",
            "name": "mimo",
            "temperature": None,
            "visual": False,
        },
        "api_provider": {
            "name": "mimo",
            "base_url": "https://api.xiaomimimo.com/v1",
            "api_key": "sk-test-key",
            "auth_type": "bearer",
            "auth_header_name": "Authorization",
            "auth_header_prefix": "Bearer",
            "auth_query_name": "api_key",
            "default_headers": {},
            "default_query": {},
            "timeout": 30,
        },
        "message_list": [{"role": "user", "parts": [{"type": "text", "text": "你好"}]}],
        "max_tokens": 1024,
        "temperature": 0.7,
        "tool_options": [],
        "response_format": None,
        "extra_params": {},
    }


def _mock_response(sentinel: bool) -> dict:
    """构造 MiMo chat/completions 响应 JSON。"""

    content = (
        "The request was rejected because it was considered high risk"
        if sentinel
        else "你好呀，我是小米 MiMo。"
    )
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "mimo-v2.6-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "reasoning_content": ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
    }


def test_provider_integration(module) -> None:
    """用 MockTransport 跑通 get_response 全链路（不联网）。"""

    async def run_normal_request():
        plugin = make_plugin(module)
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("authorization", "")
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=_mock_response(sentinel=False))

        plugin._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await plugin.handle_llm("response", _build_provider_request())
        finally:
            await plugin._http_client.aclose()
            plugin._http_client = None
        return result, captured

    result, captured = asyncio.run(run_normal_request())
    check("请求 URL 指向 chat/completions", captured.get("url", "").endswith("/v1/chat/completions"), captured.get("url", ""))
    check("Bearer 鉴权头正确", captured.get("authorization") == "Bearer sk-test-key", captured.get("authorization", ""))
    body = captured.get("body") or {}
    check("请求体模型名正确", body.get("model") == "mimo-v2.6-flash", str(body.get("model")))
    check("请求体消息已转换", body.get("messages") == [{"role": "user", "content": "你好"}], str(body.get("messages")))
    check("请求体 max_tokens 透传", body.get("max_tokens") == 1024, str(body.get("max_tokens")))
    check("请求体 temperature 透传", body.get("temperature") == 0.7, str(body.get("temperature")))
    check("正常响应 content", result.get("content") == "你好呀，我是小米 MiMo。", str(result.get("content")))
    check("正常响应 usage", (result.get("usage") or {}).get("total_tokens") == 18, str(result.get("usage")))
    check("正常响应保留 raw_data", isinstance(result.get("raw_data"), dict), str(type(result.get("raw_data"))))
    check("旧协议请求不返回 output_items", "output_items" not in result)

    async def run_context_items_request():
        plugin = make_plugin(module)
        request = _build_provider_request()
        request.pop("message_list", None)
        request["item_schema_version"] = 1
        request["logical_turn_id"] = "turn-test"
        request["context_items"] = [
            {
                "item_type": "UserMessageItem",
                "parts": [{"type": "text", "text": "你好"}],
            }
        ]

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_mock_response(sentinel=False))

        plugin._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await plugin.handle_llm("response", request)
        finally:
            await plugin._http_client.aclose()
            plugin._http_client = None

    context_result = asyncio.run(run_context_items_request())
    check("新协议请求返回 item_schema_version=1", context_result.get("item_schema_version") == 1, str(context_result.get("item_schema_version")))
    output_items = context_result.get("output_items") or []
    check("新协议请求返回 output_items", len(output_items) == 1, str(output_items))
    if output_items:
        check(
            "output_items 首项为 AssistantMessageItem",
            output_items[0].get("item_type") == "AssistantMessageItem",
            str(output_items[0]),
        )

    async def run_sentinel_request(strategy: str):
        plugin = make_plugin(module)
        plugin.config.risk.strategy = strategy

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_mock_response(sentinel=True))

        plugin._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await plugin.handle_llm("response", _build_provider_request())
        finally:
            await plugin._http_client.aclose()
            plugin._http_client = None

    raised = False
    try:
        asyncio.run(run_sentinel_request("raise"))
    except module.MiMoRiskContentError as exc:
        raised = "high risk" in str(exc) or "风险" in str(exc)
    check("raise 策略下拦截文本抛错而非返回", raised)

    result = asyncio.run(run_sentinel_request("replace"))
    check("replace 策略下拦截文本被替换", result.get("content") == module.DEFAULT_REPLACE_TEXT, str(result.get("content")))

    plugin = make_plugin(module)
    plugin.config.risk.strategy = "ignore"

    async def run_ignore():
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_mock_response(sentinel=True))

        plugin._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await plugin.handle_llm("response", _build_provider_request())
        finally:
            await plugin._http_client.aclose()
            plugin._http_client = None

    result = asyncio.run(run_ignore())
    check(
        "ignore 策略下拦截文本原样返回",
        result.get("content") == "The request was rejected because it was considered high risk",
        str(result.get("content")),
    )

    async def run_embedding():
        plugin = make_plugin(module)
        try:
            await plugin.handle_llm("embedding", _build_provider_request())
        except NotImplementedError:
            return True
        return False

    check("embedding 请求明确抛出 NotImplementedError", asyncio.run(run_embedding()))

    plugin = make_plugin(module)
    schema = plugin.build_config_schema(plugin_id="myxxr.mimo-rollback")
    sections = schema.get("sections") or {}
    check("WebUI 配置 Schema 含 risk 分组", "risk" in sections, str(list(sections.keys())))


def load_host_context_modules(maibot_ref: str):
    """按文件路径加载宿主 1.3.15 的 Context Item 纯数据模块（无第三方依赖）。"""

    base = Path(maibot_ref) / "src" / "llm_models" / "payload_content"
    if not (base / "context_item.py").is_file():
        return None

    package_name = "mimo_host_context"
    package = types.ModuleType(package_name)
    package.__path__ = [str(base)]
    sys.modules[package_name] = package

    modules = {}
    for module_name in ("context_item", "context_snapshot", "context_protocol"):
        spec = importlib.util.spec_from_file_location(f"{package_name}.{module_name}", base / f"{module_name}.py")
        if spec is None or spec.loader is None:
            return None
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = loaded
        spec.loader.exec_module(loaded)
        modules[module_name] = loaded
    return modules


def test_output_items_cross_version(module, maibot_ref: str) -> None:
    """用宿主 1.3.15 的反序列化与关系校验验证 output_items 格式。"""

    if not maibot_ref:
        print("[SKIP] 未提供 --maibot-ref，跳过 1.3.15 output_items 交叉校验")
        return

    host = load_host_context_modules(maibot_ref)
    if host is None:
        print(f"[SKIP] {maibot_ref} 下未找到 Context Item 模块，跳过交叉校验")
        return

    context_item = host["context_item"]
    context_snapshot = host["context_snapshot"]
    context_protocol = host["context_protocol"]

    check(
        "item_schema_version 与宿主一致",
        module.ITEM_SCHEMA_VERSION == context_item.CONTEXT_ITEM_SCHEMA_VERSION,
        f"{module.ITEM_SCHEMA_VERSION} vs {context_item.CONTEXT_ITEM_SCHEMA_VERSION}",
    )

    raw_items = module._build_output_items(
        "这是回答",
        "这是推理",
        [{"id": "call_1", "function": {"name": "ping", "arguments": {"a": 1}}}],
    )
    check("output_items 含 3 项", len(raw_items) == 3, str(raw_items))

    turn_id = "turn-cross-version"
    timestamp = datetime.now().isoformat()
    payloads = [
        {**item, "meta": {"item_id": uuid.uuid4().hex, "logical_turn_id": turn_id, "timestamp": timestamp}}
        for item in raw_items
    ]
    try:
        items = [context_snapshot.deserialize_context_item_snapshot(payload) for payload in payloads]
        context_protocol.validate_context_items(items, context_protocol.ContextProtocolMode.MODEL_OUTPUT)
        ok, detail = True, ""
    except ValueError as exc:
        ok, detail = False, str(exc)
    check("宿主 1.3.15 反序列化并校验 output_items", ok, detail)

    if ok:
        check(
            "宿主可提取回答文本",
            context_item.get_response_text(items) == "这是回答",
            context_item.get_response_text(items),
        )
        check(
            "宿主可提取推理文本",
            context_item.get_response_reasoning(items) == "这是推理",
            context_item.get_response_reasoning(items),
        )
        calls = context_item.get_response_tool_calls(items)
        check("宿主可提取工具调用", len(calls) == 1 and calls[0].func_name == "ping", str(calls))


def main() -> int:
    """运行全部自测。"""

    parser = argparse.ArgumentParser(description="MaiBot-MiMo-rollback 自测")
    parser.add_argument("--sdk", default="", help="maibot-plugin-sdk 目录")
    parser.add_argument(
        "--maibot-ref",
        default=str(REPO_ROOT / "maibot-1.3.15-ref" / "MaiBot-1.3.15"),
        help="MaiBot 1.3.15 源码目录，用于交叉校验 output_items（可选）",
    )
    args = parser.parse_args()

    sdk_dir = resolve_sdk_dir(args.sdk)
    sys.path.insert(0, str(sdk_dir))
    print(f"[INFO] 使用 SDK: {sdk_dir}")

    module = load_plugin_module()
    print(f"[INFO] 插件目录: {PLUGIN_DIR}")

    test_manifest(module)
    test_config_defaults(module)
    test_risk_detection(module)
    test_message_conversion(module)
    test_context_items_conversion(module)
    test_conversions(module)
    test_risk_strategies(module)
    test_provider_integration(module)
    test_output_items_cross_version(module, args.maibot_ref)

    print(f"\n结果: {PASSED} 通过, {FAILED} 失败")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
