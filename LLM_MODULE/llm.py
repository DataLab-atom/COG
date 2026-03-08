from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import csv
import datetime as dt
import json
import os
import time
import warnings
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "OpenAILLM",
    "LLM",
    "Cost",
    "extract_token_usage",
    "log_token_usage",
]


_TOKEN_LOG_LOCK = Lock()
_TOKEN_LOG_PATHS: Dict[str, Path] = {}


def _token_logs_root() -> Path:
    return Path(__file__).resolve().parent / "token_logs"


def _ensure_token_log_file(module_name: str) -> Path:
    module_name = (module_name or "").strip() or "unknown_module"
    with _TOKEN_LOG_LOCK:
        existing = _TOKEN_LOG_PATHS.get(module_name)
        if existing is not None:
            return existing

        logs_dir = _token_logs_root() / module_name
        logs_dir.mkdir(parents=True, exist_ok=True)

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = logs_dir / f"{module_name}_tokens_usage_{timestamp}.csv"

        with open(log_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=[
                    "timestamp",
                    "module_name",
                    "agent_name",
                    "model",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cost",
                ],
            )
            writer.writeheader()

        _TOKEN_LOG_PATHS[module_name] = log_path
        return log_path


def extract_token_usage(response: Any) -> Tuple[int, int, int]:
    """
    token 统计逻辑：尽可能从不同 SDK/响应对象中提取 usage（prompt/completion/total）。
    """
    if response is None:
        return 0, 0, 0

    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")

    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0)
    elif usage is not None:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)

    if total_tokens <= 0:
        total_tokens = max(0, prompt_tokens + completion_tokens)

    return prompt_tokens, completion_tokens, total_tokens


def log_token_usage(
    *,
    module_name: str,
    agent_name: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cost: float,
) -> None:
    log_path = _ensure_token_log_file(module_name)
    with _TOKEN_LOG_LOCK:
        with open(log_path, "a", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=[
                    "timestamp",
                    "module_name",
                    "agent_name",
                    "model",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cost",
                ],
            )
            writer.writerow(
                {
                    "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "module_name": module_name,
                    "agent_name": agent_name,
                    "model": model,
                    "prompt_tokens": int(prompt_tokens or 0),
                    "completion_tokens": int(completion_tokens or 0),
                    "total_tokens": int(total_tokens or 0),
                    "cost": float(cost or 0.0),
                }
            )


class Cost:
    def __init__(self) -> None:
        self._accumulated_cost: float = 0.0
        self._costs: List[float] = []

    @property
    def accumulated_cost(self) -> float:
        return self._accumulated_cost

    def add_cost(self, value: float) -> None:
        value = float(value or 0.0)
        if value < 0:
            raise ValueError("Added cost cannot be negative.")
        self._accumulated_cost += value
        self._costs.append(value)


