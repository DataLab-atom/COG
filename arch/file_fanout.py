"""
arch_file_fanout: 并发为每个 module 调用 arch_file_splitter agent。
内部使用 asyncio.gather 动态并发，绕过 graph 静态步骤的限制。
返回所有 file 定义列表，并将 file id 回填到对应 module.files。
"""
from __future__ import annotations
import asyncio
import json
import copy
from typing import Any

from llm_config import LLMConfig, load_config_from_file, run as llm_run
from utils._base import ToolResult


class ArchFileFanoutResult(ToolResult):
    ok: bool
    errors: list[str]
    modules: list[dict]
    files: list[dict]


async def _split_one_module(
    module: dict,
    all_modules: list[dict],
    requirement: str,
    config: LLMConfig,
) -> list[dict]:
    variables = {
        "requirement":    requirement,
        "module_json":    json.dumps(module, ensure_ascii=False, indent=2),
        "all_modules_json": json.dumps(all_modules, ensure_ascii=False, indent=2),
    }
    result = await llm_run(config, variables)
    files: list[dict] = result if isinstance(result, list) else []
    for f in files:
        f["kind"] = "file"
    return files


async def arch_file_fanout(
    modules: list[dict],
    requirement: str,
) -> ArchFileFanoutResult:
    config = load_config_from_file("configs/arch_file_splitter.json")
    modules = copy.deepcopy(modules)

    tasks = [
        _split_one_module(m, modules, requirement, config)
        for m in modules
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_files: list[dict] = []
    errors: list[str] = []

    for module, result in zip(modules, results):
        if isinstance(result, Exception):
            errors.append(f"{module['id']} 文件划分失败: {result}")
            continue
        file_ids = [f["id"] for f in result]
        module["files"] = file_ids
        all_files.extend(result)

    return ArchFileFanoutResult(
        ok=len(errors) == 0,
        errors=errors,
        modules=modules,
        files=all_files,
    )
