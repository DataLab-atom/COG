"""
_pipeline.py — 流水线执行器

将多个 agent / tool 步骤线性串联，步骤间有两种传值方式:

━━ inputs 连线（推荐，类型安全）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    "inputs": {
        "input_text": "preprocess.text",   # step_id.field，值原样传递（不字符串化）
        "count":      "extract.age",        # int / dict / list 均可
        "op":         "$.operator_name"     # $ 前缀引用顶层输入变量
    }

━━ {{}} 模板插值（适合字符串拼接）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    "variables": {
        "prompt": "姓名: {{extract.name}}，城市: {{extract.city}}"
    }

两种方式可混用，inputs 优先级高于 params / variables 中的同名键。

━━ 用法 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 完整执行，返回所有步骤结果
    result = await run_pipeline_from_file("pipelines/my.json", variables={...})
    result.steps["step_id"]   # 某步骤完整输出
    result.final              # 最后一步输出

    # 实时监控最后一步输出（yield (step_id, token)）
    async for step_id, token in run_pipeline_stream_from_file("pipelines/my.json", variables={...}):
        print(f"[{step_id}] {token}", end="", flush=True)

━━ 步骤并发控制 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    mode 省略（默认）→ 同步串行
    mode: "async"    → 提交至事件循环并发执行，下一个 sync 步骤前自动等待收集

━━ 步骤类型 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    "agent"    → 单次 LLM 调用（ref: configs/*.json）
    "tool"     → 确定性工具函数（ref: 工具名 或 tools/*.json）
    "input"    → 异步外部数据接入（ref: 输入源名 或 input/*.json）
    "pipeline" → 嵌套子流水线（ref: pipelines/*.json），取其 final 输出
    "graph"    → 嵌套子图（ref: graphs/*.json），取其 final 输出
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# 项目根目录
_PROJECT_ROOT = Path(__file__).parent.parent


# ── 返回值 ────────────────────────────────────────────────────────────────────

class PipelineResult(BaseModel):
    """流水线执行结果。

    Attributes:
        steps:  每个步骤的输出，键为步骤 id，值为 dict 或 str。
        final:  最后一个步骤的输出（dict 或 str）。

    Example:
        result.steps["extracted"]["name"]   # 某步骤某字段
        result.final                        # 最后步骤结果
        result.model_dump()                 # 全部转 dict
    """

    steps: dict[str, Any]
    final: Any

    model_config = {"frozen": True}


# ── 变量插值 ──────────────────────────────────────────────────────────────────

def _resolve_value(value: Any, context: dict[str, Any]) -> Any:
    """递归替换 value 中所有 {{...}} 占位符。

    支持:
        {{var}}           → context["var"]
        {{step_id.field}} → context["step_id"]["field"]
        嵌套 dict / list 内的字符串同样替换。
    """
    if isinstance(value, dict):
        return {k: _resolve_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, context) for v in value]
    if not isinstance(value, str):
        return value
    return _substitute(value, context)


def _substitute(text: str, context: dict[str, Any]) -> Any:
    """将 text 中的 {{...}} 替换为 context 中对应的值。

    支持三种模式：
    1. 整串是单个简单占位符 {{step_id.field}} → 直接返回对应值（可为非字符串）
    2. 整串是单个表达式 {{expr}} → 将 step_id.field 引用替换后 eval（供 loop_to condition）
    3. 字符串内嵌多个占位符 → 全部转为字符串拼接

    示例：
        "{{step.field}}"                    → 直接返回字段值（模式 1）
        "{{step.action == 'extend'}}"       → 表达式求值，返回 bool（模式 2）
        "{{a}} and {{b}}"                   → "value_a and value_b"（模式 3）
    """
    # 模式 1：整串是单个简单占位符 → 直接返回非字符串值
    single = re.fullmatch(r"\{\{(\w+)(?:\.(\w+))?\}\}", text.strip())
    if single:
        return _lookup(single.group(1), single.group(2), context)

    # 模式 2：整串是单个 {{...}} 但含表达式（比较运算等）→ 表达式求值
    expr_match = re.fullmatch(r"\{\{(.+?)\}\}", text.strip(), re.DOTALL)
    if expr_match:
        return _eval_expr(expr_match.group(1).strip(), context)

    # 模式 3：字符串内嵌多个简单占位符 → 全部转为字符串拼接
    def replacer(m: re.Match) -> str:
        val = _lookup(m.group(1), m.group(2), context)
        return str(val) if val is not None else m.group(0)

    return re.sub(r"\{\{(\w+)(?:\.(\w+))?\}\}", replacer, text)


def _eval_expr(expr: str, context: dict[str, Any]) -> Any:
    """将表达式中 step_id.field 引用替换为实际值后求值。

    仅供 loop_to condition 等框架内部配置使用（非用户输入），eval 风险可控。

    示例：
        "review_results.action == 'extend_evolution'"
        → eval("'confirm' == 'extend_evolution'") → False
        "run_iteration.should_continue"
        → eval("True") → True
    """
    def subst_ref(m: re.Match) -> str:
        # 使用 _resolve_wire 支持多级引用，如 review_results.data.action
        val = _resolve_wire(m.group(0), context)
        if isinstance(val, str):
            return repr(val)
        if val is None:
            return "None"
        return str(val)

    # 匹配 identifier(.identifier)+ 格式的多级引用（至少含一个点），
    # 首字符必须是字母或下划线（排除 Python 数字字面量如 1.5）
    resolved = re.sub(r'\b([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+)\b', subst_ref, expr)
    try:
        return eval(resolved, {"__builtins__": {}})  # noqa: S307
    except Exception:
        return resolved  # 降级：返回字符串（非空字符串视为 truthy）


def _lookup(key: str, field: str | None, context: dict[str, Any]) -> Any:
    val = context.get(key)
    if val is None:
        return None
    if field is None:
        return val
    if isinstance(val, dict):
        return val.get(field)
    return getattr(val, field, None)


# ── inputs 连线解析 ───────────────────────────────────────────────────────────

def _resolve_wire(wire: Any, context: dict[str, Any]) -> Any:
    """解析单条 inputs 连线，返回对应的值（类型原样传递）。

    支持:
        短格式字符串  "step_id.field"   → context["step_id"]["field"]
                      "$.var_name"      → context["var_name"]
        显式 dict     {"from": "step_id", "field": "fname"}
                      {"from": "$",       "field": "var_name"}
        其他值        原样返回（字面量）
    """
    if isinstance(wire, dict):
        source = wire.get("from", "")
        field = wire.get("field")
        if source == "$":
            return context.get(field)
        val = context.get(source)
        if val is None or field is None:
            return val
        return val.get(field) if isinstance(val, dict) else getattr(val, field, None)

    if isinstance(wire, str):
        if wire.startswith("$."):
            return context.get(wire[2:])
        if "." in wire:
            # 支持多级嵌套访问，如 receive.data.user_input
            parts = wire.split(".")
            val = context.get(parts[0])
            for part in parts[1:]:
                if val is None:
                    return None
                val = val.get(part) if isinstance(val, dict) else getattr(val, part, None)
            return val

    return wire  # 字面量原样返回


def _resolve_inputs(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """批量解析 inputs 连线声明，返回 {key: resolved_value} 字典。"""
    return {key: _resolve_wire(wire, context) for key, wire in inputs.items()}


# ── 步骤执行 ──────────────────────────────────────────────────────────────────

async def _run_step(step: dict, context: dict[str, Any]) -> Any:
    """执行单个步骤，返回完整结果（dict / str）。

    对于流式 agent 步骤，会将 AsyncGenerator 完整收集为字符串后返回，
    以便下游步骤可以直接使用该值做插值或连线。

    inputs 连线优先级高于 params / variables 中的同名键。
    """
    step_type = step.get("type")
    ref = step.get("ref", "")
    wired = _resolve_inputs(step.get("inputs", {}), context)

    if step_type == "tool":
        from ._registry import call_tool, call_from_file
        params = {**_resolve_value(step.get("params", {}), context), **wired}
        if Path(ref).suffix == ".json":
            result = call_from_file(ref, params)
        else:
            result = call_tool(ref, params)
        return result.model_dump()

    if step_type == "input":
        # 异步外部数据接入：ref 为输入源名称或 input/*.json 路径
        from ._registry import call_input, call_input_from_file
        params = {**_resolve_value(step.get("params", {}), context), **wired}
        if Path(ref).suffix == ".json":
            result = await call_input_from_file(ref, params)
        else:
            result = await call_input(ref, params)
        return result.model_dump()

    if step_type == "agent":
        from llm_config import run_from_file
        variables = {**_resolve_value(step.get("variables", {}), context), **wired}
        history = context.get("__history__") if step.get("use_history") else None
        ref_path = _PROJECT_ROOT / ref if not Path(ref).is_absolute() else Path(ref)
        result = await run_from_file(ref_path, variables=variables, history=history)
        # 流式结果收集为字符串，供下游步骤使用
        if hasattr(result, "__aiter__"):
            chunks = [chunk async for chunk in result]
            return "".join(chunks)
        return result

    if step_type == "pipeline":
        # 嵌套子流水线：完整执行，返回 final 输出供下游使用
        # params 和 variables 均合并入子流水线的 variables，支持 {{channel_id}} 等配置值透传
        sub_vars = {
            **_resolve_value(step.get("params", {}), context),
            **_resolve_value(step.get("variables", {}), context),
            **wired,
        }
        result = await run_pipeline_from_file(ref, variables=sub_vars)
        return result.final

    if step_type == "graph":
        # 嵌套子图：完整执行，返回 final 输出供下游使用
        # params 和 variables 均合并入子图的 variables，支持 {{channel_id}} 等配置值透传
        from ._graph import run_graph_from_file
        sub_vars = {
            **_resolve_value(step.get("params", {}), context),
            **_resolve_value(step.get("variables", {}), context),
            **wired,
        }
        result = await run_graph_from_file(ref, variables=sub_vars)
        return result.final

    raise ValueError(f"未知步骤类型: {step_type!r}，仅支持 'agent' / 'tool' / 'input' / 'pipeline' / 'graph'")


# ── 流水线入口 ────────────────────────────────────────────────────────────────

async def run_pipeline(
    config: dict, variables: dict[str, Any] | None = None
) -> PipelineResult:
    """执行流水线配置并返回所有步骤的结果。

    Args:
        config:    流水线配置字典（需含 steps 列表）。
        variables: 顶层输入变量，在各步骤的 params / variables 中用 {{var}} 引用。

    Returns:
        PipelineResult:
            .steps  每个步骤输出，键为步骤 id
            .final  最后一个步骤的输出

    Raises:
        ValueError: 步骤类型未知，或缺少必填字段。
        KeyError:   引用了不存在的步骤 id 或字段。

    Example:
        >>> result = await run_pipeline(config, {"input_text": "...", "operator_name": "系统"})
        >>> result.steps["extracted"]["name"]
        '张三'
        >>> result.final
        {'summary': '...', 'keywords': [...]}
    """
    steps = config.get("steps", [])
    if not steps:
        raise ValueError("流水线配置缺少 steps 列表")

    context: dict[str, Any] = dict(variables or {})
    step_outputs: dict[str, Any] = {}
    # 已提交但尚未收集结果的异步任务 {step_id: Task}
    pending: dict[str, asyncio.Task] = {}

    async def _collect_pending() -> None:
        """等待所有挂起的异步任务完成并将结果写入 context。"""
        if not pending:
            return
        results = await asyncio.gather(*pending.values(), return_exceptions=True)
        for step_id, result in zip(pending, results):
            if isinstance(result, BaseException):
                raise result
            step_outputs[step_id] = result
            context[step_id] = result
        pending.clear()

    for step in steps:
        step_id = step.get("id")
        if not step_id:
            raise ValueError(f"步骤缺少 id 字段: {step}")

        mode = step.get("mode", "sync")

        if mode == "async":
            # 提交到事件循环，立即继续下一步（不阻塞）
            ctx_snap = dict(context)
            pending[step_id] = asyncio.create_task(_run_step(step, ctx_snap))
        else:
            # sync：先等待所有挂起的异步任务，再执行本步骤
            await _collect_pending()
            output = await _run_step(step, context)
            step_outputs[step_id] = output
            context[step_id] = output

    # 收集流水线末尾仍在运行的异步任务
    await _collect_pending()

    # final 取最后一个声明步骤的输出（而非 dict 插入顺序最后一个）
    last_id = steps[-1]["id"] if steps else None
    final = step_outputs.get(last_id) if last_id else None
    return PipelineResult(steps=step_outputs, final=final)


async def run_pipeline_stream(
    config: dict, variables: dict[str, Any] | None = None
) -> AsyncGenerator[tuple[str, str], None]:
    """执行流水线，将最后一个步骤的输出作为 (step_id, token) 流实时 yield。

    中间步骤（除最后一步外）完整执行并将结果注入 context；最后一步若为
    流式 agent，则直接透传 AsyncGenerator，不收集为字符串。
    非流式步骤（tool / 非流式 agent）以单条 (step_id, text) 形式 yield。

    Args:
        config:    流水线配置字典。
        variables: 顶层输入变量。

    Yields:
        (step_id, token) 元组：step_id 标识来源步骤，token 为文本片段。

    Example:
        async for step_id, token in run_pipeline_stream(config, variables):
            print(f"[{step_id}] {token}", end="", flush=True)
    """
    steps = config.get("steps", [])
    if not steps:
        raise ValueError("流水线配置缺少 steps 列表")

    # 中间步骤：完整执行，将结果注入 context
    context: dict[str, Any] = dict(variables or {})
    if len(steps) > 1:
        mid_result = await run_pipeline({**config, "steps": steps[:-1]}, variables)
        context.update(mid_result.steps)

    # 最后一步：直接透传流，不收集
    last_step = steps[-1]
    if not last_step.get("id"):
        raise ValueError(f"步骤缺少 id 字段: {last_step}")

    step_id = last_step["id"]
    step_type = last_step.get("type")
    ref = last_step.get("ref", "")
    wired = _resolve_inputs(last_step.get("inputs", {}), context)

    if step_type == "tool":
        from ._registry import call_tool, call_from_file
        params = {**_resolve_value(last_step.get("params", {}), context), **wired}
        result = (
            call_from_file(ref, params) if Path(ref).suffix == ".json"
            else call_tool(ref, params)
        )
        yield (step_id, str(result.model_dump()))
        return

    if step_type == "input":
        # 输入源：异步获取数据，以单条 (step_id, text) 形式 yield
        from ._registry import call_input, call_input_from_file
        params = {**_resolve_value(last_step.get("params", {}), context), **wired}
        result = (
            await call_input_from_file(ref, params) if Path(ref).suffix == ".json"
            else await call_input(ref, params)
        )
        yield (step_id, str(result.model_dump()))
        return

    if step_type == "agent":
        from llm_config import run_from_file
        step_vars = {**_resolve_value(last_step.get("variables", {}), context), **wired}
        history = context.get("__history__") if last_step.get("use_history") else None
        ref_path = _PROJECT_ROOT / ref if not Path(ref).is_absolute() else Path(ref)
        result = await run_from_file(ref_path, variables=step_vars, history=history)
        if hasattr(result, "__aiter__"):
            async for token in result:
                yield (step_id, token)
        else:
            yield (step_id, str(result))
        return

    if step_type == "pipeline":
        # 子流水线：透传其内部流，token 标签替换为当前步骤 id
        sub_vars = {**_resolve_value(last_step.get("variables", {}), context), **wired}
        async for _, token in run_pipeline_stream_from_file(ref, variables=sub_vars):
            yield (step_id, token)
        return

    if step_type == "graph":
        # 子图：透传其所有终端节点流，token 标签替换为当前步骤 id
        from ._graph import run_graph_stream_from_file
        sub_vars = {**_resolve_value(last_step.get("variables", {}), context), **wired}
        async for _, token in run_graph_stream_from_file(ref, variables=sub_vars):
            yield (step_id, token)
        return

    raise ValueError(f"未知步骤类型: {step_type!r}，仅支持 'agent' / 'tool' / 'input' / 'pipeline' / 'graph'")


async def run_pipeline_stream_from_file(
    path: str | Path, variables: dict[str, Any] | None = None
) -> AsyncGenerator[tuple[str, str], None]:
    """从 JSON 文件加载流水线配置并以流式方式执行最后一步。"""
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    config = json.loads(p.read_text(encoding="utf-8"))
    async for item in run_pipeline_stream(config, variables):
        yield item


async def run_pipeline_from_file(
    path: str | Path, variables: dict[str, Any] | None = None
) -> PipelineResult:
    """从 JSON 文件加载流水线配置并执行。

    Args:
        path:      流水线 JSON 文件路径（相对路径以项目根目录为基准）。
        variables: 顶层输入变量。

    Returns:
        PipelineResult

    Example:
        >>> result = await run_pipeline_from_file(
        ...     "pipelines/extract_then_summarize.json",
        ...     variables={"input_text": "张三，28岁，上海", "operator_name": "系统"},
        ... )
        >>> result.final
        {'summary': '...', 'keywords': [...]}
    """
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    config = json.loads(p.read_text(encoding="utf-8"))
    return await run_pipeline(config, variables)
