"""arch_internal_fanout: 全量并发为每个 type/fn 调用 arch_internal_definer。

设计原则（重构版，替换原有 per-file 多轮迭代方案）：

两阶段全量并发：
  Phase 1：所有文件的所有 type:: 并发定义 → 建立 init_fn → overload 依赖关系图
  Phase 2：所有文件的所有 fn:: 并发定义
           - init_fn（is_init 函数）：无等待，立即并发执行
           - overload fn（类成员函数）：等待对应 type 的 init_fn 写入完成（asyncio.Event）
           - 独立 fn（非任何 type 的 overload）：无等待，立即并发执行

asyncio.Lock：所有共享状态（all_ids / all_types / all_fns / files_dict）的写操作均加锁，
  保证在引入 ThreadPoolExecutor 或其他真并行执行器时依然安全。

needs 解析：fn 返回的 needs 由下游图步骤 arch_resolve_needs 统一处理，
  internal_fanout 本身不负责 needs 解析，职责单一。
"""
from __future__ import annotations

import asyncio
import copy
import json

from llm_config import load_config_from_file, run as llm_run
from utils._base import ToolResult


class ArchInternalFanoutResult(ToolResult):
    ok: bool
    errors: list[str]
    files: list[dict]
    types: list[dict]
    functions: list[dict]
    global_ids: list[str]
    tree: list[dict]  # 完整项目树，供下游步骤直接使用


async def arch_internal_fanout(
    files: list[dict],
    requirement: str,
    global_ids: list[str],
    modules: list[dict] | None = None,
) -> ArchInternalFanoutResult:
    config = load_config_from_file("configs/arch_internal_definer.json")

    # ── 共享可变状态 ──────────────────────────────────────────────────────────
    files_dict: dict[str, dict] = {f["id"]: copy.deepcopy(f) for f in files}
    all_types: list[dict] = []
    all_fns:   list[dict] = []
    all_ids:   set[str]   = set(global_ids)
    all_errors: list[str] = []
    lock = asyncio.Lock()
    # ─────────────────────────────────────────────────────────────────────────

    # ── init_fn 依赖表（Phase 1 完成后填充）──────────────────────────────────
    # init_fn_id → Event：该 fn 是某 type 的 init_fn，写入完成后 set
    init_fn_events: dict[str, asyncio.Event] = {}
    # overload fn_id → Event：需等待对应 type 的 init_fn 写入完成后再执行
    overload_gate: dict[str, asyncio.Event] = {}
    # ─────────────────────────────────────────────────────────────────────────

    # ══ Phase 1：并发定义所有 type ════════════════════════════════════════════

    async def _define_type(file_id: str, type_id: str) -> None:
        async with lock:
            snapshot_ids = sorted(all_ids)
            file_snap = copy.deepcopy(files_dict[file_id])

        variables = {
            "requirement":     requirement,
            "file_json":       json.dumps(file_snap, ensure_ascii=False, indent=2),
            "global_ids_json": json.dumps(snapshot_ids, ensure_ascii=False),
            "target_id":       type_id,
        }

        try:
            result = await llm_run(config, variables)
        except Exception as e:
            async with lock:
                all_errors.append(f"{type_id} 定义失败: {e}")
            return

        if not isinstance(result, dict):
            return

        for t in result.get("types", []):
            if t.get("id") != type_id:
                continue
            t["kind"] = "type"

            init_fn_id: str | None = t.get("init_fn")
            overloads: list[dict]  = t.get("overloads", [])

            async with lock:
                if type_id not in all_ids:
                    all_types.append(t)
                    all_ids.add(type_id)
                    flist = files_dict[file_id].setdefault("types", [])
                    if type_id not in flist:
                        flist.append(type_id)

                # 建立 init_fn → overload fn 依赖关系
                if init_fn_id:
                    ev = asyncio.Event()
                    init_fn_events[init_fn_id] = ev
                    for overload in overloads:
                        fn_id = overload.get("fn")
                        if fn_id and fn_id != init_fn_id:
                            overload_gate[fn_id] = ev

            break  # 只取第一个 id 匹配的 type

    type_tasks = [
        _define_type(file_id, type_id)
        for file_id, f in files_dict.items()
        for type_id in list(f.get("types", []))
    ]
    await asyncio.gather(*type_tasks)

    # ══ Phase 2：并发定义所有 fn ══════════════════════════════════════════════

    async def _define_fn(file_id: str, fn_id: str) -> None:
        # overload fn：等待对应 type 的 init_fn 写入完成后再执行
        gate = overload_gate.get(fn_id)
        if gate:
            await gate.wait()

        async with lock:
            snapshot_ids = sorted(all_ids)
            file_snap = copy.deepcopy(files_dict[file_id])

        variables = {
            "requirement":     requirement,
            "file_json":       json.dumps(file_snap, ensure_ascii=False, indent=2),
            "global_ids_json": json.dumps(snapshot_ids, ensure_ascii=False),
            "target_id":       fn_id,
        }

        try:
            result = await llm_run(config, variables)
        except Exception as e:
            async with lock:
                all_errors.append(f"{fn_id} 定义失败: {e}")
            _maybe_set_init_event(fn_id, init_fn_events)
            return

        if not isinstance(result, dict):
            _maybe_set_init_event(fn_id, init_fn_events)
            return

        for fn in result.get("functions", []):
            if fn.get("id") != fn_id:
                continue
            fn["kind"] = "function"

            async with lock:
                if fn_id not in all_ids:
                    all_fns.append(fn)
                    all_ids.add(fn_id)
                    flist = files_dict[file_id].setdefault("functions", [])
                    if fn_id not in flist:
                        flist.append(fn_id)

            break  # 只取第一个 id 匹配的 fn

        # init_fn 写入完成 → 解锁等待的 overload fn 们
        _maybe_set_init_event(fn_id, init_fn_events)

    fn_tasks = [
        _define_fn(file_id, fn_id)
        for file_id, f in files_dict.items()
        for fn_id in list(f.get("functions", []))
    ]
    await asyncio.gather(*fn_tasks)

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


def _maybe_set_init_event(
    fn_id: str,
    init_fn_events: dict[str, asyncio.Event],
) -> None:
    """若此 fn 是某 type 的 init_fn，无论成功与否都 set Event，避免 overload fn 死锁。"""
    ev = init_fn_events.get(fn_id)
    if ev and not ev.is_set():
        ev.set()
