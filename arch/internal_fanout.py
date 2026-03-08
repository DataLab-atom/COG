"""
arch_internal_fanout: 并发为每个 file 迭代调用 arch_internal_definer agent。

设计原则（对应设计文档 Step 4）：
- 文件间并发：asyncio.gather 并发处理所有文件
- 文件内串行：同一 file 的每轮 LLM 调用带历史，必须串行
- 共享可变状态：files_dict / all_types / all_fns / all_ids 在所有协程间共享
  （asyncio 单线程协作式调度，await 点之间的同步代码无竞争）
- 全量 needs 解析：每轮 LLM 返回后，用全量树（所有文件）调用 arch_resolve_needs
  修复原有 partial_tree 导致跨文件 needs 无法定位目标 file 的根本 bug
"""
from __future__ import annotations
import asyncio
import copy
import json
from typing import Any

from llm_config import load_config_from_file, run as llm_run
from utils._base import ToolResult
from arch.resolve_needs import arch_resolve_needs


class ArchInternalFanoutResult(ToolResult):
    ok: bool
    errors: list[str]
    files: list[dict]
    types: list[dict]
    functions: list[dict]
    global_ids: list[str]
    tree: list[dict]  # 完整项目树，供下游步骤直接使用


_MAX_ROUNDS = 10  # 单文件最大迭代轮次


async def arch_internal_fanout(
    files: list[dict],
    requirement: str,
    global_ids: list[str],
    modules: list[dict] | None = None,
) -> ArchInternalFanoutResult:
    config = load_config_from_file("configs/arch_internal_definer.json")

    # ── 共享可变状态（asyncio 单线程，await 点之间无竞争）──────────────────
    files_dict: dict[str, dict] = {f["id"]: copy.deepcopy(f) for f in files}
    all_types: list[dict] = []
    all_fns: list[dict] = []
    all_ids: set[str] = set(global_ids)
    # ─────────────────────────────────────────────────────────────────────────

    async def _process_file(file_id: str) -> dict[str, Any]:
        """单文件多轮迭代；直接读写外部共享状态。"""
        file = files_dict[file_id]
        history: list[dict] = []
        history_defined: list[dict] = []
        errors: list[str] = []

        for round_idx in range(_MAX_ROUNDS):
            depth = min(round_idx + 1, 3)

            variables = {
                "requirement":          requirement,
                "file_json":            json.dumps(file, ensure_ascii=False, indent=2),
                # 使用共享 all_ids：LLM 能看到其他文件本轮已定义的 id
                "global_ids_json":      json.dumps(sorted(all_ids), ensure_ascii=False),
                "depth":                str(depth),
                "history_defined_json": json.dumps(history_defined, ensure_ascii=False),
            }

            try:
                result = await llm_run(config, variables, history=history)
            except Exception as e:
                errors.append(f"{file_id} 第 {round_idx + 1} 轮失败: {e}")
                break

            if not isinstance(result, dict):
                break

            history.append({"role": "assistant", "content": json.dumps(result, ensure_ascii=False)})

            new_types: list[dict] = result.get("types", [])
            new_fns:   list[dict] = result.get("functions", [])

            if not new_types and not new_fns:
                break  # 该文件所有节点已定义完毕

            # ── 同步更新共享状态（此处无 await，运行原子性）──────────────
            for t in new_types:
                t["kind"] = "type"
                nid = t["id"]
                if nid not in all_ids:
                    file.setdefault("types", []).append(nid)
                    all_types.append(t)
                    all_ids.add(nid)
                    history_defined.append({"kind": "type", "id": nid})

            for fn in new_fns:
                fn["kind"] = "function"
                nid = fn["id"]
                if nid not in all_ids:
                    file.setdefault("functions", []).append(nid)
                    all_fns.append(fn)
                    all_ids.add(nid)
                    history_defined.append({"kind": "function", "id": nid})

            # ── 全量 needs 解析（跨文件核心修复）────────────────────────────
            # 传入所有文件（files_dict.values()），而非仅当前文件的局部树
            # arch_resolve_needs 内部 deepcopy，不污染共享状态；结果回写见下方
            snapshot = (
                [{"kind": "file", **f} for f in files_dict.values()]
                + list(all_types)
                + list(all_fns)
            )
            resolved = arch_resolve_needs(snapshot)

            if not resolved.ok:
                errors.extend(resolved.errors)
            else:
                _apply_resolved(resolved.tree, all_fns, all_ids, files_dict)

        return {"ok": len(errors) == 0, "errors": errors}

    # ── 并发处理所有文件 ─────────────────────────────────────────────────────
    tasks = [_process_file(fid) for fid in files_dict]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_errors: list[str] = []
    for r in results:
        if isinstance(r, Exception):
            all_errors.append(f"文件处理失败: {r}")
        else:
            all_errors.extend(r.get("errors", []))

    updated_files = list(files_dict.values())
    tree = (
        [{"kind": "module", **m} for m in (modules or [])]
        + [{"kind": "file",     **f} for f in updated_files]
        + [{"kind": "type",     **t} for t in all_types]
        + [{"kind": "function", **fn} for fn in all_fns]
    )

    return ArchInternalFanoutResult(
        ok=len(all_errors) == 0,
        errors=all_errors,
        files=updated_files,
        types=all_types,
        functions=all_fns,
        global_ids=sorted(all_ids),
        tree=tree,
    )


def _apply_resolved(
    resolved_tree: list[dict],
    all_fns: list[dict],
    all_ids: set[str],
    files_dict: dict[str, dict],
) -> None:
    """将 arch_resolve_needs 产生的变化回写到共享状态（同步，无 await）。

    变化类型：
    1. 已有 fn 的 needs 被清空、calls 被回填 → 原地替换
    2. needs 解析新生成的 stub fn → 插入 all_fns + 更新目标 file.functions
    """
    res_fn_map   = {n["id"]: n for n in resolved_tree if n.get("kind") == "function"}
    res_file_map = {n["id"]: n for n in resolved_tree if n.get("kind") == "file"}

    # 1. 原地替换已有 fn（needs 清空，calls 回填）
    for i in range(len(all_fns)):
        fid = all_fns[i]["id"]
        if fid in res_fn_map:
            all_fns[i] = res_fn_map[fid]

    # 2. 插入 stub fn（needs 解析新生成的）
    existing_ids = {fn["id"] for fn in all_fns}
    for fid, rfn in res_fn_map.items():
        if fid not in existing_ids:
            all_fns.append(rfn)
            all_ids.add(fid)
            existing_ids.add(fid)

    # 3. 同步目标 file 的 functions 列表（解析可能把 stub 注入到非当前 file）
    for file_id, rfile in res_file_map.items():
        if file_id not in files_dict:
            continue
        current_fns = set(files_dict[file_id].get("functions", []))
        for fn_id in rfile.get("functions", []):
            if fn_id not in current_fns:
                files_dict[file_id].setdefault("functions", []).append(fn_id)
                current_fns.add(fn_id)
