"""
arch_fn_codegen_fanout: 并发为所有函数调用 arch_codegen agent 生成函数体。
- 收集每个函数需要的上下文：calls 签名、所属类字段、同文件函数、reference 代码
- asyncio.gather 并发调用
- 返回 {"fn_bodies": {fn_id: body_code}, "errors": [...]}
"""
from __future__ import annotations
import asyncio
import json
import httpx
from typing import Any

from llm_config import LLMConfig, run as llm_run


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
        "params_json":         json.dumps(fn.get("params", []),   ensure_ascii=False),
        "returns_json":        json.dumps(fn.get("returns", {}),  ensure_ascii=False),
        "calls_context_json":  json.dumps(calls_ctx,              ensure_ascii=False),
        "class_context_json":  json.dumps(class_ctx,              ensure_ascii=False),
        "file_fns_json":       json.dumps(file_fns,               ensure_ascii=False),
        "reference_code":      ref_code or "无",
    }

    result = await llm_run(config, variables)
    body = result.get("body", "pass") if isinstance(result, dict) else "pass"
    return fn_id, body


async def arch_fn_codegen_fanout(
    tree: list[dict],
    fn_stubs: dict[str, dict],
) -> dict[str, Any]:
    config    = LLMConfig.from_file("configs/arch_codegen.json")
    files     = {n["id"]: n for n in tree if n.get("kind") == "file"}
    types_map = {n["id"]: n for n in tree if n.get("kind") == "type"}
    fns_map   = {n["id"]: n for n in tree if n.get("kind") == "function"}

    # fn → 所属 type
    fn_to_type: dict[str, str] = {}
    for tid, t in types_map.items():
        for ovl in t.get("overloads", []):
            fn_to_type[ovl["fn"]] = tid

    tasks = []
    fn_ids = []
    for fn_id, stub in fn_stubs.items():
        fn = fns_map.get(fn_id)
        if not fn:
            continue
        fn_ids.append(fn_id)
        tasks.append(_codegen_one(fn, stub, files, types_map, fns_map, fn_to_type, config))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    fn_bodies: dict[str, str] = {}
    errors: list[str] = []
    for fn_id, result in zip(fn_ids, results):
        if isinstance(result, Exception):
            errors.append(f"{fn_id} 生成失败: {result}")
            fn_bodies[fn_id] = "pass  # generation failed"
        else:
            _, body = result
            fn_bodies[fn_id] = body

    return {"ok": len(errors) == 0, "errors": errors, "fn_bodies": fn_bodies}
