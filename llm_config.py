"""
llm_config.py
─────────────
从 JSON 配置文件驱动大模型请求的轻量库。

支持:
  - OpenAI 兼容接口 (OpenAI / vLLM 本地部署)
  - 提示词模板变量插值 {{variable}}
  - JSON Schema 验证 (Prompt 提示 + jsonschema 强制校验)
  - Instructor 结构化输出
  - Dynaconf 配置管理（环境变量 / settings.toml / .secrets.toml）

依赖:
  pip install -r requirements.txt
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

import jsonschema
import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, create_model

from utils import _truncate_content

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 1. 配置数据结构
# ──────────────────────────────────────────────

@dataclass
class LLMConfig:
    model: str
    system_prompt_path: str | None = None
    user_prompt_path: str | None = None
    json_output: bool = False
    json_schema: dict | None = None
    use_instructor: bool = False
    stream: bool = False              # 仅 json_output=false 时生效
    system_variables: list[str] = field(default_factory=list)  # system 提示词所需变量
    user_variables: list[str] = field(default_factory=list)    # user 提示词所需变量
    timeout: float | None = None      # 请求超时秒数，None 表示不限
    max_retries: int = 0              # 失败重试次数（不含首次请求）
    description: str | None = None   # 该请求的用途说明，仅作注释，不参与请求逻辑
    agent_name: str | None = None    # 智能体标识，用于历史消息来源标记
    enable_history: bool = False     # 是否启用多轮历史，启用时需在 run() 中传入 history
    base_url: str | None = None       # vLLM: "http://localhost:8000/v1"
    api_key: str | None = None        # None = 从 dynaconf/环境变量读取
    params: dict = field(default_factory=dict)


# ──────────────────────────────────────────────
# 2. Dynaconf 统一配置入口
# ──────────────────────────────────────────────

from config import settings as _settings_obj


def _settings():
    """返回 Dynaconf settings 实例（统一配置入口）。"""
    return _settings_obj


# ──────────────────────────────────────────────
# 3. 配置加载
# ──────────────────────────────────────────────

def _resolve_preset(config: dict) -> dict:
    """如果 config 中声明了 "preset"，从 settings.toml 加载预设并合并。

    合并规则：preset 为底层默认值，config 中显式写的字段覆盖 preset。
    params 字段做浅合并（config.params 覆盖 preset.params 中的同名 key）。
    """
    preset_name = config.get("preset")
    if not preset_name:
        return config

    s = _settings()
    presets = s.get("PRESETS", {})
    preset = presets.get(preset_name) if presets else None
    # Dynaconf 嵌套 table 在非 default 环境下可能无法继承，回退到 default 环境
    if not preset:
        try:
            default_presets = s.from_env("default").get("PRESETS", {})
            preset = default_presets.get(preset_name) if default_presets else None
        except Exception:
            pass
    if not preset:
        logger.warning("预设 '%s' 未在 settings.toml 中定义，忽略", preset_name)
        return config

    # preset 为底，config 覆盖
    merged = {**preset, **{k: v for k, v in config.items() if k != "preset"}}

    # params 浅合并：preset.params + config.params
    preset_params = preset.get("params") or {}
    config_params = config.get("params") or {}
    if preset_params or config_params:
        merged["params"] = {**preset_params, **config_params}

    return merged


def load_config(config: dict) -> LLMConfig:
    config = _resolve_preset(config)

    if "model" not in config:
        raise ValueError("配置缺少必填字段: model")
    if config.get("json_output") and not config.get("json_schema"):
        raise ValueError("json_output=true 时必须提供 json_schema")
    if config.get("stream") and config.get("json_output"):
        raise ValueError("stream=true 仅在 json_output=false 时有效")

    s = _settings()
    return LLMConfig(
        model=config["model"],
        system_prompt_path=config.get("system_prompt_path"),
        user_prompt_path=config.get("user_prompt_path"),
        json_output=config.get("json_output", False),
        json_schema=config.get("json_schema"),
        use_instructor=config.get("use_instructor", False),
        stream=config.get("stream", False),
        system_variables=config.get("system_variables", []),
        user_variables=config.get("user_variables", []),
        timeout=config.get("timeout", s.get("DEFAULT_TIMEOUT")),
        max_retries=config.get("max_retries", s.get("DEFAULT_MAX_RETRIES", 0)),
        description=config.get("description"),
        agent_name=config.get("agent_name"),
        enable_history=config.get("enable_history", False),
        base_url=config.get("base_url") or s.get("OPENAI_API_BASE") or None,
        api_key=config.get("api_key"),   # None → make_client() 从 dynaconf 读取
        params=config.get("params", {}),
    )


def load_config_from_file(path: str | Path) -> LLMConfig:
    with open(path, encoding="utf-8") as f:
        return load_config(json.load(f))


# ──────────────────────────────────────────────
# 4. 提示词加载 & 变量插值
# ──────────────────────────────────────────────

def load_prompt(path: str | None, variables: dict) -> str | None:
    """读取提示词文件，替换 {{var}} 风格占位符。"""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        logger.error(
            "提示词文件不存在  path=%s  检查: 文件路径是否正确、prompts/ 目录下是否有对应文件",
            path,
        )
        raise FileNotFoundError(f"提示词文件不存在: {path}")
    text = p.read_text(encoding="utf-8")
    # 检测未替换的变量
    text_for_subst = re.sub(r"\{\{(\w+)\}\}", r"${\1}", text)
    result = Template(text_for_subst).safe_substitute(variables)
    # 检查是否有未替换的占位符残留
    unresolved = re.findall(r'\$\{(\w+)\}', result)
    if unresolved:
        logger.warning(
            "提示词中有未替换的变量  path=%s  未替换=%s  已提供变量=%s",
            path, unresolved, list(variables.keys()),
        )
    return result


def validate_variables(config: LLMConfig, variables: dict) -> None:
    """校验调用方传入的 variables 是否包含配置中声明的所有必需变量。"""
    missing_system = [v for v in config.system_variables if v not in variables]
    missing_user = [v for v in config.user_variables if v not in variables]
    if missing_system:
        raise ValueError(f"system 提示词缺少变量: {missing_system}")
    if missing_user:
        raise ValueError(f"user 提示词缺少变量: {missing_user}")


def build_messages(system_text: str | None, user_text: str | None, history: list[dict] | None = None) -> list[dict]:
    messages = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    if history:
        messages.extend(history)
    if user_text:
        messages.append({"role": "user", "content": user_text})
    if not messages:
        raise ValueError("system_prompt_path 和 user_prompt_path 至少需要一个")
    return messages


# ──────────────────────────────────────────────
# 4a. 原生 Structured Outputs 支持检测
# ──────────────────────────────────────────────

# 支持 response_format=json_schema 的模型前缀/名称
# 参考：https://platform.openai.com/docs/guides/structured-outputs
_STRUCTURED_OUTPUT_PREFIXES = (
    "gpt-4o-mini",          # gpt-4o-mini-2024-07-18+
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-11-20",
    "gpt-4.1",              # gpt-4.1, gpt-4.1-mini, gpt-4.1-nano
    "o1-2024-12-17",
    "o1-mini",
    "o3",
    "o4",
    "openai/gpt-5",         # gpt-5, gpt-5-mini 等 OpenRouter 前缀
)


def _supports_structured_output(model: str) -> bool:
    """判断该模型是否支持原生 json_schema response_format。"""
    return any(model.startswith(p) for p in _STRUCTURED_OUTPUT_PREFIXES)


def _make_strict_schema(schema: dict) -> dict:
    """
    为 OpenAI Structured Outputs strict 模式递归补全 schema：
    - object: additionalProperties=false，所有 properties 加入 required，去掉 default
    - array: 递归处理 items
    - anyOf/oneOf/allOf: 递归处理各分支
    - 所有类型: 去掉 default（strict 模式不允许）
    """
    schema = {k: v for k, v in schema.items() if k != "default"}
    t = schema.get("type")

    if t == "object":
        props = schema.get("properties", {})
        patched = {
            **schema,
            "additionalProperties": False,
            "required": list(props.keys()),
            "properties": {
                k: _make_strict_schema(v) if isinstance(v, dict) else v
                for k, v in props.items()
            },
        }
        return patched

    if t == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            return {**schema, "items": _make_strict_schema(items)}
        return schema

    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            return {**schema, key: [
                _make_strict_schema(s) if isinstance(s, dict) else s
                for s in schema[key]
            ]}

    return schema


# ──────────────────────────────────────────────
# 4b. JSON Schema → Pydantic 动态模型（供 Instructor）
# ──────────────────────────────────────────────

_JSON_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def schema_to_pydantic(schema: dict) -> type[BaseModel]:
    """把简单的 JSON Schema 转成 Pydantic BaseModel（支持一层 properties）。"""
    props = schema.get("properties", {})
    required = set(schema.get("required", props.keys()))
    fields = {
        name: (_JSON_TYPE_MAP.get(info.get("type", "string"), Any), ... if name in required else None)
        for name, info in props.items()
    }
    return create_model("DynamicSchema", **fields)


# ──────────────────────────────────────────────
# 5. 请求执行（全异步）
# ──────────────────────────────────────────────

def make_client(config: LLMConfig) -> AsyncOpenAI:
    # api_key 优先级: JSON 配置 > dynaconf (.secrets.toml / 环境变量) > None（SDK 自动读取 OPENAI_API_KEY）
    api_key = config.api_key or _settings().get("OPENAI_API_KEY") or None
    key_source = (
        "config.json" if config.api_key else
        "dynaconf" if _settings().get("OPENAI_API_KEY") else
        "env/SDK默认"
    )
    key_preview = f"{api_key[:8]}...{api_key[-4:]}" if api_key and len(api_key) > 12 else "(未设置)"
    logger.debug(
        "    LLM 客户端创建  agent=%s  base_url=%s  api_key来源=%s  api_key=%s  timeout=%s  max_retries=%s",
        config.agent_name or "?", config.base_url or "default",
        key_source, key_preview, config.timeout, config.max_retries,
    )
    if not api_key:
        logger.warning(
            "LLM 客户端缺少 API Key  agent=%s  model=%s  "
            "检查: config.json 的 api_key 字段 / .secrets.toml 的 OPENAI_API_KEY / 环境变量 OPENAI_API_KEY",
            config.agent_name or "?", config.model,
        )
    return AsyncOpenAI(
        api_key=api_key,
        base_url=config.base_url,
        timeout=config.timeout,
        max_retries=config.max_retries,
    )


async def run_text(client: AsyncOpenAI, config: LLMConfig, messages: list[dict]) -> str:
    t0 = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=config.model,
            messages=messages,
            **config.params,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.error(
            "LLM 请求失败  agent=%s  model=%s  base_url=%s  耗时=%.1fs  "
            "error_type=%s  error=%s  messages_count=%d  total_chars=%d  params=%s",
            config.agent_name or "?", config.model, config.base_url or "default",
            elapsed, type(exc).__name__, exc,
            len(messages), sum(len(m.get("content", "")) for m in messages),
            json.dumps(config.params, ensure_ascii=False, default=str) if config.params else "{}",
        )
        raise
    elapsed = time.perf_counter() - t0
    content = resp.choices[0].message.content
    finish_reason = resp.choices[0].finish_reason
    usage = resp.usage
    logger.info(
        "    LLM 完成  agent=%s  model=%s  耗时=%.1fs  tokens=%s/%s  finish_reason=%s",
        config.agent_name or "?", config.model, elapsed,
        usage.prompt_tokens if usage else "?",
        usage.completion_tokens if usage else "?",
        finish_reason,
    )
    logger.debug(
        "    LLM 输出  agent=%s  length=%d  content:\n%s",
        config.agent_name or "?", len(content) if content else 0,
        _truncate_content(content or ""),
    )
    return content


async def run_text_stream(
    client: AsyncOpenAI, config: LLMConfig, messages: list[dict]
) -> AsyncGenerator[str, None]:
    """流式输出，逐块 yield 文本片段。"""
    t0 = time.perf_counter()
    collected: list[str] = []
    async with client.chat.completions.stream(
        model=config.model,
        messages=messages,
        **config.params,
    ) as stream:
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                collected.append(delta)
                yield delta
    elapsed = time.perf_counter() - t0
    full_output = "".join(collected)
    logger.info(
        "    LLM 流式完成  agent=%s  model=%s  耗时=%.1fs  output_chars=%d",
        config.agent_name or "?", config.model, elapsed, len(full_output),
    )
    logger.debug(
        "    LLM 输出(流式)  agent=%s  length=%d  content:\n%s",
        config.agent_name or "?", len(full_output),
        _truncate_content(full_output),
    )


async def run_json_prompt(client: AsyncOpenAI, config: LLMConfig, messages: list[dict]) -> dict:
    """JSON 输出模式。

    - 支持原生 Structured Outputs 的模型（gpt-4o-mini / gpt-4.1 等）：
      直接使用 response_format=json_schema，API 层保证格式正确，无需 prompt 注入。
    - 其他模型（旧版 gpt-4o / vLLM 等）：
      注入 schema 提示 + json_object response_format + jsonschema 事后验证（兜底）。
    """
    kwargs = dict(config.params)

    if _supports_structured_output(config.model):
        # 原生 Structured Outputs：API 保证 schema 合规，不会返回空 content
        # API 要求顶层必须是 type:object；若 schema 顶层为 array，自动包裹后解包
        raw_schema = config.json_schema or {}
        _wrap_array = raw_schema.get("type") == "array"
        if _wrap_array:
            api_schema = {
                "type": "object",
                "required": ["result"],
                "additionalProperties": False,
                "properties": {"result": raw_schema},
            }
        else:
            api_schema = raw_schema
        strict_schema = _make_strict_schema(api_schema)
        schema_name = (config.agent_name or "output").replace(" ", "_")
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": strict_schema,
                "strict": True,
            },
        }
        t0 = time.perf_counter()
        try:
            resp = await client.chat.completions.create(
                model=config.model,
                messages=messages,
                **kwargs,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            logger.error(
                "LLM JSON 请求失败 (structured)  agent=%s  model=%s  base_url=%s  耗时=%.1fs  "
                "error_type=%s  error=%s  schema_name=%s  messages_count=%d",
                config.agent_name or "?", config.model, config.base_url or "default",
                elapsed, type(exc).__name__, exc, schema_name, len(messages),
            )
            raise
        elapsed = time.perf_counter() - t0
        content = resp.choices[0].message.content
        usage = resp.usage
        logger.info(
            "    LLM JSON 完成 (structured)  agent=%s  model=%s  耗时=%.1fs  tokens=%s/%s  finish_reason=%s",
            config.agent_name or "?", config.model, elapsed,
            usage.prompt_tokens if usage else "?",
            usage.completion_tokens if usage else "?",
            resp.choices[0].finish_reason,
        )
        logger.debug(
            "    LLM 输出(structured)  agent=%s  raw_content:\n%s",
            config.agent_name or "?", _truncate_content(content or ""),
        )
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error(
                "LLM JSON 解析失败 (structured)  agent=%s  model=%s  "
                "error=%s  content_preview=%s",
                config.agent_name or "?", config.model, exc,
                _truncate_content(content or "(empty)", 500),
            )
            raise ValueError(
                f"LLM 返回的内容无法解析为 JSON（agent={config.agent_name!r}, model={config.model!r}）: {exc}"
            ) from exc
        return result["result"] if _wrap_array else result

    # 旧式 Prompt 提示模式：兼容不支持原生 Structured Outputs 的模型
    schema_hint = json.dumps(config.json_schema, ensure_ascii=False, indent=2)
    inject = f"\n\n请严格按照以下 JSON Schema 返回 JSON，不要包含任何多余文字:\n{schema_hint}"
    augmented = list(messages)
    for i in range(len(augmented) - 1, -1, -1):
        if augmented[i]["role"] == "system":
            augmented[i] = {**augmented[i], "content": augmented[i]["content"] + inject}
            break
    else:
        augmented.insert(0, {"role": "system", "content": inject.strip()})

    # array 顶层类型不能用 json_object（OpenAI 限制）
    top_type = (config.json_schema or {}).get("type")
    if top_type != "array":
        kwargs["response_format"] = {"type": "json_object"}

    t0 = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=config.model,
            messages=augmented,
            **kwargs,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.error(
            "LLM JSON 请求失败 (prompt)  agent=%s  model=%s  base_url=%s  耗时=%.1fs  "
            "error_type=%s  error=%s  messages_count=%d  has_response_format=%s",
            config.agent_name or "?", config.model, config.base_url or "default",
            elapsed, type(exc).__name__, exc, len(augmented),
            "response_format" in kwargs,
        )
        raise
    elapsed = time.perf_counter() - t0
    choice = resp.choices[0]
    content = choice.message.content
    usage = resp.usage
    logger.info(
        "    LLM JSON 完成 (prompt)  agent=%s  model=%s  耗时=%.1fs  tokens=%s/%s  finish_reason=%s",
        config.agent_name or "?", config.model, elapsed,
        usage.prompt_tokens if usage else "?",
        usage.completion_tokens if usage else "?",
        choice.finish_reason,
    )
    logger.debug(
        "    LLM 输出(prompt)  agent=%s  raw_content:\n%s",
        config.agent_name or "?", _truncate_content(content or ""),
    )
    if not content:
        logger.error(
            "LLM 返回空内容  agent=%s  model=%s  finish_reason=%s  "
            "base_url=%s  messages_count=%d  prompt_tokens=%s",
            config.agent_name or "?", config.model, choice.finish_reason,
            config.base_url or "default", len(augmented),
            usage.prompt_tokens if usage else "?",
        )
        raise ValueError(
            f"LLM 返回了空内容（agent={config.agent_name!r}, finish_reason={choice.finish_reason!r}，"
            f"模型={config.model!r}），可能原因：内容过滤、token 截断或模型异常。"
        )
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.error(
            "LLM JSON 解析失败 (prompt)  agent=%s  model=%s  "
            "error=%s  content_preview=%s",
            config.agent_name or "?", config.model, exc,
            _truncate_content(content, 500),
        )
        raise ValueError(
            f"LLM 返回的内容无法解析为 JSON（agent={config.agent_name!r}, model={config.model!r}）: {exc}"
        ) from exc
    try:
        jsonschema.validate(instance=result, schema=config.json_schema)
    except jsonschema.ValidationError as exc:
        logger.error(
            "LLM JSON Schema 校验失败  agent=%s  model=%s  "
            "schema_path=%s  validator=%s  message=%s  "
            "result_keys=%s",
            config.agent_name or "?", config.model,
            list(exc.absolute_schema_path) if exc.absolute_schema_path else "?",
            exc.validator, exc.message,
            list(result.keys()) if isinstance(result, dict) else type(result).__name__,
        )
        raise
    return result


async def run_json_instructor(client: AsyncOpenAI, config: LLMConfig, messages: list[dict]) -> dict:
    """Instructor 模式：强制结构化输出，自动重试直到格式正确。"""
    inst_client = instructor.from_openai(client)
    DynModel = schema_to_pydantic(config.json_schema)
    t0 = time.perf_counter()
    try:
        result = await inst_client.chat.completions.create(
            model=config.model,
            messages=messages,
            response_model=DynModel,
            **config.params,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.error(
            "LLM JSON 请求失败 (instructor)  agent=%s  model=%s  base_url=%s  耗时=%.1fs  "
            "error_type=%s  error=%s  messages_count=%d",
            config.agent_name or "?", config.model, config.base_url or "default",
            elapsed, type(exc).__name__, exc, len(messages),
        )
        raise
    elapsed = time.perf_counter() - t0
    logger.info(
        "    LLM JSON 完成 (instructor)  agent=%s  model=%s  耗时=%.1fs",
        config.agent_name or "?", config.model, elapsed,
    )
    dumped = result.model_dump()
    logger.debug(
        "    LLM 输出(instructor)  agent=%s  result:\n%s",
        config.agent_name or "?", _truncate_content(json.dumps(dumped, ensure_ascii=False, indent=2)),
    )
    return dumped


# ──────────────────────────────────────────────
# 6. 统一入口
# ──────────────────────────────────────────────

async def run(
    config: LLMConfig,
    variables: dict | None = None,
    history: list[dict] | None = None,
) -> dict | str | AsyncGenerator[str, None]:
    """
    执行请求并返回结果。

    Args:
        config:    LLMConfig 实例
        variables: 提示词模板变量
        history:   多轮历史消息，仅 enable_history=true 时生效

    Returns:
        json_output=False, stream=False → str
        json_output=False, stream=True  → AsyncGenerator[str, None]（逐块 yield 文本）
        json_output=True                → dict（已通过 JSON Schema 验证）

        调用方追加历史示例:
            result = await run(config, variables, history)
            history.append({"role": "assistant", "content": result, "name": config.agent_name})
    """
    variables = variables or {}
    validate_variables(config, variables)
    system_text = load_prompt(config.system_prompt_path, variables)
    user_text = load_prompt(config.user_prompt_path, variables)
    active_history = history if config.enable_history else None
    messages = build_messages(system_text, user_text, active_history)
    client = make_client(config)

    mode = "stream" if config.stream else ("json" if config.json_output else "text")
    msg_count = len(messages)
    total_chars = sum(len(m.get("content", "")) for m in messages)
    logger.info(
        "    LLM 请求  agent=%s  model=%s  mode=%s  messages=%d  total_chars=%d  base_url=%s  params=%s",
        config.agent_name or "?", config.model, mode, msg_count, total_chars,
        config.base_url or "default",
        json.dumps(config.params, ensure_ascii=False, default=str) if config.params else "{}",
    )
    # 记录发送给模型的完整 messages，便于复现问题
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        logger.debug(
            "    LLM 输入  agent=%s  message[%d]  role=%s  content:\n%s",
            config.agent_name or "?", i, role, _truncate_content(content),
        )

    if not config.json_output:
        if config.stream:
            return run_text_stream(client, config, messages)
        return await run_text(client, config, messages)
    if config.use_instructor:
        return await run_json_instructor(client, config, messages)
    return await run_json_prompt(client, config, messages)


async def run_from_file(
    config_path: str | Path,
    variables: dict | None = None,
    history: list[dict] | None = None,
) -> dict | str | AsyncGenerator[str, None]:
    return await run(load_config_from_file(config_path), variables, history)


async def run_from_dict(
    config: dict,
    variables: dict | None = None,
    history: list[dict] | None = None,
) -> dict | str | AsyncGenerator[str, None]:
    return await run(load_config(config), variables, history)
