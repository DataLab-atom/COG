"""
arch_backfill_files: 将 map 步骤的逐模块文件列表回填到模块，汇总为全量文件列表。

对每个模块的文件列表：
    - 设置 kind: "file"
    - 回填 module["files"] = [file_id, ...]
    - 汇总为扁平 files 列表

输入:
    modules:    list[dict]        — 模块列表（来自 module_split.result）
    file_lists: list[list[dict]]  — 每个模块对应的文件列表（map 步骤原始结果）

输出:
    ArchBackfillFilesResult(ok, errors, modules, files)
"""
from __future__ import annotations

import copy

from utils._base import ToolResult


class ArchBackfillFilesResult(ToolResult):
    ok: bool
    errors: list[str]
    modules: list[dict]
    files: list[dict]


def arch_backfill_files(
    modules: list[dict],
    file_lists: list,
) -> ArchBackfillFilesResult:
    modules = copy.deepcopy(modules)
    all_files: list[dict] = []
    errors: list[str] = []

    if len(modules) != len(file_lists):
        errors.append(
            f"modules 长度 ({len(modules)}) 与 file_lists 长度 ({len(file_lists)}) 不一致"
        )
        return ArchBackfillFilesResult(ok=False, errors=errors, modules=modules, files=[])

    for module, file_list in zip(modules, file_lists):
        if not isinstance(file_list, list):
            errors.append(f"{module.get('id', '?')} 的文件列表不是 list，跳过")
            continue
        file_ids: list[str] = []
        for f in file_list:
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
