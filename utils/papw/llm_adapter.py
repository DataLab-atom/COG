"""llm_adapter — 同步 LLM 客户端，读 Science-OS dynaconf 配置

替代 Eureka 的 llm.client.LLMClient，提供同步接口供 papw 写作模块使用。
API Key / Base URL 统一从 Science-OS 的 config.settings 读取。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel
from openai import OpenAI

logger = logging.getLogger(__name__)


def _get_settings():
    """懒加载 dynaconf settings。"""
    from config import settings
    return settings


def _get_client() -> OpenAI:
    """创建同步 OpenAI 客户端，从 dynaconf 读取配置。"""
    s = _get_settings()
    api_key = s.get("OPENAI_API_KEY", "") or ""
    base_url = s.get("OPENAI_API_BASE") or None
    if not api_key:
        raise ValueError("OPENAI_API_KEY 未配置（检查 .secrets.toml 或环境变量）")
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


class LLMClient:
    """兼容 Eureka LLMClient 接口的同步 LLM 客户端。"""

    def __init__(
        self,
        default_model: Optional[str] = None,
        default_temperature: Optional[float] = None,
        max_retries: int = 3,
    ) -> None:
        self.default_model = default_model
        self.default_temperature = default_temperature
        self.max_retries = max_retries

    def call(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        response_schema: Optional[Type[BaseModel]] = None,
        json_mode: Optional[bool] = None,
    ) -> Any:
        resolved_model = model or self.default_model
        if not resolved_model:
            raise ValueError("model is required for LLM call")
        resolved_temperature = temperature if temperature is not None else self.default_temperature

        client = _get_client()

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": resolved_model,
                    "messages": messages,
                }
                if resolved_temperature is not None:
                    kwargs["temperature"] = resolved_temperature

                if response_schema is not None:
                    completion = client.beta.chat.completions.parse(
                        response_format=response_schema,
                        **kwargs,
                    )
                    parsed = completion.choices[0].message.parsed
                    result = dict(parsed)
                    logger.debug("LLM schema call ok  model=%s  attempt=%d", resolved_model, attempt)
                    return result

                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                    completion = client.chat.completions.create(**kwargs)
                    content = completion.choices[0].message.content
                    logger.debug("LLM json call ok  model=%s  attempt=%d", resolved_model, attempt)
                    return _parse_json(content)

                completion = client.chat.completions.create(**kwargs)
                content = completion.choices[0].message.content
                logger.debug("LLM text call ok  model=%s  attempt=%d", resolved_model, attempt)
                return content

            except Exception as exc:
                last_error = exc
                logger.warning("LLM call failed  model=%s  attempt=%d: %s", resolved_model, attempt, exc)

        raise RuntimeError(f"LLM call failed after {self.max_retries} retries") from last_error


def _parse_json(content: str) -> Dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        markdown_match = re.search(r"```json\s*([\s\S]*?)\s*```", content, re.IGNORECASE)
        if markdown_match:
            return json.loads(markdown_match.group(1).strip())
        json_match = re.search(r"^\s*\{.*\}\s*$", content, re.DOTALL)
        if json_match:
            return json.loads(content.strip())
        raise


# ── 消息构建辅助函数（替代 Eureka 的 llm.message）──────────────────────────


def system(text: str) -> Dict[str, Any]:
    return {"role": "system", "content": text}


def text_part(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def image_part(base64_data: str, media_type: str = "image/png") -> Dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{base64_data}"},
    }


def user_content(parts: list) -> Dict[str, Any]:
    return {"role": "user", "content": parts}
