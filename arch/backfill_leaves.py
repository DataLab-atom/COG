"""
arch_backfill_leaves: 将 arch_internal_fanout 的逐文件叶子结果回填到文件，汇总全量节点列表。

arch_internal_fanout 两阶段并发完成后产出每个文件的 {types, functions}（leaf_results）。
本步骤将其合并：
  - 为 types/functions 设置 kind: "type" / "function"
  - 将 type_id / fn_id 列表写入对应 file["types"] / file["functions"]
  - 检测 ID 冲突（同一 id 出现在多个文件结果中 → 记录错误）
  - 汇总为扁平 types / functions 列表及累积 global_ids

files 与 leaf_results 长度必须一致（顺序对齐），否则返回错误。

输入:
    files:        list[dict]       — 文件列表
    leaf_results: list[dict]       — 每个文件对应的叶子结果（{types, functions}）
    global_ids:   list[str] | None — 已有全局 id 列表（可选，默认 []）

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

    seen: set[str] = set(updated_ids)

    for file, result in zip(files, leaf_results):
        if not isinstance(result, dict):
            errors.append(f"{file.get('id', '?')} 叶子结果不是 dict，跳过")
            continue

        types = result.get("types", [])
        fns = result.get("functions", [])

        for t in types:
            t["kind"] = "type"
            nid = t["id"]
            if nid in seen:
                errors.append(f"ID 冲突: '{nid}' (file={file.get('id', '?')}, kind=type)")
            else:
                seen.add(nid)
            file.setdefault("types", []).append(nid)
            updated_ids.append(nid)
        for fn in fns:
            fn["kind"] = "function"
            nid = fn["id"]
            if nid in seen:
                errors.append(f"ID 冲突: '{nid}' (file={file.get('id', '?')}, kind=function)")
            else:
                seen.add(nid)
            file.setdefault("functions", []).append(nid)
            updated_ids.append(nid)

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
