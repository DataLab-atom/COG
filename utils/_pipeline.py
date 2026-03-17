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
import logging
import re
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ._const import PROJECT_ROOT as _PROJECT_ROOT
from ._trace import (
    _summarize_dict, _truncate, _truncate_content, safe_serialize,
    push_step_path, get_step_path, _step_path,
    check_and_increment_depth, _nesting_depth,
)

logger = logging.getLogger(__name__)


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
    except Exception as e:
        # eval 失败通常意味着条件表达式配置有误（如引用了不存在的字段）。
        # 记录 warning 便于排查；降级返回原始字符串（非空字符串视为 truthy，do-while 循环将继续执行）。
        logger.warning("条件表达式求值失败，降级为字符串（原始: %r，解析后: %r）: %s", expr, resolved, e)
        return resolved


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
            rest = wire[2:]
            if "." in rest:
                parts = rest.split(".")
                val = context.get(parts[0])
                for part in parts[1:]:
                    if val is None:
                        return None
                    val = val.get(part) if isinstance(val, dict) else getattr(val, part, None)
                return val
            return context.get(rest)
        if "." in wire:
            # 支持多级嵌套访问，如 receive.data.user_input
            parts = wire.split(".")
            val = context.get(parts[0])
            for part in parts[1:]:
                if val is None:
                    return None
                val = val.get(part) if isinstance(val, dict) else getattr(val, part, None)
            return val

    # 纯步骤名引用（无 "." 无 "$." 前缀）：优先在 context 中查找步骤输出，
    # 找不到时才作为字面量返回。
    if isinstance(wire, str) and wire in context:
        return context[wire]
    return wire  # 字面量原样返回


