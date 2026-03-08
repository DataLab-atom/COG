"""open-agents 图执行器（含 carry 支持）。

支持的步骤类型：
  agent   — 调用 LLM Agent（ref 指向 config JSON 路径）
  tool    — 调用同步 Python 函数（ref 指向 tools/<name>.json）
  input   — 调用异步 Python 函数（ref 指向 input/<name>.json）
  map     — 对列表并发执行子步骤

控制流原语：
  depends_on   — 声明依赖，保证执行顺序
  loop_to      — 条件跳回（实现 while 语义），支持 max_iterations
  carry        — 循环携带变量（NEW）：让步骤的状态在 loop_to 迭代间可见

carry 语义
----------
步骤定义中声明：

    "carry": {
        "field": {
            "init":   "path.to.initial",   # 第一次迭代的初始值来源
            "update": "step_id.field"       # 后续迭代的更新值来源（可自引用）
        }
    }

在 inputs 中用 "$.carry.<field>" 引用携带值。
当 loop_to 触发时，框架在清空输出前先抓取 update 路径的值存入 carry_state，
下一轮执行时注入。这样 carry 值在图的每一轮都是可见的步骤输出。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ── 结果类型 ──────────────────────────────────────────────────────────────────

@dataclass
class GraphResult:
    """图执行的结果。

    Attributes:
        final: 按图 outputs 字段映射的最终输出。
        steps: 所有步骤的输出，键为步骤 ID。
    """
    final: dict
    steps: dict


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _load_json(path: str | Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _to_dict(result: Any) -> dict:
    """将 ToolResult / dataclass / dict 统一转为 dict。"""
    if isinstance(result, dict):
        return result
    if hasattr(result, "to_dict"):
        return result.to_dict()
    if hasattr(result, "__dataclass_fields__"):
        return {k: getattr(result, k) for k in result.__dataclass_fields__}
    if hasattr(result, "__dict__"):
        return result.__dict__
    return {"value": result}


def _get_nested(obj: Any, field_path: str) -> Any:
    """用点路径遍历嵌套 dict/对象，如 'state.frontier'。"""
    if not field_path:
        return obj
    for part in field_path.split("."):
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif hasattr(obj, part):
            obj = getattr(obj, part)
        else:
            return None
    return obj


def _resolve_path(path: Any, outputs: dict, variables: dict, carry: dict) -> Any:
    """将路径字符串解析为实际值。

    支持的格式：
      $.carry.<field>  → 当前步骤的 carry 值
      $.<var>          → 图级变量
      <step>.<field>   → 前序步骤的输出字段
      <step>           → 前序步骤的完整输出
      其他             → 字面量（list / int / bool 等原样返回）
    """
    if not isinstance(path, str):
        return path  # 字面量：list、int、bool 等直接返回

    if path.startswith("$.carry."):
        field = path[len("$.carry."):]
        return carry.get(field)

    if path.startswith("$."):
        var = path[2:]
        return variables.get(var)

    if "." in path:
        step_id, field_path = path.split(".", 1)
        step_out = outputs.get(step_id)
        if step_out is not None:
            return _get_nested(step_out, field_path)

    if path in outputs:
        return outputs[path]

    return path  # 无法解析则按字面量字符串返回


def _resolve_inputs(
    inputs_def: dict,
    outputs: dict,
    variables: dict,
    carry: dict,
) -> dict:
    return {
        param: _resolve_path(path, outputs, variables, carry)
        for param, path in inputs_def.items()
    }


def _eval_condition(condition_str: str, outputs: dict) -> bool:
    """解析 '{{step_id.field}}' 模板并返回布尔值。"""
    m = re.fullmatch(r"\{\{([^}]+)\}\}", condition_str.strip())
    if not m:
        return False
    path = m.group(1)
    if "." in path:
        step_id, field = path.split(".", 1)
        val = _get_nested(outputs.get(step_id, {}), field)
    else:
        val = outputs.get(path)
    return bool(val)


# ── 步骤执行器 ────────────────────────────────────────────────────────────────

async def _exec_tool(step: dict, inputs: dict, base_dir: Path) -> dict:
    """执行 tool 步骤：加载 tools/<ref>.json，调用对应 Python 函数。"""
    tool_def = _load_json(base_dir / "tools" / f"{step['ref']}.json")
    module = importlib.import_module(tool_def["module"])
    func = getattr(module, tool_def["function"])
    result = func(**inputs)
    return _to_dict(result)


async def _exec_input(step: dict, inputs: dict, base_dir: Path) -> dict:
    """执行 input 步骤：加载 input/<ref>.json，调用对应异步/同步函数。"""
    input_def = _load_json(base_dir / "input" / f"{step['ref']}.json")
    module = importlib.import_module(input_def["module"])
    func = getattr(module, input_def["function"])
    if input_def.get("async", False):
        result = await func(**inputs)
    else:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: func(**inputs))
    return _to_dict(result)


async def _exec_agent(step: dict, inputs: dict, base_dir: Path) -> dict:
    """执行 agent 步骤：加载 LLM agent 配置，调用模型。"""
    ref = step["ref"]
    config_path = base_dir / ref if not Path(ref).is_absolute() else Path(ref)
    config = _load_json(config_path)

    # 使用 COG 的 LLM 基础设施
    from LLM_MODULE.llm import call_agent  # type: ignore
    result = await call_agent(config, inputs, base_dir=str(base_dir))
    return result if isinstance(result, dict) else _to_dict(result)


async def _exec_map(
    step: dict,
    outputs: dict,
    variables: dict,
    carry_state: dict,
    base_dir: Path,
) -> dict:
    """执行 map 步骤：对列表每个元素并发执行子步骤。"""
    over = _resolve_path(
        step["over"], outputs, variables,
        carry_state.get(step.get("id", ""), {}),
    )
    if not isinstance(over, list):
        over = []

    iter_var = step["as"]
    sub_step = step["step"]

    async def run_one(item: Any) -> dict:
        sub_vars = {**variables, iter_var: item}
        sub_inputs = _resolve_inputs(
            sub_step.get("inputs", {}), outputs, sub_vars, {}
        )
        return await _exec_one(sub_step, sub_inputs, sub_vars, outputs, {}, base_dir)

    results = await asyncio.gather(*[run_one(item) for item in over])
    return {"results": list(results)}


async def _exec_one(
    step: dict,
    inputs: dict,
    variables: dict,
    outputs: dict,
    carry_state: dict,
    base_dir: Path,
) -> dict:
    """分发到具体步骤类型执行器。"""
    t = step["type"]
    if t == "tool":
        return await _exec_tool(step, inputs, base_dir)
    if t == "input":
        return await _exec_input(step, inputs, base_dir)
    if t == "agent":
        return await _exec_agent(step, inputs, base_dir)
    if t == "map":
        return await _exec_map(step, outputs, variables, carry_state, base_dir)
    raise ValueError(f"未知步骤类型: {t!r}")


# ── 主执行引擎 ────────────────────────────────────────────────────────────────

async def run_graph(
    graph: dict,
    variables: dict | None = None,
    base_dir: str | Path = ".",
) -> GraphResult:
    """执行一个图，返回 GraphResult。

    执行顺序：按 steps 列表顺序（已拓扑排序）顺序执行。
    loop_to 触发时跳回 target 步骤，carry_state 在迭代间持久化。

    carry 生命周期：
      1. 第一次执行有 carry 的步骤：从 carry[field].init 路径初始化。
      2. loop_to 触发前：从 carry[field].update 路径抓取最新值存入 carry_state。
      3. 清空 target..current 的输出。
      4. 从 target 步骤重新执行，有 carry 的步骤直接从 carry_state 注入，
         跳过 init 路径。
    """
    variables = variables or {}
    base_dir = Path(base_dir)
    outputs: dict[str, dict] = {}
    carry_state: dict[str, dict] = {}   # step_id → {field → value}
    loop_counts: dict[str, int] = {}    # step_id（有 loop_to 的步骤）→ 已触发次数

    steps_list: list[dict] = graph.get("steps", [])
    steps_by_id: dict[str, dict] = {s["id"]: s for s in steps_list}
    exec_order: list[str] = [s["id"] for s in steps_list]

    i = 0
    while i < len(exec_order):
        step_id = exec_order[i]
        step = steps_by_id[step_id]

        # ── carry 注入 ────────────────────────────────────────────────────────
        # 第一次执行：从 init 路径初始化；后续迭代：直接用 carry_state（已在
        # loop_to 触发时更新好了）。
        step_carry: dict = {}
        if "carry" in step:
            if step_id not in carry_state:
                carry_state[step_id] = {}
                for field, carry_def in step["carry"].items():
                    carry_state[step_id][field] = _resolve_path(
                        carry_def["init"], outputs, variables, {}
                    )
            step_carry = carry_state[step_id]

        # ── 输入解析 ──────────────────────────────────────────────────────────
        resolved = _resolve_inputs(step.get("inputs", {}), outputs, variables, step_carry)

        # ── 执行 ──────────────────────────────────────────────────────────────
        output = await _exec_one(step, resolved, variables, outputs, carry_state, base_dir)
        outputs[step_id] = output

        # ── loop_to 检查 ──────────────────────────────────────────────────────
        if "loop_to" in step:
            loop = step["loop_to"]
            cond_val = _eval_condition(loop["condition"], outputs)
            should_loop = (not cond_val) if loop.get("condition_negate", False) else cond_val

            count = loop_counts.get(step_id, 0)
            max_iter = loop.get("max_iterations", 1)

            if should_loop and count < max_iter:
                loop_counts[step_id] = count + 1

                # ── carry 更新（在清空输出前） ────────────────────────────────
                # 遍历所有声明了 carry 的步骤，抓取 update 路径的最新值。
                for sid, s in steps_by_id.items():
                    if "carry" not in s or sid not in carry_state:
                        continue
                    for field, carry_def in s["carry"].items():
                        updated = _resolve_path(
                            carry_def["update"], outputs, variables,
                            carry_state.get(sid, {}),
                        )
                        if updated is not None:
                            carry_state[sid][field] = updated

                # ── 清空从 target 到当前步骤（含）的输出 ─────────────────────
                target_id = loop["target"]
                if target_id not in exec_order:
                    # target 不在图中，直接终止循环
                    i += 1
                    continue

                target_idx = exec_order.index(target_id)
                for sid in exec_order[target_idx: i + 1]:
                    outputs.pop(sid, None)

                # ── 跳回 target ───────────────────────────────────────────────
                i = target_idx
                continue

        i += 1

    # ── 构建最终输出 ──────────────────────────────────────────────────────────
    final: dict = {}
    for key, path in graph.get("outputs", {}).items():
        final[key] = _resolve_path(path, outputs, variables, {})

    return GraphResult(final=final, steps=dict(outputs))


async def run_graph_from_file(
    graph_path: str | Path,
    variables: dict | None = None,
    base_dir: str | Path | None = None,
) -> GraphResult:
    """从 JSON 文件加载图并执行。

    base_dir 默认为 graph_path 所在目录的上一级（即项目根目录），
    从而正确解析 tools/<name>.json、input/<name>.json 等相对路径。
    """
    path = Path(graph_path)
    if base_dir is None:
        # graphs/xxx.json → base_dir = graphs/ 的上级目录 = 项目根
        base_dir = path.parent.parent if path.parent.name == "graphs" else path.parent

    graph = _load_json(path)
    return await run_graph(graph, variables, base_dir=base_dir)
