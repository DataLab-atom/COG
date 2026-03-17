"""
_registry.py — 工具函数注册表

通过 JSON 描述文件按名称加载并调用工具函数，与智能体的 run_from_file 用法对应:

    # 智能体
    run_from_file("configs/extract_user.json", variables={...})

    # 工具函数（同步）
    call_from_file("tools/truncate.json", params={...})
    call_tool("truncate", params={...})          # 自动在 tools/ 目录下查找

    # 输入源函数（异步 I/O，需 await）
    await call_input_from_file("input/http_get.json", params={...})
    await call_input("http_get", params={...})   # 自动在 input/ 目录下查找

    # 查看所有已注册工具 / 输入源
    list_tools()
    list_inputs()

    # 生成 OpenAI function calling 格式（供 LLM 工具调用）
    to_openai_schema("truncate")
"""

from __future__ import annotations

import importlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from ._base import ToolResult
from ._const import PROJECT_ROOT as _PROJECT_ROOT
from ._trace import _summarize_dict, _truncate_content

logger = logging.getLogger(__name__)

# tools/ 目录默认路径
_TOOLS_DIR = _PROJECT_ROOT / "tools"
# input/ 目录默认路径
_INPUT_DIR = _PROJECT_ROOT / "input"


def _resolve_absolute(path: str | Path) -> Path:
    """将路径规范化为绝对路径，相对路径以项目根目录为基准。"""
    p = Path(path)
    return p if p.is_absolute() else _PROJECT_ROOT / p


# ── 通用内部辅助 ───────────────────────────────────────────────────────────────

def _load_json_config(path: str | Path, entity_type: str = "配置") -> dict:
    """从 JSON 文件加载配置，并校验 name / module / function 必填字段。

    Args:
        path:        JSON 文件路径。
        entity_type: 实体类型名（用于错误消息，如 "工具" / "输入源"）。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 缺少必填字段。
    """
    data = json.loads(_resolve_absolute(path).read_text(encoding="utf-8"))
    for field in ("name", "module", "function"):
        if field not in data:
            raise ValueError(f"{entity_type}配置缺少必填字段: {field}（文件: {path}）")
    return data


def _resolve_config_path(name_or_path: str | Path, target_dir: Path, entity_type: str) -> Path:
    """将名称或路径解析为实际 JSON 文件（绝对路径）。

    Args:
        name_or_path: 名称（不含 .json）或 JSON 文件路径。
        target_dir:   按名称查找时的目标目录（如 tools/ 或 input/）。
        entity_type:  实体类型名（用于错误消息，如 "工具" / "输入源"）。
    """
    p = Path(name_or_path)
    if p.suffix == ".json":
        return _resolve_absolute(p)
    candidate = target_dir / f"{name_or_path}.json"
    if not candidate.exists():
        raise FileNotFoundError(f"未找到{entity_type} '{name_or_path}'，请确认 {candidate} 存在")
    return candidate


def _build_config_summary(config: dict) -> dict:
    """从配置字典中提取摘要信息（name / description / long_running / parameters）。"""
    return {
        "name": config["name"],
        "description": config.get("description", ""),
        "long_running": config.get("long_running", False),
        "parameters": config.get("parameters", {}),
    }


# ── 配置加载 ──────────────────────────────────────────────────────────────────

def load_tool_config(path: str | Path) -> dict:
    """从 JSON 文件加载工具描述配置。

    Args:
        path: JSON 文件路径。

    Returns:
        工具配置字典，包含 name / module / function / parameters 等字段。

    Raises:
        FileNotFoundError: 文件不存在时抛出。
        ValueError: 缺少必填字段时抛出。
    """
    return _load_json_config(path, entity_type="工具")


def _resolve_path(name_or_path: str | Path) -> Path:
    """将工具名称或路径解析为实际 JSON 文件（绝对路径）。"""
    return _resolve_config_path(name_or_path, _TOOLS_DIR, "工具")


# ── 调用入口 ──────────────────────────────────────────────────────────────────

def call_from_file(path: str | Path, params: dict[str, Any] | None = None) -> ToolResult:
    """从 JSON 文件加载工具配置并调用对应函数。

    Args:
        path:   工具 JSON 文件路径（如 "tools/truncate.json"）。
        params: 传递给工具函数的参数字典。

    Returns:
        ToolResult 子类实例，字段可直接访问。

    Raises:
        FileNotFoundError: JSON 文件不存在。
        ValueError: 配置缺少必填字段，或参数缺少 required 字段。
        TypeError: 参数类型与函数签名不匹配。

    Example:
        >>> r = call_from_file("tools/truncate.json", {"text": "你好世界", "max_chars": 4})
        >>> r.text
        '你好...'
        >>> r.was_truncated
        True
    """
    config = load_tool_config(path)
    return _dispatch(config, params or {})