def _resolve_inputs(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """批量解析 inputs 连线声明，返回 {key: resolved_value} 字典。

    当连线引用的上游步骤或字段不存在时，直接抛出异常终止执行，
    避免将 None 传给下游导致级联错误、难以定位根因。

    可选连线：键名以 ``?`` 结尾（如 ``"best_patches?": "u2e.patches"``）
    表示该连线允许解析为 None，不会抛异常。输出字典中的键名不含 ``?`` 后缀。
    """
    resolved = {}
    for key, wire in inputs.items():
        optional = key.endswith("?")
        clean_key = key.rstrip("?") if optional else key

        val = _resolve_wire(wire, context)
        if val is None and isinstance(wire, str) and "." in wire and not optional:
            parts = wire.split(".")
            step_ref = parts[0].lstrip("$.")
            if step_ref not in context:
                raise ValueError(
                    f"inputs 连线解析失败: '{wire}' → 步骤 '{step_ref}' "
                    f"不在上下文中（可用键: {list(context.keys())}）"
                )
            elif isinstance(context.get(step_ref), dict):
                available_fields = list(context[step_ref].keys())
                field_path = ".".join(parts[1:])
                raise ValueError(
                    f"inputs 连线解析失败: '{wire}' → 步骤 '{step_ref}' "
                    f"无字段 '{field_path}'（可用字段: {available_fields[:15]}）"
                )
        resolved[clean_key] = val
    return resolved


# ── 步骤执行 ──────────────────────────────────────────────────────────────────

async def _run_step(step: dict, context: dict[str, Any], event_sink=None) -> Any:
    """执行单个步骤，返回完整结果（dict / str）。

    对于流式 agent 步骤，会将 AsyncGenerator 完整收集为字符串后返回，
    以便下游步骤可以直接使用该值做插值或连线。

    inputs 连线优先级高于 params / variables 中的同名键。

    Args:
        event_sink: 可选异步回调 async def(event: dict) -> None，
                    用于将子图内部事件实时透传给调用方（如 run_graph_events）。
    """
    step_id = step.get("id", "?")

    # 追踪嵌套执行路径，所有日志自动带完整路径
    path_token = push_step_path(step_id)
    try:
        return await _run_step_inner(step, context, event_sink)
    except Exception as exc:
        # 在异常中附加完整执行路径，方便定位嵌套层级
        full_path = get_step_path()
        if not getattr(exc, '_step_path_attached', False):
            exc._step_path_attached = True  # type: ignore[attr-defined]
            exc.add_note(f"执行路径: {full_path}")
        raise
    finally:
        _step_path.reset(path_token)


async def _run_step_inner(step: dict, context: dict[str, Any], event_sink=None) -> Any:
    """_run_step 的内部实现，由 _run_step 包装路径追踪和异常增强。"""
    step_type = step.get("type")
    ref = step.get("ref", "")
    wired = _resolve_inputs(step.get("inputs", {}), context)

    step_id = step.get("id", "?")

    if step_type == "tool":
        from ._registry import call_tool, call_from_file
        params = {**_resolve_value(step.get("params", {}), context), **wired}
        logger.debug("  [%s] 工具入参  ref=%s  params=%s", step_id, ref, _summarize_dict(params))
        logger.debug("  [%s] 工具入参(完整)  ref=%s  params:\n%s", step_id, ref, _truncate_content(json.dumps(params, ensure_ascii=False, default=str)))
        try:
            if Path(ref).suffix == ".json":
                result = call_from_file(ref, params)
            else:
                result = call_tool(ref, params)
        except Exception as exc:
            logger.error(
                "步骤执行失败 [tool]  step=%s  ref=%s  error_type=%s  error=%s  "
                "params_keys=%s  可用上下文键=%s",
                step_id, ref, type(exc).__name__, exc,
                list(params.keys()), list(context.keys()),
            )
            raise
        output = result.model_dump()
        logger.debug("  [%s] 工具输出(完整)  ref=%s  output:\n%s", step_id, ref, _truncate_content(json.dumps(output, ensure_ascii=False, default=str)))
        # 发送工具输出详情事件
        if event_sink:
            await event_sink({
                "type":    "tool_output",
                "step_id": step.get("id", ""),
                "ref":     ref,
                "params":  safe_serialize(params, max_str_len=4000),
                "output":  safe_serialize(output, max_str_len=4000),
            })
        return output

    if step_type == "input":
        # 异步外部数据接入：ref 为输入源名称或 input/*.json 路径
        from ._registry import call_input, call_input_from_file
        params = {**_resolve_value(step.get("params", {}), context), **wired}
        logger.debug("  [%s] 输入源入参  ref=%s  params=%s", step_id, ref, _summarize_dict(params))
        logger.debug("  [%s] 输入源入参(完整)  ref=%s  params:\n%s", step_id, ref, _truncate_content(json.dumps(params, ensure_ascii=False, default=str)))
        try:
            if Path(ref).suffix == ".json":
                result = await call_input_from_file(ref, params)
            else:
                result = await call_input(ref, params)
        except Exception as exc:
            logger.error(
                "步骤执行失败 [input]  step=%s  ref=%s  error_type=%s  error=%s  "
                "params_keys=%s  可用上下文键=%s",
                step_id, ref, type(exc).__name__, exc,
                list(params.keys()), list(context.keys()),
            )
            raise
        output = result.model_dump()
        logger.debug("  [%s] 输入源输出(完整)  ref=%s  output:\n%s", step_id, ref, _truncate_content(json.dumps(output, ensure_ascii=False, default=str)))
        # 发送输入源输出详情事件
        if event_sink:
            await event_sink({
                "type":    "tool_output",
                "step_id": step.get("id", ""),
                "ref":     ref,
                "params":  safe_serialize(params, max_str_len=4000),
                "output":  safe_serialize(output, max_str_len=4000),
            })
        return output

    if step_type == "agent":
        from llm_config import run_from_file
        variables = {**_resolve_value(step.get("variables", {}), context), **wired}
        logger.debug("  [%s] Agent 入参  ref=%s  variables=%s", step_id, ref, _summarize_dict(variables))
        # Agent 配置以 Template.safe_substitute 做字符串插值，dict/list 需先序列化为 JSON 字符串
        variables = {
            k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
            for k, v in variables.items()
        }
        history = context.get("__history__") if step.get("use_history") else None
        ref_path = _PROJECT_ROOT / ref if not Path(ref).is_absolute() else Path(ref)
        try:
            result = await run_from_file(ref_path, variables=variables, history=history)
        except FileNotFoundError:
            logger.error(
                "步骤执行失败 [agent]  step=%s  ref=%s  配置文件不存在  "
                "完整路径=%s  检查: ref 路径是否正确、configs/ 目录下是否有对应文件",
                step_id, ref, ref_path,
            )
            raise
        except Exception as exc:
            logger.error(
                "步骤执行失败 [agent]  step=%s  ref=%s  error_type=%s  error=%s  "
                "variables_keys=%s  可用上下文键=%s",
                step_id, ref, type(exc).__name__, exc,
                list(variables.keys()), list(context.keys()),
            )
            raise
        # 流式结果收集为字符串，供下游步骤使用
        if hasattr(result, "__aiter__"):
            chunks = [chunk async for chunk in result]
            result = "".join(chunks)
        logger.debug("  [%s] Agent 输出(完整)  ref=%s  output:\n%s", step_id, ref,
                      _truncate_content(json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, dict) else str(result)[:2000]))
        # 发送 LLM 输出详情事件
        if event_sink:
            await event_sink({
                "type":    "llm_output",
                "step_id": step.get("id", ""),
                "ref":     ref,
                "output":  safe_serialize(result, max_str_len=4000),
            })
        return result

    if step_type == "pipeline":
        # 嵌套子流水线：完整执行，返回 final 输出供下游使用
        # params 和 variables 均合并入子流水线的 variables，支持 {{channel_id}} 等配置值透传
        depth_token = check_and_increment_depth(step_id)
        try:
            sub_vars = {
                **_resolve_value(step.get("params", {}), context),
                **_resolve_value(step.get("variables", {}), context),
                **wired,
            }
            # 透传 checkpoint_dir 到子流水线
            if "checkpoint_dir" in context:
                sub_vars.setdefault("checkpoint_dir", context["checkpoint_dir"])
            logger.info("  [%s] 子流水线开始  ref=%s  路径=%s", step_id, ref, get_step_path())
            result = await run_pipeline_from_file(ref, variables=sub_vars)
            return result.final
        finally:
            _nesting_depth.reset(depth_token)

    if step_type == "graph":
        # 嵌套子图：完整执行，返回 final 输出供下游使用
        # params 和 variables 均合并入子图的 variables，支持 {{channel_id}} 等配置值透传
        depth_token = check_and_increment_depth(step_id)
        try:
            sub_vars = {
                **_resolve_value(step.get("params", {}), context),
                **_resolve_value(step.get("variables", {}), context),
                **wired,
            }
            # 透传 checkpoint_dir 到子图
            if "checkpoint_dir" in context:
                sub_vars.setdefault("checkpoint_dir", context["checkpoint_dir"])
            logger.info("  [%s] 子图开始  ref=%s  路径=%s", step_id, ref, get_step_path())
            if event_sink:
                # 通过 run_graph_events 执行，将子图内部事件透传给调用方
                from ._graph import run_graph_events
                ref_path = (_PROJECT_ROOT / ref) if not Path(ref).is_absolute() else Path(ref)
                sub_config = json.loads(ref_path.read_text(encoding="utf-8"))
                output = None
                async for sub_event in run_graph_events(sub_config, variables=sub_vars):
                    if sub_event["type"] == "graph_done":
                        output = sub_event["final"]
                    else:
                        # 向事件注入执行路径，方便前端定位嵌套层级
                        sub_event.setdefault("step_path", get_step_path())
                        await event_sink(sub_event)
                if output is None:
                    logger.warning("[%s] 子图 %s 未产出 graph_done 事件，output 为 None  路径=%s", step_id, ref, get_step_path())
                return output if output is not None else {}
            from ._graph import run_graph_from_file
            result = await run_graph_from_file(ref, variables=sub_vars)
            return result.final
        finally:
            _nesting_depth.reset(depth_token)

    if step_type == "map":
        # 并行映射：将 step["step"] 应用于 over 指定列表的每一项，asyncio.gather 并发。
        #
        # 基础字段：
        #   over    — 连线表达式，解析为 list（同 inputs 连线语法）
        #   as      — 每项绑定的变量名，在子步骤 inputs / variables 中以 "$.as变量名" 引用
        #   step    — 子步骤定义（同普通步骤，支持 agent / tool / input / graph / pipeline）
        #   collect — 结果合并策略（可选）：
        #               不填        → list[Any]，每项的原始输出
        #               "flat"      → 每项输出是 list 时展平一层，适合 agent 直接返回数组的场景
        #               "字段名"    → 从每项 dict 中取该字段，合并为 flat list
        #               {"k": "f"} → 从每项 dict 中取多个字段，各自合并，返回 dict
        #               "keyed"    → 返回 {item_key: output} dict（需配合 item_key 使用）
        #
        # 依赖感知字段（可选，配合使用）：
        #   item_key         — 连线表达式，在每个 item 的上下文中求值，返回该 item 的唯一 key
        #   item_depends_on  — 连线表达式，在每个 item 的上下文中求值，返回其依赖的 key 列表
        #   deps_as          — 依赖输出在子步骤 context 中的变量名，默认 "_deps"
        #                      子步骤通过 "$._deps" 访问 {dep_key: dep_output} 字典
        over_raw = step.get("over")
        items: list = _resolve_wire(over_raw, context) if over_raw is not None else []
        if not isinstance(items, list):
            items = [items] if items is not None else []

        as_var         = step.get("as", "_item")
        inner          = step.get("step")
        collect        = step.get("collect")
        item_key_path  = step.get("item_key")
        item_deps_path = step.get("item_depends_on")
        deps_as        = step.get("deps_as", "_deps")

        if not inner:
            return []

        logger.info("  [%s] map 开始  items=%d  as=%s  collect=%s  inner_type=%s  inner_ref=%s",
                     step_id, len(items), as_var, collect, inner.get("type", "?"), inner.get("ref", ""))

        if item_key_path and item_deps_path:
            # ── 依赖感知并发：构建 item 内部 DAG，等待依赖完成后再执行 ──────────────

            def _get_item_key(item: Any) -> str:
                val = _resolve_wire(item_key_path, {as_var: item})
                return str(val) if val is not None else str(id(item))

            def _get_item_deps(item: Any) -> list[str]:
                val = _resolve_wire(item_deps_path, {as_var: item})
                return [str(d) for d in val] if isinstance(val, list) else []

            keyed_items   = {_get_item_key(item): item for item in items}
            item_deps_map = {
                key: [d for d in _get_item_deps(item) if d in keyed_items]
                for key, item in keyed_items.items()
            }

            results_by_key: dict[str, Any] = {}
            events_by_key: dict[str, asyncio.Event] = {k: asyncio.Event() for k in keyed_items}

            async def _run_keyed(key: str, item: Any) -> None:
                # 等待所有声明的依赖完成
                for dep_key in item_deps_map.get(key, []):
                    ev = events_by_key.get(dep_key)
                    if ev:
                        await ev.wait()
                # 收集依赖输出，注入为 deps_as 变量
                dep_outputs = {
                    dk: results_by_key.get(dk, {})
                    for dk in item_deps_map.get(key, [])
                }
                try:
                    result = await _run_step(inner, {**context, as_var: item, deps_as: dep_outputs}, event_sink)
                    results_by_key[key] = result
                except Exception as exc:
                    logger.warning("  [%s] map item 失败  key=%s  error=%s", step_id, key, exc)
                    results_by_key[key] = {}
                finally:
                    events_by_key[key].set()  # 无论成败都解锁下游

            await asyncio.gather(*[_run_keyed(k, v) for k, v in keyed_items.items()])
            logger.info("  [%s] map 完成（依赖感知）  items=%d  成功=%d", step_id, len(keyed_items), sum(1 for v in results_by_key.values() if v != {}))

            # collect: "keyed" → 直接返回 {key: output} dict
            if collect == "keyed":
                return results_by_key

            # 其他 collect 策略：按原始 items 顺序重建 results list
            results: list = [results_by_key.get(_get_item_key(item), {}) for item in items]

        else:
            # ── 简单并发（原有逻辑，无 item 间依赖）────────────────────────────────

            async def _run_one(item: Any) -> Any:
                return await _run_step(inner, {**context, as_var: item}, event_sink)

            raw = await asyncio.gather(*[_run_one(item) for item in items], return_exceptions=True)
            for r in raw:
                if isinstance(r, BaseException):
                    raise r
            results = list(raw)
            logger.info("  [%s] map 完成（简单并发）  items=%d", step_id, len(results))

            # 简单并发 + item_key + collect="keyed"：需手动构建 keyed dict
            if collect == "keyed" and item_key_path:
                results_by_key: dict[str, Any] = {}
                for item, result in zip(items, results):
                    val = _resolve_wire(item_key_path, {as_var: item})
                    key = str(val) if val is not None else str(id(item))
                    results_by_key[key] = result
                return results_by_key

        # ── collect 合并策略 ──────────────────────────────────────────────────────
        if collect is None:
            return results

        if collect == "flat":
            merged: list = []
            for r in results:
                if r is None:
                    continue
                if isinstance(r, list):
                    merged.extend(r)
                else:
                    merged.append(r)
            return merged

        if isinstance(collect, str):
            merged = []
            for r in results:
                if isinstance(r, dict):
                    val = r.get(collect, [])
                    merged.extend(val) if isinstance(val, list) else merged.append(val)
            return merged

        if isinstance(collect, dict):
            out: dict[str, list] = {k: [] for k in collect}
            for r in results:
                if isinstance(r, dict):
                    for out_key, src_field in collect.items():
                        val = r.get(src_field, [])
                        out[out_key].extend(val) if isinstance(val, list) else out[out_key].append(val)
            return out

        return results

    if step_type == "loop":
        # 有状态 while 循环：将 body 子步骤列表反复执行，每轮以当前状态作为 variables 输入。
        depth_token = check_and_increment_depth(step_id)
        try:
            return await _run_loop(step, step_id, ref, context, wired, event_sink)
        finally:
            _nesting_depth.reset(depth_token)

    if step_type == "cond":
        return await _run_cond(step, step_id, context, event_sink)

    raise ValueError(
        f"未知步骤类型: {step_type!r}（步骤 {step_id!r}，路径: {get_step_path()}），"
        f"仅支持 'agent' / 'tool' / 'input' / 'pipeline' / 'graph' / 'map' / 'loop' / 'cond'"
    )


