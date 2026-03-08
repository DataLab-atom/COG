"""
arch_backfill_leaves: 将 map 步骤的逐文件叶子结果回填到文件，汇总为全量节点列表。

对每个文件的 {types, functions} 结果：
    - 设置 kind: "type" / "function"
    - 回填 file["types"] = [type_id, ...], file["functions"] = [fn_id, ...]
    - 汇总为扁平 types / functions 列表
    - 累积 global_ids

输入:
    files:        list[dict]  — 文件列表（来自 file_fanout.files）
    leaf_results: list[dict]  — 每个文件对应的叶子结果（map 步骤原始结果，list[dict]）
    global_ids:   list[str]   — 已有全局 id 列表（可选，默认 []）

输出:
    ArchBackfillLeavesResult(ok, errors, files, types, functions, global_ids)
"""
from __future__ import annotations

import copy

from utils._base import ToolResult


class ArchBackfillLeavesResult(ToolResult):
    ok: bool
    errors: list[str]
    files: list[dict]
    types: list[dict]
    functions: list[dict]
    global_ids: list[str]


def arch_backfill_leaves(
    files: list[dict],
    leaf_results: list,
    global_ids: list | None = None,
) -> ArchBackfillLeavesResult:
    files = copy.deepcopy(files)
    updated_ids: list[str] = list(global_ids or [])
    all_types: list[dict] = []
    all_functions: list[dict] = []
    errors: list[str] = []

    if len(files) != len(leaf_results):
        errors.append(
            f"files 长度 ({len(files)}) 与 leaf_results 长度 ({len(leaf_results)}) 不一致"
        )
        return ArchBackfillLeavesResult(
            ok=False, errors=errors,
            files=files, types=[], functions=[], global_ids=updated_ids,
        )

    for file, result in zip(files, leaf_results):
        if not isinstance(result, dict):
            errors.append(f"{file.get('id', '?')} 叶子结果不是 dict，跳过")
            continue

        types = result.get("types", [])
        fns = result.get("functions", [])

        for t in types:
            t["kind"] = "type"
            file.setdefault("types", []).append(t["id"])
            updated_ids.append(t["id"])
        for fn in fns:
            fn["kind"] = "function"
            file.setdefault("functions", []).append(fn["id"])
            updated_ids.append(fn["id"])

        all_types.extend(types)
        all_functions.extend(fns)

    return ArchBackfillLeavesResult(
        ok=len(errors) == 0,
        errors=errors,
        files=files,
        types=all_types,
        functions=all_functions,
        global_ids=updated_ids,
    )
