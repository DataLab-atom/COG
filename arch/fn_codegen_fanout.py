"""
arch_fn_codegen_fanout: 分两阶段并发为所有函数调用 arch_codegen agent 生成函数体。

阶段一：并发生成所有 is_init=true 的 __init__ 体
阶段二：并发生成其余函数体，类方法可读取所属类 __init__ 体（init_body）作为上下文

返回 {"fn_bodies": {fn_id: body_code}, "errors": [...]}
"""
from __future__ import annotations
import asyncio
import json
import httpx
from typing import Any

from llm_config import LLMConfig, load_config_from_file, run as llm_run
from utils._base import ToolResult


class ArchFnCodegenFanoutResult(ToolResult):
    ok: bool
    errors: list[str]
    fn_bodies: dict[str, str]


def _fn_name(fn_id: str) -> str:
    return fn_id.replace("fn::", "")


def _build_calls_context(fn: dict, fns_map: dict[str, dict]) -> list[dict]:
    result = []
    for called_id in fn.get("calls", []):
        called = fns_map.get(called_id, {})
        if called:
            result.append({
                "id":          called_id,
                "name":        _fn_name(called_id),
                "description": called.get("description", ""),
                "params":      called.get("params", []),
                "returns":     called.get("returns", {}),
                "is_async":    called.get("is_async", False),
            })
    return result


def _build_class_context(fn_id: str, types_map: dict, fn_to_type: dict) -> dict:
    tid = fn_to_type.get(fn_id)
    if not tid:
        return {}
    t = types_map.get(tid, {})
    return {
        "id":         tid,
        "base_class": t.get("base_class"),
        "constants":  t.get("constants", []),
        "fields":     t.get("fields", []),
    }


def _build_file_fns(fn_id: str, file: dict, fns_map: dict) -> list[dict]:
    result = []
    for fid in file.get("functions", []):
        if fid == fn_id:
            continue
        fn = fns_map.get(fid, {})
        if fn:
            result.append({
                "id":          fid,
                "name":        _fn_name(fid),
                "description": fn.get("description", ""),
                "params":      fn.get("params", []),
                "returns":     fn.get("returns", {}),
            })
    return result


async def _fetch_reference(ref: dict | None) -> str:
    if not ref:
        return ""
    source = ref.get("source", "")
    target = ref.get("target", "")
    note   = ref.get("note", "")
    if not source:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(source)
            content = resp.text[:3000]  # 截断避免超 token
    except Exception:
        content = f"[无法获取 {source}]"
    return f"source: {source}\ntarget: {target}\nnote: {note}\n\n{content}"


async def _codegen_one(
    fn: dict,
    stub: dict,
    files: dict[str, dict],
    types_map: dict[str, dict],
    fns_map: dict[str, dict],
    fn_to_type: dict[str, str],
    config: LLMConfig,
    init_body: str = "无",
) -> tuple[str, str]:
    """返回 (fn_id, body_code)"""
    fn_id = fn["id"]
    file  = files.get(stub["file_id"], {})

    calls_ctx = _build_calls_context(fn, fns_map)
    class_ctx = _build_class_context(fn_id, types_map, fn_to_type)
    file_fns  = _build_file_fns(fn_id, file, fns_map)
    ref_code  = await _fetch_reference(fn.get("reference"))

    variables = {
        "fn_id":               fn_id,
        "description":         fn.get("description", ""),
        "is_async":            str(fn.get("is_async", False)),
        "is_init":             str(stub.get("is_init", False)),
        "params_json":         json.dumps(fn.get("params", []),   ensure_ascii=False),
        "returns_json":        json.dumps(fn.get("returns", {}),  ensure_ascii=False),
        "calls_context_json":  json.dumps(calls_ctx,              ensure_ascii=False),
        "class_context_json":  json.dumps(class_ctx,              ensure_ascii=False),
        "file_fns_json":       json.dumps(file_fns,               ensure_ascii=False),
        "reference_code":      ref_code or "无",
        "init_body":           init_body,
    }

    result = await llm_run(config, variables)
    body = result.get("body", "pass") if isinstance(result, dict) else "pass"
    return fn_id, body


def _get_init_body(fn_id: str, fn_to_type: dict, types_map: dict, init_bodies: dict[str, str]) -> str:
    """查找 fn_id 所属类的 init_fn 已生成的 body，若无则返回 '无'。"""
    tid = fn_to_type.get(fn_id)
    if not tid:
        return "无"
    init_fn_id = types_map.get(tid, {}).get("init_fn")
    if not init_fn_id:
        return "无"
    return init_bodies.get(init_fn_id, "无")


async def arch_fn_codegen_fanout(
    tree: list[dict],
    fn_stubs: dict[str, dict],
) -> ArchFnCodegenFanoutResult:
    config    = load_config_from_file("configs/arch_codegen.json")
    files     = {n["id"]: n for n in tree if n.get("kind") == "file"}
    types_map = {n["id"]: n for n in tree if n.get("kind") == "type"}
    fns_map   = {n["id"]: n for n in tree if n.get("kind") == "function"}

    # fn → 所属 type（overloads + init_fn）
    fn_to_type: dict[str, str] = {}
    for tid, t in types_map.items():
        for ovl in t.get("overloads", []):
            fn_to_type[ovl["fn"]] = tid
        if t.get("init_fn"):
            fn_to_type[t["init_fn"]] = tid

    # 按 is_init 拆分两批
    init_stubs  = {fid: s for fid, s in fn_stubs.items() if s.get("is_init")}
    other_stubs = {fid: s for fid, s in fn_stubs.items() if not s.get("is_init")}

    fn_bodies: dict[str, str] = {}
    errors:    list[str]      = []

    # ── 阶段一：并发生成所有 __init__ 体 ─────────────────────────────────────
    if init_stubs:
        init_ids   = []
        init_tasks = []
        for fn_id, stub in init_stubs.items():
            fn = fns_map.get(fn_id)
            if not fn:
                continue
            init_ids.append(fn_id)
            init_tasks.append(_codegen_one(fn, stub, files, types_map, fns_map, fn_to_type, config))

        init_results = await asyncio.gather(*init_tasks, return_exceptions=True)
        for fn_id, result in zip(init_ids, init_results):
            if isinstance(result, Exception):
                errors.append(f"{fn_id} 生成失败: {result}")
                fn_bodies[fn_id] = "pass  # generation failed"
            else:
                _, body = result
                fn_bodies[fn_id] = body

    # ── 阶段二：并发生成其余函数体，类方法可读取 init_body ────────────────────
    if other_stubs:
        other_ids   = []
        other_tasks = []
        for fn_id, stub in other_stubs.items():
            fn = fns_map.get(fn_id)
            if not fn:
                continue
            init_body = _get_init_body(fn_id, fn_to_type, types_map, fn_bodies)
            other_ids.append(fn_id)
            other_tasks.append(
                _codegen_one(fn, stub, files, types_map, fns_map, fn_to_type, config, init_body)
            )

        other_results = await asyncio.gather(*other_tasks, return_exceptions=True)
        for fn_id, result in zip(other_ids, other_results):
            if isinstance(result, Exception):
                errors.append(f"{fn_id} 生成失败: {result}")
                fn_bodies[fn_id] = "pass  # generation failed"
            else:
                _, body = result
                fn_bodies[fn_id] = body

    return ArchFnCodegenFanoutResult(ok=len(errors) == 0, errors=errors, fn_bodies=fn_bodies)