async def _run_loop(step: dict, step_id: str, ref: str, context: dict[str, Any], wired: dict, event_sink=None) -> Any:
    """loop 步骤内部实现，从 _run_step_inner 分离以简化控制流。"""
    max_iter = step.get("max_iterations")
    if max_iter is None:
        raise ValueError(f"loop 步骤 '{step_id}' 缺少 max_iterations 字段")

    body_steps = step.get("body", [])
    if not body_steps:
        return {}

    # 解析初始状态
    state: dict[str, Any] = {
        k: _resolve_wire(v, context)
        for k, v in step.get("state", {}).items()
    }
    state_update_map: dict[str, str] = step.get("state_update", {})
    while_expr: str | None = step.get("while")
    body_config = {"steps": body_steps}
    last_result: Any = state.copy()

    logger.info("  [%s] loop 开始  max_iterations=%d  state_keys=%s  while=%s  路径=%s", step_id, max_iter, list(state.keys()), while_expr, get_step_path())
    _iter_count = 0

    if event_sink:
        # 有事件回调时，通过 run_graph_events 执行 body，透传内部事件
        from ._graph import run_graph_events
        for _iter_num in range(max_iter):
            # 循环体不能使用 checkpoint：步骤 ID 不变但每轮输入不同，
            # 若不剔除会命中迭代 1 的缓存导致后续迭代空转
            sub_vars = {k: v for k, v in {**context, **state}.items() if k != "checkpoint_dir"}
            body_final: Any = None
            body_steps_out: dict[str, Any] = {}
            async for sub_event in run_graph_events(body_config, variables=sub_vars):
                if sub_event["type"] == "graph_done":
                    body_final = sub_event["final"]
                    body_steps_out = sub_event.get("steps", {})
                else:
                    sub_event.setdefault("step_path", get_step_path())
                    await event_sink(sub_event)
            last_result = body_final
            if state_update_map:
                body_ctx = {**sub_vars, **body_steps_out}
                state = {k: _resolve_wire(v, body_ctx) for k, v in state_update_map.items()}
            elif isinstance(body_final, dict):
                state = {**state, **body_final}
            _iter_count += 1
            logger.info("  [%s] loop 迭代 #%d 完成  state=%s", step_id, _iter_count, _summarize_dict(state))
            if while_expr:
                check_ctx = {**context, **body_steps_out, **state}
                cond_val = _substitute(while_expr, check_ctx)
                logger.info(
                    "  [%s] loop while 检查  expr=%s  result=%s  → %s  iteration=%d/%d",
                    step_id, while_expr, cond_val,
                    "继续" if cond_val else "退出",
                    _iter_count, max_iter,
                )
                if not cond_val:
                    break
    else:
        from ._graph import run_graph
        for _iter_num in range(max_iter):
            sub_vars = {k: v for k, v in {**context, **state}.items() if k != "checkpoint_dir"}
            body_result = await run_graph(body_config, variables=sub_vars)
            last_result = body_result.final
            if state_update_map:
                body_ctx = {**sub_vars, **body_result.steps}
                state = {k: _resolve_wire(v, body_ctx) for k, v in state_update_map.items()}
            elif isinstance(body_result.final, dict):
                state = {**state, **body_result.final}
            _iter_count += 1
            logger.info("  [%s] loop 迭代 #%d 完成  state=%s", step_id, _iter_count, _summarize_dict(state))
            if while_expr:
                check_ctx = {**context, **body_result.steps, **state}
                cond_val = _substitute(while_expr, check_ctx)
                logger.info(
                    "  [%s] loop while 检查  expr=%s  result=%s  → %s  iteration=%d/%d",
                    step_id, while_expr, cond_val,
                    "继续" if cond_val else "退出",
                    _iter_count, max_iter,
                )
                if not cond_val:
                    break

    logger.info("  [%s] loop 结束  总迭代=%d  路径=%s", step_id, _iter_count, get_step_path())
    return state if state else last_result

