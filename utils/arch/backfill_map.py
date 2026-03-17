"""
arch_backfill_map — map 步骤结果回填工具集

包含两个互补的 map 结果合并操作，均用于将并发 map 步骤的输出回填到父节点列表。

arch_backfill_files:
    将 arch_file_splitter map 输出（每模块一组文件）回填到模块，
    设置 kind="file"，汇总为全量文件列表。

arch_backfill_leaves:
    将 arch_internal_fanout map 输出（每文件的 types/functions）回填到文件，
    设置 kind="type"/"function"，检测 ID 冲突，汇总为全量叶子节点列表。
"""
from __future__ import annotations

import copy

from utils import ToolResult


# ── arch_backfill_files ────────────────────────────────────────────────────────

class ArchBackfillFilesResult(ToolResult):
    ok: bool
    errors: list[str]
    modules: list[dict]
    files: list[dict]


def arch_backfill_files(
    modules: list[dict],
    file_lists: list,
) -> ArchBackfillFilesResult:
    """将 arch_file_splitter 的逐模块输出回填到模块节点，汇总全量文件列表。

    modules 与 file_lists 长度必须一致（顺序对齐），否则返回错误。
    """
    modules = copy.deepcopy(modules)
    all_files: list[dict] = []
    errors: list[str] = []

    if len(modules) != len(file_lists):
        errors.append(
            f"modules 长度 ({len(modules)}) 与 file_lists 长度 ({len(file_lists)}) 不一致"
        )
        return ArchBackfillFilesResult(ok=False, errors=errors, modules=modules, files=[])

    for module, file_list in zip(modules, file_lists):
        if isinstance(file_list, dict) and "result" in file_list:
            file_list = file_list["result"]
        if not isinstance(file_list, list):
            errors.append(f"{module.get('id', '?')} 的文件列表不是 list，跳过")
            continue
        file_ids: list[str] = []
        for f in file_list:
            if not isinstance(f, dict) or "id" not in f:
                errors.append(f"{module.get('id', '?')} 有非法文件条目（缺 id），跳过")
                continue
            f["kind"] = "file"
            file_ids.append(f["id"])
            all_files.append(f)
        module["files"] = file_ids

    return ArchBackfillFilesResult(
        ok=len(errors) == 0,
        errors=errors,
        modules=modules,
        files=all_files,
    )


# ── arch_backfill_leaves ───────────────────────────────────────────────────────

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
    """将 arch_internal_fanout 的逐文件叶子结果回填到文件，汇总全量节点列表。

    files 与 leaf_results 长度必须一致（顺序对齐），否则返回错误。
    检测 ID 冲突：同一 id 出现在多个文件结果中将记录错误。
    """
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
            if not isinstance(t, dict) or "id" not in t:
                errors.append(f"{file.get('id', '?')} 有非法 type 条目（缺 id），跳过")
                continue
            t["kind"] = "type"
            nid = t["id"]
            if nid in seen:
                errors.append(f"ID 冲突: '{nid}' (file={file.get('id', '?')}, kind=type)")
            else:
                seen.add(nid)
            file.setdefault("types", []).append(nid)
            updated_ids.append(nid)
        for fn in fns:
            if not isinstance(fn, dict) or "id" not in fn:
                errors.append(f"{file.get('id', '?')} 有非法 function 条目（缺 id），跳过")
                continue
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
