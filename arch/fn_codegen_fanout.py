"""
arch_fn_codegen_fanout: 全量并发为所有函数生成函数体，基于调用图依赖排序。

设计原则：
- 全量并发：所有函数在同一 asyncio.gather 中启动，每个 fn 一个 asyncio.Event
- Step 1（calls 依赖）：fn A calls fn B → A 等待 B 的 Event，
  确保 A 生成时可将 B 的实际实现作为上下文（calls_bodies_json）
- Step 2（类方法依赖）：类方法（非 init）等待同类 init_fn 的 Event，
  即使方法不直接 call init_fn 也需等待（init_body 作为上下文）
- 死锁防护：fn.calls 已由 arch_validate_dag 保证 DAG（Step 1 安全）；
  Step 2 若 init_fn.calls 包含本 fn（init 主动调用了该方法），则跳过等待，
  避免 init_fn 等本 fn（Step 1）与本 fn 等 init_fn（Step 2）形成互相死锁
- 失败容错：任一 fn 生成失败时 fallback 为 "pass  # generation failed"，
  finally 块强制 set Event，确保依赖该 fn 的 caller 不被阻塞

返回 ArchFnCodegenFanoutResult(ok, errors, fn_bodies)
  - fn_bodies：{fn_id: body_code}（body 不含 def 行，由 arch_assemble 填入骨架）
"""
from __future__ import annotations
import asyncio
import json
import httpx

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
    calls_bodies: dict[str, str] | None = None,
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
        "calls_bodies_json":   json.dumps(calls_bodies or {},     ensure_ascii=False),
        "class_context_json":  json.dumps(class_ctx,              ensure_ascii=False),
        "file_fns_json":       json.dumps(file_fns,               ensure_ascii=False),
        "reference_code":      ref_code or "无",
        "init_body":           init_body,
    }

    result = await llm_run(config, variables)
    body = result.get("body", "pass") if isinstance(result, dict) else "pass"
    return fn_id, body


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

    fn_bodies: dict[str, str] = {}
    errors:    list[str]      = []

    # ── 每个 fn 一个 Event：body 写入后 set，下游 caller 解除等待 ──────────────
    fn_events: dict[str, asyncio.Event] = {fid: asyncio.Event() for fid in fn_stubs}

    async def _generate(fn_id: str, stub: dict) -> None:
        fn = fns_map.get(fn_id)
        if not fn:
            fn_bodies[fn_id] = "pass"
            fn_events[fn_id].set()
            return

        # 1. 等待所有被调函数（calls）的 body 就绪
        for called_id in fn.get("calls", []):
            ev = fn_events.get(called_id)
            if ev:
                await ev.wait()

        # 2. 若为类方法（非 init），等待同类 init_fn 的 body 就绪
        #    例外：若 init_fn.calls 包含本 fn（init 调用了该方法），
        #    step 1 已让 init_fn 等待本 fn，此处反向等待会死锁，跳过。
        if not stub.get("is_init"):
            tid = fn_to_type.get(fn_id)
            if tid:
                init_fn_id = types_map.get(tid, {}).get("init_fn")
                if init_fn_id:
                    init_fn_data = fns_map.get(init_fn_id, {})
                    if fn_id not in init_fn_data.get("calls", []):
                        ev = fn_events.get(init_fn_id)
                        if ev:
                            await ev.wait()

        # 3. 收集已就绪的 callee bodies 作为上下文
        calls_bodies = {
            called_id: fn_bodies[called_id]
            for called_id in fn.get("calls", [])
            if called_id in fn_bodies
        }

        # 4. 获取 init_body（类方法上下文）
        #    若 init_fn 调用了本 fn（step 2 跳过了等待），init_fn body 可能尚未就绪，
        #    此时 fn_bodies.get 返回 "无"，不影响正确性。
        tid = fn_to_type.get(fn_id)
        init_body = "无"
        if tid and not stub.get("is_init"):
            init_fn_id = types_map.get(tid, {}).get("init_fn")
            if init_fn_id:
                init_body = fn_bodies.get(init_fn_id, "无")

        # 5. 调用 LLM 生成函数体
        try:
            _, body = await _codegen_one(
                fn, stub, files, types_map, fns_map, fn_to_type, config,
                init_body=init_body,
                calls_bodies=calls_bodies,
            )
            fn_bodies[fn_id] = body
        except Exception as e:
            errors.append(f"{fn_id} 生成失败: {e}")
            fn_bodies[fn_id] = "pass  # generation failed"
        finally:
            fn_events[fn_id].set()  # 无论成败，解除等待该 fn 的 caller

    await asyncio.gather(*[_generate(fid, stub) for fid, stub in fn_stubs.items()])

    return ArchFnCodegenFanoutResult(ok=len(errors) == 0, errors=errors, fn_bodies=fn_bodies)