async def _run_cond(step: dict, step_id: str, context: dict[str, Any], event_sink=None) -> Any:
    """cond 步骤内部实现。"""
    condition = step.get("condition")
    if condition is not None:
        cond_val = _substitute(condition, context)
        branch_key = "if_true" if cond_val else "if_false"
        branch = step.get(branch_key)
        logger.info("  [%s] cond if-else  condition=%s  result=%s  选择=%s  路径=%s", step_id, condition, cond_val, branch_key, get_step_path())
    else:
        on_val = _resolve_wire(step.get("on"), context)
        branch = step.get("branches", {}).get(str(on_val) if on_val is not None else "")
        if branch is None:
            branch = step.get("default")
        logger.info("  [%s] cond switch  on=%s  value=%s  匹配=%s  路径=%s", step_id, step.get("on"), on_val, "default" if branch == step.get("default") else str(on_val), get_step_path())

    if branch is None:
        logger.info("  [%s] cond 无匹配分支，返回空  路径=%s", step_id, get_step_path())
        return {}
    return await _run_step({**branch, "id": step.get("id", "_cond")}, context, event_sink)


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

    pipeline_name = config.get("name", "<unnamed>")
    logger.info("流水线开始执行: %s", pipeline_name)

    context: dict[str, Any] = dict(variables or {})
    step_outputs: dict[str, Any] = {}
    # 已提交但尚未收集结果的异步任务 {step_id: Task}
    pending: dict[str, asyncio.Task] = {}

    async def _collect_pending() -> None:
        """等待所有挂起的异步任务完成并将结果写入 context。"""
        if not pending:
            return
        t0 = time.perf_counter()
        results = await asyncio.gather(*pending.values(), return_exceptions=True)
        elapsed = time.perf_counter() - t0
        for step_id, result in zip(pending, results):
            if isinstance(result, BaseException):
                logger.error("  [%s] 异步步骤执行失败 — %s", step_id, result, exc_info=result)
                raise result
            step_outputs[step_id] = result
            context[step_id] = result
            logger.info("  [%s] 步骤完成 [async]", step_id)
        logger.info("  异步步骤批次完成 (%d 个, 总耗时=%.1fs)", len(pending), elapsed)
        pending.clear()

    for step in steps:
        step_id = step.get("id")
        if not step_id:
            raise ValueError(f"步骤缺少 id 字段: {step}")

        mode = step.get("mode", "sync")

        if mode == "async":
            # 提交到事件循环，立即继续下一步（不阻塞）
            logger.info("  [%s] 步骤提交 [async]  type=%s  ref=%s", step_id, step.get("type", "?"), step.get("ref", ""))
            ctx_snap = dict(context)
            pending[step_id] = asyncio.create_task(_run_step(step, ctx_snap))
        else:
            # sync：先等待所有挂起的异步任务，再执行本步骤
            await _collect_pending()
            logger.info("  [%s] 步骤开始 [sync]  type=%s  ref=%s", step_id, step.get("type", "?"), step.get("ref", ""))
            t0 = time.perf_counter()
            try:
                output = await _run_step(step, context)
            except Exception as exc:
                logger.error("  [%s] 步骤执行失败 (%.1fs) — %s", step_id, time.perf_counter() - t0, exc, exc_info=True)
                raise
            elapsed = time.perf_counter() - t0
            step_outputs[step_id] = output
            context[step_id] = output
            logger.info("  [%s] 步骤完成 [sync]  耗时=%.1fs  输出=%s", step_id, elapsed, _summarize_dict(output) if isinstance(output, dict) else type(output).__name__)
            logger.debug("  [%s] 步骤完成(完整输出)  output:\n%s", step_id, _truncate_content(json.dumps(output, ensure_ascii=False, default=str) if isinstance(output, dict) else str(output)))

    # 收集流水线末尾仍在运行的异步任务
    await _collect_pending()

    # final 取最后一个声明步骤的输出（而非 dict 插入顺序最后一个）
    last_id = steps[-1]["id"] if steps else None
    final = step_outputs.get(last_id) if last_id else None
    logger.info("流水线执行完成: %s", pipeline_name)
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