def call_tool(name: str, params: dict[str, Any] | None = None) -> ToolResult:
    """按工具名称自动查找 JSON 配置并调用对应函数。

    在项目根目录的 tools/ 文件夹下查找 <name>.json。

    Args:
        name:   工具名称（不含 .json 后缀），如 "truncate"。
        params: 传递给工具函数的参数字典。

    Returns:
        ToolResult 子类实例，字段可直接访问。

    Example:
        >>> r = call_tool("render_template", {
        ...     "template": "你好，{{name}}！",
        ...     "variables": {"name": "张三"},
        ... })
        >>> r.text
        '你好，张三！'
    """
    path = _resolve_path(name)
    return call_from_file(path, params)


def call_from_dict(config: dict, params: dict[str, Any] | None = None) -> ToolResult:
    """从配置字典直接调用工具函数（无需 JSON 文件）。

    Args:
        config: 工具配置字典（需含 module / function 字段）。
        params: 传递给工具函数的参数字典。

    Returns:
        ToolResult 子类实例。
    """
    return _dispatch(config, params or {})


def _dispatch(config: dict, params: dict) -> ToolResult:
    """验证参数并动态调用目标函数。"""
    _validate_params(config, params)
    name = config.get("name", config["function"])
    mod_path = config["module"]
    fn_name = config["function"]
    logger.info("    工具调用  name=%s  function=%s.%s  params=%s", name, mod_path, fn_name, _summarize_dict(params))
    logger.debug("    工具调用(完整参数)  name=%s  params:\n%s", name, _truncate_content(json.dumps(params, ensure_ascii=False, default=str)))
    t0 = time.perf_counter()
    try:
        module = importlib.import_module(mod_path)
    except ImportError as exc:
        logger.error(
            "工具模块导入失败  name=%s  module=%s  error=%s  "
            "检查: 模块路径是否正确、依赖是否已安装",
            name, mod_path, exc,
        )
        raise
    fn = getattr(module, fn_name, None)
    if fn is None:
        available = [a for a in dir(module) if not a.startswith("_") and callable(getattr(module, a, None))]
        logger.error(
            "工具函数不存在  name=%s  module=%s  function=%s  "
            "模块中可用函数=%s",
            name, mod_path, fn_name, available[:20],
        )
        raise AttributeError(f"模块 {mod_path!r} 中不存在函数 {fn_name!r}，可用函数: {available[:10]}")
    try:
        result = fn(**params)
    except TypeError as exc:
        logger.error(
            "工具调用参数不匹配  name=%s  function=%s.%s  error=%s  "
            "传入参数=%s",
            name, mod_path, fn_name, exc, list(params.keys()),
        )
        raise
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.error(
            "工具执行失败  name=%s  function=%s.%s  耗时=%.1fs  "
            "error_type=%s  error=%s  params=%s",
            name, mod_path, fn_name, elapsed,
            type(exc).__name__, exc, _summarize_dict(params),
        )
        raise
    elapsed = time.perf_counter() - t0
    output = result.model_dump() if hasattr(result, "model_dump") else result
    logger.info("    工具完成  name=%s  耗时=%.1fs  输出=%s", name, elapsed, _summarize_dict(output) if isinstance(output, dict) else type(output).__name__)
    logger.debug("    工具完成(完整输出)  name=%s  output:\n%s", name, _truncate_content(json.dumps(output, ensure_ascii=False, default=str) if isinstance(output, dict) else str(output)))
    return result


def _validate_params(config: dict, params: dict) -> None:
    schema = config.get("parameters", {})
    required = schema.get("required", [])
    missing = [k for k in required if k not in params]
    if missing:
        raise ValueError(f"工具 '{config['name']}' 缺少必填参数: {missing}")


# ── 工具发现 ──────────────────────────────────────────────────────────────────

def list_tools() -> list[dict]:
    """列出 tools/ 目录下所有已注册工具的摘要信息。

    Returns:
        每个元素包含 name / description / long_running / parameters 字段的列表。

    Example:
        >>> for t in list_tools():
        ...     print(t["name"], "-", t["description"], "(长期)" if t["long_running"] else "")
        truncate - 将文本截断到指定字符数上限...
        render_template - 将模板字符串中的 {{变量名}} 占位符替换为实际值...
    """
    return [_build_config_summary(load_tool_config(f)) for f in sorted(_TOOLS_DIR.glob("*.json"))]


def to_openai_schema(name_or_path: str | Path) -> dict:
    """将工具 JSON 转换为 OpenAI function calling 格式。

    可直接传给 openai.chat.completions.create(tools=[...])。

    Args:
        name_or_path: 工具名称或 JSON 文件路径。

    Returns:
        OpenAI tools 列表单元素格式的字典。

    Example:
        >>> schema = to_openai_schema("truncate")
        >>> schema["type"]
        'function'
        >>> schema["function"]["name"]
        'truncate'
    """
    config = load_tool_config(_resolve_path(name_or_path))
    return {
        "type": "function",
        "function": {
            "name": config["name"],
            "description": config.get("description", ""),
            "parameters": config.get("parameters", {}),
        },
    }


# ── 输入源注册表 ───────────────────────────────────────────────────────────────

