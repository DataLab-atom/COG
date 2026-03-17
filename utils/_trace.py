"""
_trace.py — 运行级追踪上下文

通过 contextvars 在异步调用链中传播 run_id 和 step_path，
使所有日志自动附带追踪信息，便于过滤单次运行的完整日志。

用法:
    from utils._trace import set_run_id, set_step_path, TraceFilter

    # 在 serve.py 中设置 run_id
    set_run_id("5b0c7d5b")

    # 在步骤执行中更新 step_path
    set_step_path("validation → module_split")

    # 在 logging.basicConfig 之前添加 TraceFilter
    handler.addFilter(TraceFilter())
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from typing import Any

# 当前运行 ID（在 serve.py 中设置，贯穿整个 async 调用链）
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("_run_id", default="")

# 当前步骤路径（如 "arch_step > module_split"），用于追踪嵌套深度
_step_path: contextvars.ContextVar[str] = contextvars.ContextVar("_step_path", default="")


def set_run_id(run_id: str) -> contextvars.Token:
    """设置当前异步上下文的 run_id。"""
    return _run_id.set(run_id)


def get_run_id() -> str:
    """获取当前异步上下文的 run_id。"""
    return _run_id.get()


def set_step_path(path: str) -> contextvars.Token:
    """设置当前步骤路径。"""
    return _step_path.set(path)


def get_step_path() -> str:
    """获取当前步骤路径。"""
    return _step_path.get()


def push_step_path(step_id: str) -> contextvars.Token:
    """将 step_id 追加到当前路径末尾，返回 token 用于恢复。

    用法:
        token = push_step_path("fn_codegen")
        try:
            ...  # 执行步骤，内部日志自动带完整路径
        finally:
            _step_path.reset(token)

    路径格式: "arch_build → fn_codegen → engineer"
    """
    current = _step_path.get()
    new_path = f"{current} → {step_id}" if current else step_id
    return _step_path.set(new_path)


# 最大嵌套深度（防止循环引用导致无限递归）
MAX_NESTING_DEPTH = 20

# 当前嵌套深度
_nesting_depth: contextvars.ContextVar[int] = contextvars.ContextVar("_nesting_depth", default=0)


def check_and_increment_depth(step_id: str) -> contextvars.Token:
    """递增嵌套深度并检查是否超限，返回 token 用于恢复。

    Raises:
        RecursionError: 嵌套深度超过 MAX_NESTING_DEPTH。
    """
    depth = _nesting_depth.get()
    if depth >= MAX_NESTING_DEPTH:
        path = _step_path.get()
        raise RecursionError(
            f"子图嵌套深度超过上限 {MAX_NESTING_DEPTH}，可能存在循环引用。"
            f"当前路径: {path} → {step_id}"
        )
    return _nesting_depth.set(depth + 1)


def get_nesting_depth() -> int:
    """获取当前嵌套深度。"""
    return _nesting_depth.get()


class TraceFilter(logging.Filter):
    """日志过滤器：向每条 LogRecord 注入 run_id 和 step_path 字段。

    配合日志格式使用，例如:
        format="%(asctime)s  %(levelname)-8s  [%(run_id)s]  [%(step_path)s]  %(name)s  %(message)s"
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()  # type: ignore[attr-defined]
        record.step_path = _step_path.get()  # type: ignore[attr-defined]
        return True


class JSONFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器，适用于日志收集系统（ELK / Datadog / CloudWatch）。"""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": getattr(record, "run_id", ""),
            "step_path": getattr(record, "step_path", ""),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


# 人类可读格式（含 step_path）
TEXT_FORMAT = "%(asctime)s  %(levelname)-8s  [%(run_id)s]  [%(step_path)s]  %(name)s  %(message)s"
TEXT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: str | None = None,
    json_output: bool = False,
    max_bytes: int = 50 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """统一日志初始化。

    Args:
        level:        日志级别 (DEBUG/INFO/WARNING/ERROR)。
        log_dir:      日志文件输出目录，None 则仅输出到 console。
        json_output:  是否使用 JSON 格式（生产环境推荐）。
        max_bytes:    单个日志文件最大字节数，默认 50MB。
        backup_count: 日志文件轮转保留份数，默认 5。
    """
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    trace_filter = TraceFilter()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清除已有 handler（避免重复初始化）
    root.handlers.clear()

    def _make_handler(handler: logging.Handler) -> logging.Handler:
        handler.addFilter(trace_filter)
        if json_output:
            handler.setFormatter(JSONFormatter(datefmt=TEXT_DATEFMT))
        else:
            handler.setFormatter(logging.Formatter(TEXT_FORMAT, datefmt=TEXT_DATEFMT))
        return handler

    # Console handler（始终添加）
    root.addHandler(_make_handler(logging.StreamHandler()))

    # File handler（如果指定了 log_dir）
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(log_path / "serve.log"),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        root.addHandler(_make_handler(file_handler))

        # JSON 日志独立文件（非 JSON 模式下也输出一份 JSON，方便机器解析）
        if not json_output:
            json_handler = RotatingFileHandler(
                str(log_path / "serve.json.log"),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            json_handler.addFilter(trace_filter)
            json_handler.setFormatter(JSONFormatter(datefmt=TEXT_DATEFMT))
            root.addHandler(json_handler)

    # 项目模块开启 DEBUG，第三方库保持传入的 level
    for mod in ("utils", "llm_config", "__main__", "serve"):
        logging.getLogger(mod).setLevel(logging.DEBUG)


def _truncate(obj: Any, max_len: int = 200) -> str:
    """将对象转为字符串并截断，用于日志输出。"""
    s = str(obj)
    if len(s) > max_len:
        return s[:max_len] + f"...({len(s)} chars)"
    return s


def _truncate_content(text: str, max_len: int = 2000) -> str:
    """截断长文本内容用于日志输出，保留头尾便于定位问题。"""
    if not text or len(text) <= max_len:
        return text
    half = max_len // 2
    return text[:half] + f"\n... [省略 {len(text) - max_len} 字符] ...\n" + text[-half:]


def _summarize_dict(d: dict, max_keys: int = 8, max_val_len: int = 80) -> str:
    """摘要输出 dict 的键和截断后的值，用于日志。"""
    if not isinstance(d, dict):
        return _truncate(d)
    keys = list(d.keys())
    parts = []
    for k in keys[:max_keys]:
        v = d[k]
        if isinstance(v, str) and len(v) > max_val_len:
            parts.append(f"{k}=({len(v)} chars)")
        elif isinstance(v, (list, dict)):
            parts.append(f"{k}=({type(v).__name__}, len={len(v)})")
        else:
            parts.append(f"{k}={_truncate(v, max_val_len)}")
    if len(keys) > max_keys:
        parts.append(f"...+{len(keys) - max_keys} keys")
    return "{" + ", ".join(parts) + "}"


def safe_serialize(obj: Any, max_str_len: int = 0) -> Any:
    """递归将对象转为可 JSON 序列化的格式。

    Args:
        obj: 任意对象。
        max_str_len: 字符串截断上限，0 表示不截断。
    """
    if isinstance(obj, dict):
        return {k: safe_serialize(v, max_str_len) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [safe_serialize(v, max_str_len) for v in obj]
    if isinstance(obj, str):
        if max_str_len and len(obj) > max_str_len:
            return obj[:max_str_len] + "..."
        return obj
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        s = str(obj)
        if max_str_len and len(s) > max_str_len:
            return s[:max_str_len] + "..."
        return s
