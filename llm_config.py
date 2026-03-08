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
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

import jsonschema
import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, create_model


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
# 2. Dynaconf 懒加载（避免循环导入）
# ──────────────────────────────────────────────

def _settings():
    """懒加载 dynaconf settings，首次调用时才导入。"""
    from config import settings  # noqa: PLC0415
    return settings


# ──────────────────────────────────────────────
# 3. 配置加载
# ──────────────────────────────────────────────

def load_config(config: dict) -> LLMConfig:
    if "model" not in config:
        raise ValueError("配置缺少必填字段: model")
    if config.get("json_output") and not config.get("json_schema"):
        raise ValueError("json_output=true 时必须提供 json_schema")
    if config.get("stream") and config.get("json_output"):
        raise ValueError("stream=true 仅在 json_output=false 时有效")

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
        timeout=config.get("timeout"),
        max_retries=config.get("max_retries", 0),
        description=config.get("description"),
        agent_name=config.get("agent_name"),
        enable_history=config.get("enable_history", False),
        base_url=config.get("base_url") or _settings().get("OPENAI_API_BASE") or None,
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
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r"\{\{(\w+)\}\}", r"${\1}", text)
    return Template(text).safe_substitute(variables)


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
# 4. JSON Schema → Pydantic 动态模型（供 Instructor）
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
    return AsyncOpenAI(
        api_key=api_key,
        base_url=config.base_url,
        timeout=config.timeout,
        max_retries=config.max_retries,
    )


async def run_text(client: AsyncOpenAI, config: LLMConfig, messages: list[dict]) -> str:
    resp = await client.chat.completions.create(
        model=config.model,
        messages=messages,
        **config.params,
    )
    return resp.choices[0].message.content


async def run_text_stream(
    client: AsyncOpenAI, config: LLMConfig, messages: list[dict]
) -> AsyncGenerator[str, None]:
    """流式输出，逐块 yield 文本片段。"""
    async with client.chat.completions.stream(
        model=config.model,
        messages=messages,
        **config.params,
    ) as stream:
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


async def run_json_prompt(client: AsyncOpenAI, config: LLMConfig, messages: list[dict]) -> dict:
    """Prompt 提示模式：注入 schema 要求 + jsonschema 验证。"""
    schema_hint = json.dumps(config.json_schema, ensure_ascii=False, indent=2)
    inject = f"\n\n请严格按照以下 JSON Schema 返回 JSON，不要包含任何多余文字:\n{schema_hint}"

    augmented = list(messages)
    for i in range(len(augmented) - 1, -1, -1):
        if augmented[i]["role"] == "system":
            augmented[i] = {**augmented[i], "content": augmented[i]["content"] + inject}
            break
    else:
        augmented.insert(0, {"role": "system", "content": inject.strip()})

    resp = await client.chat.completions.create(
        model=config.model,
        messages=augmented,
        response_format={"type": "json_object"},
        **config.params,
    )
    result = json.loads(resp.choices[0].message.content)
    jsonschema.validate(instance=result, schema=config.json_schema)
    return result


async def run_json_instructor(client: AsyncOpenAI, config: LLMConfig, messages: list[dict]) -> dict:
    """Instructor 模式：强制结构化输出，自动重试直到格式正确。"""
    inst_client = instructor.from_openai(client)
    DynModel = schema_to_pydantic(config.json_schema)
    result = await inst_client.chat.completions.create(
        model=config.model,
        messages=messages,
        response_model=DynModel,
        **config.params,
    )
    return result.model_dump()


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