def load_input_config(path: str | Path) -> dict:
    """从 JSON 文件加载输入源配置。

    Args:
        path: JSON 文件路径。

    Returns:
        输入源配置字典，包含 name / module / function / parameters 等字段。

    Raises:
        FileNotFoundError: 文件不存在时抛出。
        ValueError: 缺少必填字段时抛出。
    """
    return _load_json_config(path, entity_type="输入源")


def _resolve_input_path(name_or_path: str | Path) -> Path:
    """将输入源名称或路径解析为实际 JSON 文件（绝对路径）。"""
    return _resolve_config_path(name_or_path, _INPUT_DIR, "输入源")


async def call_input_from_file(
    path: str | Path, params: dict[str, Any] | None = None
) -> ToolResult:
    """从 JSON 文件加载输入源配置并调用对应异步函数。

    Args:
        path:   输入源 JSON 文件路径（如 "input/http_get.json"）。
        params: 传递给输入源函数的参数字典。

    Returns:
        ToolResult 子类实例，字段可直接访问。

    Raises:
        FileNotFoundError: JSON 文件不存在。
        ValueError: 配置缺少必填字段，或参数缺少 required 字段。

    Example:
        >>> r = await call_input_from_file("input/http_get.json", {"url": "https://httpbin.org/get"})
        >>> r.status_code
        200
    """
    config = load_input_config(path)
    return await _dispatch_async(config, params or {})


async def call_input(name: str, params: dict[str, Any] | None = None) -> ToolResult:
    """按名称自动查找输入源 JSON 配置并调用对应异步函数。

    在项目根目录的 input/ 文件夹下查找 <name>.json。

    Args:
        name:   输入源名称（不含 .json 后缀），如 "http_get"。
        params: 传递给输入源函数的参数字典。

    Returns:
        ToolResult 子类实例，字段可直接访问。

    Example:
        >>> r = await call_input("http_get", {"url": "https://httpbin.org/get"})
        >>> r.status_code
        200
    """
    path = _resolve_input_path(name)
    return await call_input_from_file(path, params)


async def _dispatch_async(config: dict, params: dict) -> ToolResult:
    """验证参数并动态调用目标异步函数。"""
    _validate_params(config, params)
    name = config.get("name", config["function"])
    mod_path = config["module"]
    fn_name = config["function"]
    logger.info("    输入源调用  name=%s  function=%s.%s  params=%s", name, mod_path, fn_name, _summarize_dict(params))
    logger.debug("    输入源调用(完整参数)  name=%s  params:\n%s", name, _truncate_content(json.dumps(params, ensure_ascii=False, default=str)))
    t0 = time.perf_counter()
    try:
        module = importlib.import_module(mod_path)
    except ImportError as exc:
        logger.error(
            "输入源模块导入失败  name=%s  module=%s  error=%s  "
            "检查: 模块路径是否正确、依赖是否已安装",
            name, mod_path, exc,
        )
        raise
    fn = getattr(module, fn_name, None)
    if fn is None:
        available = [a for a in dir(module) if not a.startswith("_") and callable(getattr(module, a, None))]
        logger.error(
            "输入源函数不存在  name=%s  module=%s  function=%s  "
            "模块中可用函数=%s",
            name, mod_path, fn_name, available[:20],
        )
        raise AttributeError(f"模块 {mod_path!r} 中不存在函数 {fn_name!r}，可用函数: {available[:10]}")
    try:
        result = await fn(**params)
    except TypeError as exc:
        logger.error(
            "输入源调用参数不匹配  name=%s  function=%s.%s  error=%s  "
            "传入参数=%s",
            name, mod_path, fn_name, exc, list(params.keys()),
        )
        raise
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.error(
            "输入源执行失败  name=%s  function=%s.%s  耗时=%.1fs  "
            "error_type=%s  error=%s  params=%s",
            name, mod_path, fn_name, elapsed,
            type(exc).__name__, exc, _summarize_dict(params),
        )
        raise
    elapsed = time.perf_counter() - t0
    output = result.model_dump() if hasattr(result, "model_dump") else result
    logger.info("    输入源完成  name=%s  耗时=%.1fs  输出=%s", name, elapsed, _summarize_dict(output) if isinstance(output, dict) else type(output).__name__)
    logger.debug("    输入源完成(完整输出)  name=%s  output:\n%s", name, _truncate_content(json.dumps(output, ensure_ascii=False, default=str) if isinstance(output, dict) else str(output)))
    return result


def list_inputs() -> list[dict]:
    """列出 input/ 目录下所有已注册输入源的摘要信息。

    Returns:
        每个元素包含 name / description / long_running / parameters 字段的列表。

    Example:
        >>> for inp in list_inputs():
        ...     print(inp["name"], "-", inp["description"], "(长期)" if inp["long_running"] else "")
        http_get - 通过 HTTP GET 请求获取外部数据
        read_file - 读取本地文件内容
    """
    if not _INPUT_DIR.exists():
        return []
    return [_build_config_summary(load_input_config(f)) for f in sorted(_INPUT_DIR.glob("*.json"))]
