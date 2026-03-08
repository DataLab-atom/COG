"""
arch_leaf_fanout: 并发为每个 file 调用 arch_leaf_definer agent。
内部使用 asyncio.gather 动态并发。
返回所有叶子节点（type + fn）定义，并将 id 回填到对应 file。
"""
from __future__ import annotations
import asyncio
import json
import copy
from typing import Any

from llm_config import LLMConfig, run as llm_run


async def _define_leaves_for_file(
    file: dict,
    global_ids: list[str],
    requirement: str,
    config: LLMConfig,
) -> dict[str, Any]:
    variables = {
        "requirement":    requirement,
        "file_json":      json.dumps(file, ensure_ascii=False, indent=2),
        "global_ids_json": json.dumps(global_ids, ensure_ascii=False, indent=2),
    }
    result = await llm_run(config, variables)
    return result if isinstance(result, dict) else {"types": [], "functions": []}


async def arch_leaf_fanout(
    files: list[dict],
    requirement: str,
    global_ids: list[str],
) -> dict[str, Any]:
    config = LLMConfig.from_file("configs/arch_leaf_definer.json")
    files = copy.deepcopy(files)

    tasks = [
        _define_leaves_for_file(f, global_ids, requirement, config)
        for f in files
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_types: list[dict] = []
    all_functions: list[dict] = []
    errors: list[str] = []
    updated_ids = list(global_ids)

    for file, result in zip(files, results):
        if isinstance(result, Exception):
            errors.append(f"{file['id']} 叶子定义失败: {result}")
            continue

        types = result.get("types", [])
        fns   = result.get("functions", [])

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

    return {
        "ok":        len(errors) == 0,
        "errors":    errors,
        "files":     files,
        "types":     all_types,
        "functions": all_functions,
        "global_ids": updated_ids,
    }