class OpenAILLM:
    """
    轻量封装 OpenAI Chat Completions（兼容新旧 SDK）。
    额外能力：
    - agent_name 属性（按需求新增）
    - 可选 token 日志写入到 LLM_MODULE/token_logs/<module_name>/
    """

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        module_name: str = "unknown_module",
        agent_name: str = "unknown_agent",
    ):
        self.model = model
        self.module_name = module_name
        self.agent_name = agent_name

        self._client = None
        self._is_new: Optional[bool] = None
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("OPENAI_BASE_URL")
        try:
            self.max_retries = int(os.getenv("OPENAI_MAX_RETRIES", "3"))
        except Exception:
            self.max_retries = 2
        try:
            self.retry_backoff = float(os.getenv("OPENAI_RETRY_BACKOFF", "0.5"))
        except Exception:
            self.retry_backoff = 0.5

    def _ensure_client(self) -> None:
        if self._client is not None:
            return

        try:
            from openai import OpenAI  # type: ignore

            kwargs: Dict[str, Any] = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
            self._is_new = True
        except Exception:
            try:
                import openai as _openai  # type: ignore
            except Exception as exc:
                raise RuntimeError("未安装 openai 库，请先执行 pip install openai") from exc

            api_key = self.api_key
            if not api_key:
                raise RuntimeError("缺少 OPENAI_API_KEY 环境变量")
            _openai.api_key = api_key
            if self.base_url:
                _openai.api_base = self.base_url  # type: ignore[attr-defined]
            self._client = _openai
            self._is_new = False

    def _maybe_log_usage(self, response: Any, *, cost: float = 0.0) -> None:
        try:
            p, c, t = extract_token_usage(response)
            if t <= 0:
                return
            log_token_usage(
                module_name=self.module_name,
                agent_name=self.agent_name,
                model=str(self.model or ""),
                prompt_tokens=p,
                completion_tokens=c,
                total_tokens=t,
                cost=float(cost or 0.0),
            )
        except Exception:
            return

    def _sleep_backoff(self, attempt: int, op: str, err: Exception) -> None:
        delay = min(8.0, float(self.retry_backoff) * (2 ** attempt))
        warnings.warn(
            f"LLM call failed (op={op}, attempt={attempt + 1}/{self.max_retries + 1}): {err}. "
            f"Retrying in {delay:.1f}s",
            RuntimeWarning,
        )
        time.sleep(delay)

    def _request_message_with_retry(self, make_call: Callable[[], Any], op: str) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(max(0, self.max_retries) + 1):
            try:
                resp = make_call()
                self._maybe_log_usage(resp, cost=0.0)
                msg = self._extract_message(resp)
                return msg
            except Exception as e:
                last_err = e
                if attempt >= max(0, self.max_retries):
                    break
                self._sleep_backoff(attempt, op, e)
        if last_err is not None:
            raise last_err
        raise RuntimeError(f"LLM call failed (op={op}) with unknown error.")

    def _describe_response(self, response: Any) -> str:
        if response is None:
            return "None"
        if isinstance(response, dict):
            try:
                keys = list(response.keys())
            except Exception:
                keys = []
            return f"dict(keys={keys})"
        return type(response).__name__

    def _extract_choices(self, response: Any) -> List[Any]:
        if response is None:
            raise RuntimeError(
                f"LLM response invalid: response is None. model={self.model!s}, base_url={self.base_url or ''}"
            )

        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, dict):
            choices = response.get("choices")
        if choices is None:
            try:
                choices = response["choices"]  # type: ignore[index]
            except Exception:
                choices = None

        if not choices:
            raise RuntimeError(
                "LLM response invalid: missing choices. "
                f"model={self.model!s}, base_url={self.base_url or ''}, "
                f"resp={self._describe_response(response)}"
            )

        return list(choices)

    def _extract_message(self, response: Any) -> Any:
        choices = self._extract_choices(response)
        first = choices[0]
        msg = getattr(first, "message", None)
        if msg is None and isinstance(first, dict):
            msg = first.get("message")
        if msg is None:
            raise RuntimeError(
                "LLM response invalid: missing message in first choice. "
                f"model={self.model!s}, base_url={self.base_url or ''}, "
                f"resp={self._describe_response(response)}"
            )
        return msg

    @staticmethod
    def _message_content(msg: Any) -> str:
        if msg is None:
            return ""
        if isinstance(msg, dict):
            return msg.get("content") or ""
        return getattr(msg, "content", None) or ""

    @staticmethod
    def _message_tool_calls(msg: Any) -> List[Any]:
        if msg is None:
            return []
        if isinstance(msg, dict):
            return msg.get("tool_calls") or []
        return getattr(msg, "tool_calls", None) or []

    @staticmethod
    def _normalize_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for tc in tool_calls or []:
            if isinstance(tc, dict):
                func = tc.get("function") or {}
                normalized.append(
                    {
                        "id": tc.get("id"),
                        "type": tc.get("type") or "function",
                        "function": {
                            "name": func.get("name"),
                            "arguments": func.get("arguments"),
                        },
                    }
                )
            else:
                func = getattr(tc, "function", None)
                normalized.append(
                    {
                        "id": getattr(tc, "id", None),
                        "type": getattr(tc, "type", None),
                        "function": {
                            "name": getattr(func, "name", None) if func is not None else None,
                            "arguments": getattr(func, "arguments", None) if func is not None else None,
                        },
                    }
                )
        return normalized

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
        self._ensure_client()
        if self._is_new:
            msg = self._request_message_with_retry(
                lambda: self._client.chat.completions.create(  # type: ignore[union-attr]
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                ),
                "chat.completions.create",
            )
            return self._message_content(msg)

        msg = self._request_message_with_retry(
            lambda: self._client.ChatCompletion.create(  # type: ignore[union-attr]
                model=self.model, messages=messages, temperature=temperature
            ),
            "ChatCompletion.create",
        )
        return self._message_content(msg)

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_handler: Callable[[str, Dict[str, Any]], str],
        temperature: float = 0.2,
        max_tool_loops: int = 200,
        need_append_msg: bool = True,

    ) -> str:
        self._ensure_client()

        msgs: List[Dict[str, Any]] = messages if need_append_msg else list(messages)

        if self._is_new:
            for _ in range(max_tool_loops):
                msg = self._request_message_with_retry(
                    lambda: self._client.chat.completions.create(  # type: ignore[union-attr]
                        model=self.model,
                        messages=msgs,
                        tools=tools,
                        temperature=temperature,
                    ),
                    "chat.completions.create",
                )
                tool_calls = self._message_tool_calls(msg)
                if tool_calls:
                    normalized_calls = self._normalize_tool_calls(tool_calls)
                    msgs.append(
                        {
                            "role": "assistant",
                            "content": self._message_content(msg),
                            "tool_calls": normalized_calls,
                        }
                    )
                    for call in normalized_calls:
                        func = call.get("function") or {}
                        name = func.get("name")
                        if not name:
                            raise RuntimeError(
                                "LLM response invalid: tool call missing function name. "
                                f"model={self.model!s}, base_url={self.base_url or ''}"
                            )
                        try:
                            args = json.loads((func.get("arguments") or "{}"))
                        except Exception:
                            args = {"raw": func.get("arguments")}
                        result = tool_handler(name, args)
                        msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id"),
                                "name": name,
                                "content": result,
                            }
                        )
                    continue

                return self._message_content(msg)

            raise RuntimeError("工具调用循环超过上限，可能存在循环依赖。")

        for _ in range(max_tool_loops):
            choice = self._request_message_with_retry(
                lambda: self._client.ChatCompletion.create(  # type: ignore[union-attr]
                    model=self.model,
                    messages=msgs,
                    functions=[t["function"] for t in tools if t.get("type") == "function"],
                    function_call="auto",
                    temperature=temperature,
                ),
                "ChatCompletion.create",
            )
            if isinstance(choice, dict):
                function_call = choice.get("function_call")
            else:
                function_call = getattr(choice, "function_call", None)
            if function_call:
                if not isinstance(function_call, dict):
                    function_call = {
                        "name": getattr(function_call, "name", None),
                        "arguments": getattr(function_call, "arguments", None),
                    }
                msgs.append(
                    {
                        "role": "assistant",
                        "content": self._message_content(choice),
                        "function_call": function_call,
                    }
                )
                name = function_call.get("name")
                if not name:
                    raise RuntimeError(
                        "LLM response invalid: function_call missing name. "
                        f"model={self.model!s}, base_url={self.base_url or ''}"
                    )
                try:
                    args = json.loads(function_call.get("arguments") or "{}")
                except Exception:
                    args = {"raw": function_call.get("arguments")}
                result = tool_handler(name, args)
                msgs.append({"role": "function", "name": name, "content": result})
                continue

            return self._message_content(choice)

        raise RuntimeError("工具调用循环超过上限，可能存在循环依赖。")
