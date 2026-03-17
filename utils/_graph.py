"""
_graph.py — 计算图执行器

将步骤组织为 DAG（有向无环图），通过 depends_on 声明依赖关系。
同层无依赖步骤由 asyncio.gather 自动并行执行；有依赖的步骤在其依赖完成后执行。

支持 loop_to 实现受控回跳（有限循环），在保持 DAG 拓扑的前提下允许步骤
根据条件跳回之前的步骤重新执行，适用于迭代优化、重试等场景。

━━ 用法 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 完整执行，返回所有步骤结果
    result = await run_graph_from_file("graphs/my.json", variables={...})
    result.steps["step_id"]   # 某步骤完整输出
    result.terminals          # 所有叶节点输出 dict
    result.final              # 唯一叶节点输出（或全部叶节点 dict）

    # 实时监控所有并行终端节点（aiostream.merge 交错输出，yield (step_id, token)）
    async for step_id, token in run_graph_stream(config, variables={...}):
        print(f"[{step_id}] {token}", end="", flush=True)

━━ JSON 格式 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    {
      "steps": [
        {"id": "pre",       "type": "tool",  "ref": "truncate",
         "inputs": {"text": "$.input_text"}},

        {"id": "extract",   "type": "agent", "ref": "configs/extract.json",
         "depends_on": ["pre"], "inputs": {"input_text": "pre.text"}},

        {"id": "translate", "type": "agent", "ref": "configs/chat.json",
         "depends_on": ["pre"], "inputs": {"input_text": "pre.text"}},

        {"id": "merge",     "type": "agent", "ref": "configs/summarize.json",
         "depends_on": ["extract", "translate"]}
      ]
    }

━━ loop_to 回跳 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    {
      "id": "validate",
      "type": "agent",
      "ref": "configs/validator.json",
      "depends_on": ["generate"],
      "loop_to": {
        "target": "generate",
        "condition": "{{validate.needs_retry}}",
        "max_iterations": 5,
        "carry": {
          "error_feedback": "validate.feedback"
        }
      }
    }

    当 validate 完成后，若 condition 为真且未超过 max_iterations，
    则清除 target 到当前步骤的输出，将 carry 注入 context，从 target 重新执行。

━━ 传值语法 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    inputs 连线（推荐）: "step_id.field"  /  "$.top_var"
    {{}} 插值（字符串）: "{{step_id.field}}"  /  "{{var}}"
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from aiostream import stream as astream
from pydantic import BaseModel

from ._pipeline import _run_step, _resolve_inputs, _resolve_value, _resolve_wire, _substitute
from ._const import PROJECT_ROOT as _PROJECT_ROOT
from ._trace import _summarize_dict, _truncate_content, get_step_path

logger = logging.getLogger(__name__)


# ── 断点续跑辅助函数 ──────────────────────────────────────────────────────────

def _ckpt_load(step_id: str, checkpoint_dir: str | None) -> Any | None:
    """尝试从断点目录加载步骤输出。返回 None 表示无缓存或缓存损坏。"""
    if not checkpoint_dir:
        return None
    ckpt_file = Path(checkpoint_dir) / f"{step_id}.json"
    if not ckpt_file.exists():
        return None
    try:
        return json.loads(ckpt_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ckpt_save(step_id: str, output: Any, checkpoint_dir: str | None) -> None:
    """将步骤输出序列化写入断点文件。写入失败不抛出异常（非致命）。"""
    if not checkpoint_dir:
        return
    ckpt_file = Path(checkpoint_dir) / f"{step_id}.json"
    try:
        ckpt_file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(output, dict):
            data = output
        elif hasattr(output, "model_dump"):
            data = output.model_dump()
        else:
            data = {"__scalar__": str(output)}
        ckpt_file.write_text(
            json.dumps(data, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("  断点写入失败  step=%s  error=%s", step_id, exc)


# ── 返回值 ────────────────────────────────────────────────────────────────────

class GraphResult(BaseModel):
    """计算图执行结果。

    Attributes:
        steps:     所有步骤的输出，键为步骤 id。
        terminals: 没有后继步骤的终端节点输出（叶节点）。
        final:     若只有一个终端节点则返回其结果，否则同 terminals。

    Example:
        result.steps["extract"]["name"]     # 某步骤某字段
        result.terminals                    # 所有叶节点结果
        result.final                        # 唯一叶节点的结果（或全部叶节点）
    """

    steps: dict[str, Any]
    terminals: dict[str, Any]
    final: Any

    model_config = {"frozen": True}


# ── 图 outputs 映射 ──────────────────────────────────────────────────────────

def _resolve_graph_outputs(config: dict, context: dict[str, Any]) -> dict[str, Any] | None:
    """处理图配置顶层 outputs 字段（如有），返回命名输出 dict。

    outputs 字段允许图将终端节点的字段以别名暴露给父图，例如：
        "outputs": {
            "final_state_path": "run_iteration.state_path",
            "best_code":        "run_iteration.best_code"
        }

    父图通过 "u2e_graph.final_state_path" 等引用访问这些命名输出。
    若无 outputs 字段或字段为空，返回 None（调用方默认使用终端节点输出）。
    """
    outputs_schema = config.get("outputs")
    if not outputs_schema or not isinstance(outputs_schema, dict):
        return None
    result: dict[str, Any] = {}
    for key, wire in outputs_schema.items():
        if key.startswith("_"):   # 跳过 _comment 等注释字段
            continue
        result[key] = _resolve_wire(wire, context)
    return result if result else None


# ── DAG 构建与验证 ─────────────────────────────────────────────────────────────

def _build_levels(steps: list[dict]) -> list[list[dict]]:
    """将步骤列表按依赖关系分为若干执行层，同层内可并行。

    Args:
        steps: 步骤列表，每个步骤可含 depends_on 字段。

    Returns:
        层列表，每层是可并行执行的步骤列表。

    Raises:
        ValueError: 存在循环依赖或 depends_on 引用了不存在的步骤 id。
    """
    all_ids = {s["id"] for s in steps}

    # 校验 depends_on 引用
    for step in steps:
        for dep in step.get("depends_on", []):
            if dep not in all_ids:
                raise ValueError(
                    f"步骤 '{step['id']}' 的 depends_on 引用了不存在的步骤 '{dep}'"
                )

    completed: set[str] = set()
    remaining = list(steps)
    levels: list[list[dict]] = []

    while remaining:
        ready = [
            s for s in remaining
            if all(dep in completed for dep in s.get("depends_on", []))
        ]
        if not ready:
            cycle_ids = [s["id"] for s in remaining]
            raise ValueError(f"检测到循环依赖，涉及步骤: {cycle_ids}")
        levels.append(ready)
        completed.update(s["id"] for s in ready)
        remaining = [s for s in remaining if s["id"] not in completed]

    return levels


def _validate_loop_to(steps: list[dict]) -> None:
    """校验所有 loop_to 声明的合法性。

    Raises:
        ValueError: loop_to.target 不存在、缺少 max_iterations、或目标在当前步骤之后。
    """
    all_ids = {s["id"] for s in steps}
    id_order = {s["id"]: i for i, s in enumerate(steps)}

    for step in steps:
        loop_cfg = step.get("loop_to")
        if not loop_cfg:
            continue
        target = loop_cfg.get("target")
        if not target:
            raise ValueError(f"步骤 '{step['id']}' 的 loop_to 缺少 target 字段")
        if target not in all_ids:
            raise ValueError(
                f"步骤 '{step['id']}' 的 loop_to.target '{target}' 不存在"
            )
        if "max_iterations" not in loop_cfg:
            raise ValueError(
                f"步骤 '{step['id']}' 的 loop_to 缺少 max_iterations 字段"
            )
        if id_order.get(target, 0) > id_order.get(step["id"], 0):
            raise ValueError(
                f"步骤 '{step['id']}' 的 loop_to.target '{target}' "
                "必须在当前步骤之前（不支持前跳）"
            )


def _find_level_index(levels: list[list[dict]], target_id: str) -> int:
    """找到包含指定步骤 id 的层级索引。"""
    for i, level in enumerate(levels):
        if any(s["id"] == target_id for s in level):
            return i
    raise ValueError(f"loop_to 目标步骤 '{target_id}' 不在任何层级中")


def _find_terminals(steps: list[dict]) -> set[str]:
    """找出没有后继的终端步骤（叶节点）。"""
    depended_on = {dep for s in steps for dep in s.get("depends_on", [])}
    return {s["id"] for s in steps} - depended_on


def _process_loop_to(
    level: list[dict],
    loop_counts: dict[str, int],
    levels: list[list[dict]],
    level_idx: int,
    context: dict[str, Any],
    all_outputs: dict[str, Any] | None = None,
) -> tuple[int | None, str | None, dict[str, Any]]:
    """检测当前层的 loop_to 条件，满足时执行回跳准备。

    对于满足条件的第一个 loop_to 步骤：
    - 递增该步骤的回跳计数
    - 将 carry 变量注入 context
    - 清除目标层到当前层的步骤输出（context 中的步骤 id 以及 all_outputs 中的条目）
    - 返回目标层索引、触发步骤 id、carry 字典

    Args:
        level:       当前层的步骤列表。
        loop_counts: 各步骤的已回跳次数（会被修改）。
        levels:      全部层级列表。
        level_idx:   当前层索引。
        context:     运行时上下文（会被修改：注入 carry、清除步骤输出）。
        all_outputs: 若提供，同步清除其中的步骤输出（run_graph / run_graph_events 使用）。

    Returns:
        (jump_target_idx, jump_from_step, jump_carry)
        - jump_target_idx: 回跳目标层索引，无回跳时为 None
        - jump_from_step:  触发回跳的步骤 id，无回跳时为 None
        - jump_carry:      已注入的 carry 键值对，无回跳时为空 dict
    """
    for step in level:
        loop_cfg = step.get("loop_to")
        if not loop_cfg:
            continue

        step_id = step["id"]
        max_iter = loop_cfg["max_iterations"]
        count = loop_counts.get(step_id, 0)
        if count >= max_iter:
            logger.info(
                "  [%s] loop_to 检查: 已达最大迭代次数 %d/%d，不再回跳",
                step_id, count, max_iter,
            )
            continue

        # 解析条件
        condition_raw = loop_cfg.get("condition")
        if condition_raw is not None:
            condition_val = _substitute(condition_raw, context)
            negate = loop_cfg.get("condition_negate", False)
            original_val = condition_val
            if negate:
                condition_val = not condition_val
            logger.info(
                "  [%s] loop_to 条件检查: condition=%s  原始值=%s  negate=%s  最终=%s  → %s  (%d/%d)",
                step_id, condition_raw, original_val, negate, condition_val,
                "回跳" if condition_val else "继续",
                count + 1, max_iter,
            )
            if not condition_val:
                continue
        else:
            logger.info(
                "  [%s] loop_to 无条件回跳  (%d/%d)",
                step_id, count + 1, max_iter,
            )

        # 条件满足，执行回跳
        loop_counts[step_id] = count + 1
        target_id = loop_cfg["target"]
        jump_target_idx = _find_level_index(levels, target_id)

        # 注入 carry 变量
        jump_carry: dict[str, Any] = {}
        for key, wire in loop_cfg.get("carry", {}).items():
            val = _resolve_wire(wire, context)
            context[key] = val
            jump_carry[key] = val

        # 清除从目标层到当前层的步骤输出
        for clear_idx in range(jump_target_idx, level_idx + 1):
            for s in levels[clear_idx]:
                sid = s["id"]
                context.pop(sid, None)
                if all_outputs is not None:
                    all_outputs.pop(sid, None)

        return jump_target_idx, step_id, jump_carry

    return None, None, {}


# ── 计算图入口 ────────────────────────────────────────────────────────────────

async def run_graph(
    config: dict, variables: dict[str, Any] | None = None
) -> GraphResult:
    """执行计算图配置，自动并行调度无依赖关系的步骤。

    Args:
        config:    图配置字典（需含 steps 列表，步骤可含 depends_on）。
        variables: 顶层输入变量，在步骤的 params / variables 中用 {{var}} 引用。

    Returns:
        GraphResult:
            .steps     全部步骤输出（按 id 索引）
            .terminals 终端节点（叶节点）输出
            .final     唯一叶节点的结果，或全部叶节点 dict

    Raises:
        ValueError: 循环依赖、引用不存在的步骤、缺少必填字段等。

    Example:
        >>> result = await run_graph(config, {"input_text": "...", "operator_name": "系统"})
        >>> result.steps["extract"]["name"]
        '张三'
        >>> result.terminals
        {'merge': {'summary': '...', 'keywords': [...]}}
    """
    steps = config.get("steps", [])
    if not steps:
        raise ValueError("图配置缺少 steps 列表")

    levels = _build_levels(steps)
    _validate_loop_to(steps)
    terminal_ids = _find_terminals(steps)

    context: dict[str, Any] = dict(variables or {})
    all_outputs: dict[str, Any] = {}
    loop_counts: dict[str, int] = {}  # step_id → 已回跳次数
    checkpoint_dir: str | None = (variables or {}).get("checkpoint_dir")

    graph_name = config.get("name", "<unnamed>")
    step_count = len(steps)
    level_count = len(levels)
    step_types = {}
    for s in steps:
        t = s.get("type", "?")
        step_types[t] = step_types.get(t, 0) + 1
    logger.info(
        "图开始执行: %s  步骤数=%d  层数=%d  类型分布=%s  终端节点=%s  变量键=%s",
        graph_name, step_count, level_count, step_types,
        list(terminal_ids), list((variables or {}).keys()),
    )

    level_idx = 0
    while level_idx < len(levels):
        level = levels[level_idx]

        # mode: "sync" → 强制串行；其余（默认）→ 并行
        sync_steps  = [s for s in level if s.get("mode") == "sync"]
        async_steps = [s for s in level if s.get("mode") != "sync"]

        # 串行步骤先逐一执行
        for step in sync_steps:
            sid = step["id"]
            cached = _ckpt_load(sid, checkpoint_dir)
            if cached is not None:
                logger.info("  [%s] 步骤跳过（断点缓存命中）  type=%s", sid, step.get("type", "?"))
                all_outputs[sid] = cached
                context[sid] = cached
                continue
            logger.info("  [%s] 步骤开始 [sync]  type=%s  ref=%s", sid, step.get("type", "?"), step.get("ref", ""))
            t0 = time.perf_counter()
            try:
                output = await _run_step(step, context)
            except Exception as exc:
                logger.error("  [%s] 步骤执行失败 [sync] (%.1fs) — %s  路径=%s  可用上下文键=%s", sid, time.perf_counter() - t0, exc, get_step_path(), list(context.keys()), exc_info=True)
                raise
            elapsed = time.perf_counter() - t0
            all_outputs[sid] = output
            context[sid] = output
            _ckpt_save(sid, output, checkpoint_dir)
            logger.info("  [%s] 步骤完成 [sync]  耗时=%.1fs  输出=%s", sid, elapsed, _summarize_dict(output) if isinstance(output, dict) else type(output).__name__)
            logger.debug("  [%s] 步骤完成(完整输出)  output:\n%s", sid, _truncate_content(json.dumps(output, ensure_ascii=False, default=str) if isinstance(output, dict) else str(output)))

        # 并行步骤：asyncio.gather 替代 ThreadPoolExecutor
        if len(async_steps) == 1:
            step = async_steps[0]
            sid = step["id"]
            cached = _ckpt_load(sid, checkpoint_dir)
            if cached is not None:
                logger.info("  [%s] 步骤跳过（断点缓存命中）  type=%s", sid, step.get("type", "?"))
                all_outputs[sid] = cached
                context[sid] = cached
            else:
                logger.info("  [%s] 步骤开始  type=%s  ref=%s", sid, step.get("type", "?"), step.get("ref", ""))
                t0 = time.perf_counter()
                try:
                    output = await _run_step(step, context)
                except Exception as exc:
                    logger.error("  [%s] 步骤执行失败 (%.1fs) — %s  路径=%s  可用上下文键=%s", sid, time.perf_counter() - t0, exc, get_step_path(), list(context.keys()), exc_info=True)
                    raise
                elapsed = time.perf_counter() - t0
                all_outputs[sid] = output
                context[sid] = output
                _ckpt_save(sid, output, checkpoint_dir)
                logger.info("  [%s] 步骤完成  耗时=%.1fs  输出=%s", sid, elapsed, _summarize_dict(output) if isinstance(output, dict) else type(output).__name__)
                logger.debug("  [%s] 步骤完成(完整输出)  output:\n%s", sid, _truncate_content(json.dumps(output, ensure_ascii=False, default=str) if isinstance(output, dict) else str(output)))
        elif async_steps:
            # 拆分：有断点的直接加载，无断点的走 gather
            ckpt_ready = [(s, _ckpt_load(s["id"], checkpoint_dir)) for s in async_steps]
            need_exec  = [s for s, c in ckpt_ready if c is None]
            for step, cached in ckpt_ready:
                if cached is not None:
                    logger.info("  [%s] 步骤跳过（断点缓存命中）  type=%s", step["id"], step.get("type", "?"))
                    all_outputs[step["id"]] = cached
                    context[step["id"]] = cached
            if need_exec:
                step_ids = [s["id"] for s in need_exec]
                logger.info("  并行执行 %d 个步骤: %s", len(need_exec), step_ids)
                for s in need_exec:
                    logger.info("    [%s] 步骤开始 [并行]  type=%s  ref=%s", s["id"], s.get("type", "?"), s.get("ref", ""))
                t0 = time.perf_counter()
                ctx_snapshot = dict(context)
                results = await asyncio.gather(
                    *[_run_step(s, ctx_snapshot) for s in need_exec],
                    return_exceptions=True,
                )
                gather_elapsed = time.perf_counter() - t0
                for step, result in zip(need_exec, results):
                    if isinstance(result, BaseException):
                        logger.error("    [%s] 步骤执行失败 [并行] — %s", step["id"], result, exc_info=result)
                        raise result
                    all_outputs[step["id"]] = result
                    context[step["id"]] = result
                    _ckpt_save(step["id"], result, checkpoint_dir)
                    logger.info("    [%s] 步骤完成 [并行]  输出=%s", step["id"], _summarize_dict(result) if isinstance(result, dict) else type(result).__name__)
                    logger.debug("    [%s] 步骤完成(完整输出) [并行]  output:\n%s", step["id"], _truncate_content(json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, dict) else str(result)))
                logger.info("  并行层完成  总耗时=%.1fs", gather_elapsed)

        # ── loop_to 回跳检测 ──────────────────────────────────────────────
        jump_target_idx, jump_from_step, jump_carry = _process_loop_to(
            level, loop_counts, levels, level_idx, context, all_outputs
        )
        if jump_target_idx is not None:
            target_step_id = levels[jump_target_idx][0]["id"]
            logger.info(
                "loop_to 回跳: %s → %s（第 %d 次）  carry=%s",
                jump_from_step, target_step_id, loop_counts.get(jump_from_step, 0),
                _summarize_dict(jump_carry) if isinstance(jump_carry, dict) and jump_carry else "{}",
            )
            level_idx = jump_target_idx
        else:
            level_idx += 1

    terminals = {sid: all_outputs[sid] for sid in terminal_ids if sid in all_outputs}
    # 若 config 声明 outputs 映射，优先使用其别名输出；否则取终端节点输出
    outputs_override = _resolve_graph_outputs(config, context)
    final = outputs_override if outputs_override is not None else (
        next(iter(terminals.values())) if len(terminals) == 1 else terminals
    )

    logger.info("图执行完成: %s，终端节点: %s", graph_name, list(terminals.keys()))
    logger.debug("图执行完成(完整final)  graph=%s  final:\n%s", graph_name, _truncate_content(json.dumps(final, ensure_ascii=False, default=str) if isinstance(final, dict) else str(final)))
    return GraphResult(steps=all_outputs, terminals=terminals, final=final)


async def run_graph_from_file(
    path: str | Path, variables: dict[str, Any] | None = None
) -> GraphResult:
    """从 JSON 文件加载计算图配置并执行。

    Args:
        path:      图 JSON 文件路径（相对路径以项目根目录为基准）。
        variables: 顶层输入变量。

    Returns:
        GraphResult

    Example:
        >>> result = await run_graph_from_file(
        ...     "graphs/parallel_analysis.json",
        ...     variables={"input_text": "...", "operator_name": "系统"},
        ... )
        >>> result.final
        {'summary': '...', 'keywords': [...]}
    """
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    config = json.loads(p.read_text(encoding="utf-8"))
    return await run_graph(config, variables)


# ── 并行流式合并（aiostream）─────────────────────────────────────────────────

async def _step_as_tagged_stream(
    step: dict, context: dict[str, Any]
) -> AsyncGenerator[tuple[str, str], None]:
    """将单个步骤的输出包装为 (step_id, token) 元组流。

    - agent + stream=true：逐 token yield
    - agent + stream=false / tool：完整输出以单条 (step_id, text) 形式发出
    """
    from llm_config import run_from_file

    step_id = step["id"]
    wired = _resolve_inputs(step.get("inputs", {}), context)

    if step["type"] == "tool":
        from ._registry import call_tool, call_from_file
        params = {**_resolve_value(step.get("params", {}), context), **wired}
        ref = step.get("ref", "")
        result = (
            call_from_file(ref, params) if Path(ref).suffix == ".json"
            else call_tool(ref, params)
        )
        yield (step_id, str(result.model_dump()))
        return

    if step["type"] == "input":
        # 输入源：异步获取数据，以单条 (step_id, text) 形式 yield
        from ._registry import call_input, call_input_from_file
        params = {**_resolve_value(step.get("params", {}), context), **wired}
        ref = step.get("ref", "")
        result = (
            await call_input_from_file(ref, params) if Path(ref).suffix == ".json"
            else await call_input(ref, params)
        )
        yield (step_id, str(result.model_dump()))
        return

    if step["type"] == "pipeline":
        # 子流水线：透传其内部 token 流，统一标注为当前节点 id
        from ._pipeline import run_pipeline_stream_from_file
        sub_vars = {**_resolve_value(step.get("variables", {}), context), **wired}
        ref = step.get("ref", "")
        async for _, token in run_pipeline_stream_from_file(ref, variables=sub_vars):
            yield (step_id, token)
        return

    if step["type"] == "graph":
        # 子图：透传其所有终端节点 token 流，统一标注为当前节点 id
        sub_vars = {**_resolve_value(step.get("variables", {}), context), **wired}
        ref = step.get("ref", "")
        async for _, token in run_graph_stream_from_file(ref, variables=sub_vars):
            yield (step_id, token)
        return

    if step["type"] in ("loop", "cond"):
        # loop / cond：完整执行，将最终结果以单条文本 yield
        result = await _run_step(step, context)
        yield (step_id, str(result))
        return

    if step["type"] != "agent":
        raise ValueError(
            f"未知步骤类型: {step['type']!r}，"
            "仅支持 'agent' / 'tool' / 'input' / 'pipeline' / 'graph' / 'loop' / 'cond'"
        )

    # agent 步骤
    variables = {**_resolve_value(step.get("variables", {}), context), **wired}
    history = context.get("__history__") if step.get("use_history") else None
    ref = step.get("ref", "")
    ref_path = _PROJECT_ROOT / ref if not Path(ref).is_absolute() else Path(ref)
    result = await run_from_file(ref_path, variables=variables, history=history)

    if hasattr(result, "__aiter__"):
        async for token in result:
            yield (step_id, token)
    else:
        yield (step_id, str(result))


async def run_graph_events(
    config: dict, variables: dict[str, Any] | None = None
) -> AsyncGenerator[dict[str, Any], None]:
    """执行计算图，产出结构化生命周期事件流，供后端持续运行时追踪会话状态。

    与 run_graph_stream 不同，本函数产出的是步骤生命周期事件（dict），而非 LLM token 流。
    这使调用方（如 FastAPI 后端）可以准确追踪每个步骤的开始、完成、错误以及 loop_to 回跳。

    事件格式:
        {"type": "step_start", "step_id": str, "step_name": str, "invocation": int}
        {"type": "step_done",  "step_id": str, "outputs": dict | str}
        {"type": "step_error", "step_id": str, "error": str}
        {"type": "loop_jump",  "from_step": str, "to_step": str,
                               "iteration": int, "carry": dict}
        {"type": "graph_done", "final": Any, "steps": dict}

    Args:
        config:    图配置字典（需含 steps 列表，步骤可含 depends_on）。
        variables: 顶层输入变量。

    Yields:
        结构化事件 dict，type 字段标识事件类型。

    Raises:
        ValueError: 循环依赖、引用不存在的步骤、缺少必填字段等。

    Example:
        async for event in run_graph_events(config, variables={"channel_id": "ch_001"}):
            if event["type"] == "step_start":
                print(f"开始执行: {event['step_id']}")
            elif event["type"] == "step_done":
                print(f"完成: {event['step_id']} → {event['outputs']}")
    """
    steps = config.get("steps", [])
    if not steps:
        raise ValueError("图配置缺少 steps 列表")

    levels = _build_levels(steps)
    _validate_loop_to(steps)
    terminal_ids = _find_terminals(steps)

    context: dict[str, Any] = dict(variables or {})
    all_outputs: dict[str, Any] = {}
    loop_counts: dict[str, int] = {}
    invocations: dict[str, int] = {}  # step_id → 累计调用次数（含 loop 重跑）
    checkpoint_dir: str | None = (variables or {}).get("checkpoint_dir")

    def _to_outputs(output: Any) -> Any:
        """将步骤输出规范化为可序列化格式。"""
        if isinstance(output, dict):
            return output
        if hasattr(output, "model_dump"):
            return output.model_dump()
        return {"result": str(output)}

    level_idx = 0
    while level_idx < len(levels):
        level = levels[level_idx]

        sync_steps  = [s for s in level if s.get("mode") == "sync"]
        async_steps = [s for s in level if s.get("mode") != "sync"]

        # 串行步骤
        for step in sync_steps:
            step_id = step["id"]
            invocations[step_id] = invocations.get(step_id, 0) + 1
            # 断点命中：直接加载缓存跳过执行
            cached = _ckpt_load(step_id, checkpoint_dir)
            if cached is not None:
                yield {"type": "step_start", "step_id": step_id, "step_name": step.get("name", step_id), "invocation": invocations[step_id], "skipped": True}
                all_outputs[step_id] = cached
                context[step_id] = cached
                yield {"type": "step_done", "step_id": step_id, "outputs": _to_outputs(cached), "skipped": True}
                continue
            yield {
                "type": "step_start",
                "step_id": step_id,
                "step_name": step.get("name", step_id),
                "invocation": invocations[step_id],
            }
            try:
                output = None
                async for item in _execute_step_for_events(step, context):
                    if isinstance(item, tuple) and len(item) == 2 and item[0] == "__result__":
                        output = item[1]
                    else:
                        yield item  # 透传子图内部事件
                all_outputs[step_id] = output
                context[step_id] = output
                _ckpt_save(step_id, output, checkpoint_dir)
                yield {"type": "step_done", "step_id": step_id, "outputs": _to_outputs(output)}
            except Exception as exc:
                yield {"type": "step_error", "step_id": step_id, "error": str(exc)}
                raise

        # 并行步骤（含单步并行退化情形）
        if len(async_steps) == 1:
            step = async_steps[0]
            step_id = step["id"]
            invocations[step_id] = invocations.get(step_id, 0) + 1
            cached = _ckpt_load(step_id, checkpoint_dir)
            if cached is not None:
                yield {"type": "step_start", "step_id": step_id, "step_name": step.get("name", step_id), "invocation": invocations[step_id], "skipped": True}
                all_outputs[step_id] = cached
                context[step_id] = cached
                yield {"type": "step_done", "step_id": step_id, "outputs": _to_outputs(cached), "skipped": True}
            else:
                yield {
                    "type": "step_start",
                    "step_id": step_id,
                    "step_name": step.get("name", step_id),
                    "invocation": invocations[step_id],
                }
                try:
                    output = None
                    async for item in _execute_step_for_events(step, context):
                        if isinstance(item, tuple) and len(item) == 2 and item[0] == "__result__":
                            output = item[1]
                        else:
                            yield item  # 透传子图内部事件
                    all_outputs[step_id] = output
                    context[step_id] = output
                    _ckpt_save(step_id, output, checkpoint_dir)
                    yield {"type": "step_done", "step_id": step_id, "outputs": _to_outputs(output)}
                except Exception as exc:
                    yield {"type": "step_error", "step_id": step_id, "error": str(exc)}
                    raise
        elif async_steps:
            # 拆分：有断点的直接加载，无断点的走 gather
            ckpt_pairs = [(s, _ckpt_load(s["id"], checkpoint_dir)) for s in async_steps]
            need_exec  = [s for s, c in ckpt_pairs if c is None]

            # 断点命中步骤：立即发出 step_start + step_done
            for step, cached in ckpt_pairs:
                if cached is not None:
                    step_id = step["id"]
                    invocations[step_id] = invocations.get(step_id, 0) + 1
                    yield {"type": "step_start", "step_id": step_id, "step_name": step.get("name", step_id), "invocation": invocations[step_id], "skipped": True}
                    all_outputs[step_id] = cached
                    context[step_id] = cached
                    yield {"type": "step_done", "step_id": step_id, "outputs": _to_outputs(cached), "skipped": True}

            # 非断点步骤：先统一发出 step_start，再并行执行
            for step in need_exec:
                step_id = step["id"]
                invocations[step_id] = invocations.get(step_id, 0) + 1
                yield {
                    "type": "step_start",
                    "step_id": step_id,
                    "step_name": step.get("name", step_id),
                    "invocation": invocations[step_id],
                }

            if need_exec:
                ctx_snapshot = dict(context)
                # 共享事件队列：并行步骤的子图内部事件实时透传
                eq: asyncio.Queue[Any] = asyncio.Queue()
                _PAR_SENTINEL = object()

                async def _par_sink(event: dict) -> None:
                    await eq.put(event)

                async def _gather_with_sink() -> list:
                    try:
                        return await asyncio.gather(
                            *[_run_step(s, ctx_snapshot, event_sink=_par_sink) for s in need_exec],
                            return_exceptions=True,
                        )
                    finally:
                        await eq.put(_PAR_SENTINEL)

                gather_task = asyncio.create_task(_gather_with_sink())

                # 边执行边消费子图事件
                while True:
                    item = await eq.get()
                    if item is _PAR_SENTINEL:
                        break
                    yield item

                results = await gather_task
                for step, result in zip(need_exec, results):
                    step_id = step["id"]
                    if isinstance(result, BaseException):
                        yield {"type": "step_error", "step_id": step_id, "error": str(result)}
                        raise result
                    all_outputs[step_id] = result
                    context[step_id] = result
                    _ckpt_save(step_id, result, checkpoint_dir)
                    yield {"type": "step_done", "step_id": step_id, "outputs": _to_outputs(result)}

        # ── loop_to 回跳检测 ──────────────────────────────────────────────
        jump_target_idx, jump_from_step, jump_carry = _process_loop_to(
            level, loop_counts, levels, level_idx, context, all_outputs
        )

        if jump_target_idx is not None:
            yield {
                "type":      "loop_jump",
                "from_step": jump_from_step,
                "to_step":   levels[jump_target_idx][0]["id"],
                "iteration": loop_counts.get(jump_from_step, 0),
                "carry":     jump_carry,
            }
            level_idx = jump_target_idx
        else:
            level_idx += 1

    terminals = {sid: all_outputs[sid] for sid in terminal_ids if sid in all_outputs}
    outputs_override = _resolve_graph_outputs(config, context)
    final = outputs_override if outputs_override is not None else (
        next(iter(terminals.values())) if len(terminals) == 1 else terminals
    )

    yield {
        "type":  "graph_done",
        "final": final,
        "steps": {k: _to_outputs(v) for k, v in all_outputs.items()},
    }


async def _execute_step_for_events(
    step: dict, context: dict[str, Any]
) -> AsyncGenerator:
    """单步执行辅助生成器，供 run_graph_events 内部使用。

    通过向 _run_step 传入 event_sink 回调，将所有子图/cond/loop/map 内部事件
    实时推入队列，由本生成器统一 yield 给调用方。

    无需按步骤类型分别处理 —— 所有透传逻辑由 _run_step 内部自行完成。

    调用方通过检查 item[0] == "__result__" 来区分结果哨兵与透传事件。
    """
    q: asyncio.Queue[Any] = asyncio.Queue()
    _SENTINEL = object()

    async def sink(event: dict) -> None:
        await q.put(event)

    async def _do() -> Any:
        try:
            return await _run_step(step, context, event_sink=sink)
        finally:
            await q.put(_SENTINEL)

    task = asyncio.create_task(_do())

    while True:
        item = await q.get()
        if item is _SENTINEL:
            break
        yield item

    output = await task  # 若 _run_step 异常，此处重新抛出
    yield ("__result__", output)


async def run_graph_stream_from_file(
    path: str | Path, variables: dict[str, Any] | None = None
) -> AsyncGenerator[tuple[str, str], None]:
    """从 JSON 文件加载计算图配置并以流式方式执行所有终端节点。

    Args:
        path:      图 JSON 文件路径（相对路径以项目根目录为基准）。
        variables: 顶层输入变量。

    Yields:
        (step_id, token) 元组。
    """
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    config = json.loads(p.read_text(encoding="utf-8"))
    async for item in run_graph_stream(config, variables):
        yield item


async def run_graph_stream(
    config: dict, variables: dict[str, Any] | None = None
) -> AsyncGenerator[tuple[str, str], None]:
    """执行计算图，将终端节点的输出合并为统一的 (step_id, token) 流。

    非终端节点（中间步骤）完整执行后将结果注入 context 供下游使用。
    终端节点使用 aiostream.stream.merge 并发交错消费各节点的 token 流——
    无论节点数量多少，所有终端节点的 token 都在同一个事件循环内实时交错输出，
    无需等待任何一个节点完成后再开始下一个。

    Args:
        config:    图配置字典。
        variables: 顶层输入变量。

    Yields:
        (step_id, token) 元组：step_id 标识来源节点，token 为文本片段。

    Example:
        async for step_id, token in run_graph_stream(config, variables):
            print(f"[{step_id}] {token}", end="", flush=True)
    """
    steps = config.get("steps", [])
    if not steps:
        raise ValueError("图配置缺少 steps 列表")

    levels = _build_levels(steps)
    _validate_loop_to(steps)
    terminal_ids = _find_terminals(steps)
    context: dict[str, Any] = dict(variables or {})
    loop_counts: dict[str, int] = {}

    level_idx = 0
    while level_idx < len(levels):
        level = levels[level_idx]
        sync_steps  = [s for s in level if s.get("mode") == "sync"]
        async_steps = [s for s in level if s.get("mode") != "sync"]

        # 串行步骤
        for step in sync_steps:
            output = await _run_step(step, context)
            context[step["id"]] = output

        # 区分终端节点与非终端节点
        terminal_steps  = [s for s in async_steps if s["id"] in terminal_ids]
        intermediate    = [s for s in async_steps if s["id"] not in terminal_ids]

        # 非终端节点：完整执行，结果注入 context
        if intermediate:
            ctx_snap = dict(context)
            results = await asyncio.gather(
                *[_run_step(s, ctx_snap) for s in intermediate],
                return_exceptions=True,
            )
            for step, result in zip(intermediate, results):
                if isinstance(result, BaseException):
                    raise result
                context[step["id"]] = result

        # 终端节点：aiostream.stream.merge 并发交错输出各节点 token 流
        if terminal_steps:
            ctx_snap = dict(context)
            generators = [_step_as_tagged_stream(s, ctx_snap) for s in terminal_steps]
            merged = astream.merge(*generators)
            async with merged.stream() as streamer:
                async for step_id, token in streamer:
                    yield (step_id, token)

        # ── loop_to 回跳检测（流式版本） ──────────────────────────────────
        # 流式版本无 all_outputs，传 None 仅清除 context 中的步骤输出
        jump_target_idx, _, _ = _process_loop_to(
            level, loop_counts, levels, level_idx, context, all_outputs=None
        )
        if jump_target_idx is not None:
            level_idx = jump_target_idx
        else:
            level_idx += 1


# ── stream_graph（供 serve.py 使用的文件路径版本）─────────────────────────────

async def stream_graph(
    path: str | Path, variables: dict[str, Any] | None = None
) -> AsyncGenerator[dict[str, Any], None]:
    """从 JSON 文件加载计算图并产出步骤生命周期事件。

    与 run_graph_events 功能相同，区别在于接受文件路径而非 config dict，
    供 serve.py 等调用方使用。支持通过 variables["checkpoint_dir"] 断点续跑。

    Yields:
        结构化事件 dict（同 run_graph_events）：
        step_start / step_done / step_error / loop_jump / graph_done
    """
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    config = json.loads(p.read_text(encoding="utf-8"))
    async for event in run_graph_events(config, variables):
        yield event
