"""
arch_internal_fanout: 并发为每个 file 迭代调用 arch_internal_definer agent。
- 每个 file 独立维护 history（多轮对话）
- 每轮将新定义的 id 追加到全局 id 列表
- 发现 needs 后立即静态解析（resolve_needs）
- depth 从 1 开始，最大 3
- 当某轮返回空 types+functions 时，该 file 完成
返回所有内部节点定义，并回填到对应 file。
"""
from __future__ import annotations
import asyncio
import json
import copy
from typing import Any

from llm_config import LLMConfig, load_config_from_file, run as llm_run
from utils._base import ToolResult
from arch.resolve_needs import arch_resolve_needs


class ArchInternalFanoutResult(ToolResult):
    ok: bool
    errors: list[str]
    files: list[dict]
    types: list[dict]
    functions: list[dict]
    global_ids: list[str]
    tree: list[dict]  # files + types + functions 合并，供后续步骤直接使用


_MAX_ROUNDS = 10  # 单文件最大迭代轮次


async def _define_internals_for_file(
    file: dict,
    global_ids: list[str],
    requirement: str,
    config: LLMConfig,
) -> dict[str, Any]:
    history: list[dict] = []
    history_defined: list[dict] = []
    local_types: list[dict] = []
    local_fns: list[dict] = []
    local_ids = list(global_ids)
    errors: list[str] = []

    for round_idx in range(_MAX_ROUNDS):
        depth = min(round_idx + 1, 3)
        variables = {
            "requirement":          requirement,
            "file_json":            json.dumps(file, ensure_ascii=False, indent=2),
            "global_ids_json":      json.dumps(local_ids, ensure_ascii=False, indent=2),
            "depth":                str(depth),
            "history_defined_json": json.dumps(history_defined, ensure_ascii=False, indent=2),
        }

        try:
            result = await llm_run(config, variables, history=history)
        except Exception as e:
            errors.append(f"{file['id']} 第 {round_idx+1} 轮失败: {e}")
            break

        if not isinstance(result, dict):
            break

        history.append({"role": "assistant", "content": json.dumps(result, ensure_ascii=False)})

        types = result.get("types", [])
        fns   = result.get("functions", [])

        if not types and not fns:
            break

        for t in types:
            t["kind"] = "type"
            file.setdefault("types", []).append(t["id"])
            local_ids.append(t["id"])
            history_defined.append({"kind": "type", "id": t["id"]})
        for fn in fns:
            fn["kind"] = "function"
            file.setdefault("functions", []).append(fn["id"])
            local_ids.append(fn["id"])
            history_defined.append({"kind": "function", "id": fn["id"]})

        local_types.extend(types)
        local_fns.extend(fns)

        # 静态解析 needs（伪 tree，仅含当前 file 的新节点）
        partial_tree = [{"kind": "file", **file}] + local_types + local_fns
        resolved = arch_resolve_needs(partial_tree)
        if not resolved.ok:
            errors.extend(resolved.errors)
        else:
            # 把 needs 解析产生的新 fn 追加到本轮收集
            for node in resolved.tree:
                if node.get("kind") == "function" and node["id"] not in local_ids:
                    local_fns.append(node)
                    file.setdefault("functions", []).append(node["id"])
                    local_ids.append(node["id"])

    return {
        "ok":        len(errors) == 0,
        "errors":    errors,
        "file":      file,
        "types":     local_types,
        "functions": local_fns,
        "local_ids": local_ids,
    }


async def arch_internal_fanout(
    files: list[dict],
    requirement: str,
    global_ids: list[str],
    modules: list[dict] | None = None,
) -> ArchInternalFanoutResult:
    config = load_config_from_file("configs/arch_internal_definer.json")
    files = copy.deepcopy(files)

    tasks = [
        _define_internals_for_file(f, list(global_ids), requirement, config)
        for f in files
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_types: list[dict] = []
    all_functions: list[dict] = []
    errors: list[str] = []
    updated_files: list[dict] = []
    updated_ids = list(global_ids)

    for result in results:
        if isinstance(result, Exception):
            errors.append(f"内部定义失败: {result}")
            continue
        errors.extend(result.get("errors", []))
        all_types.extend(result.get("types", []))
        all_functions.extend(result.get("functions", []))
        updated_files.append(result["file"])
        for nid in result.get("local_ids", []):
            if nid not in updated_ids:
                updated_ids.append(nid)

    tree = (
        [{"kind": "module", **m} for m in (modules or [])]
        + [{"kind": "file", **f} for f in updated_files]
        + [{"kind": "type", **t} for t in all_types]
        + [{"kind": "function", **fn} for fn in all_functions]
    )

    return ArchInternalFanoutResult(
        ok=len(errors) == 0,
        errors=errors,
        files=updated_files,
        types=all_types,
        functions=all_functions,
        global_ids=updated_ids,
        tree=tree,
    )
