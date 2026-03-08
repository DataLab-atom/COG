"""
arch_backfill_files: 将 arch_file_splitter 的逐模块输出回填到模块节点，汇总全量文件列表。

graph 中 arch_file_splitter 以 map 形式对每个模块并发调用，产出 file_lists（每模块一组文件）。
本步骤将其合并：
  - 为每个文件设置 kind: "file"
  - 将 file id 列表写入对应 module["files"]
  - 汇总为扁平 files 列表供下游步骤使用

modules 与 file_lists 长度必须一致（顺序对齐），否则返回错误。

输入:
    modules:    list[dict]        — 模块列表（来自 arch_module_splitter）
    file_lists: list[list[dict]]  — 每个模块对应的文件列表（arch_file_splitter map 结果）

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
