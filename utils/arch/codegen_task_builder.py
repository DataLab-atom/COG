"""
arch/codegen_task_builder.py — fn_codegen map 步骤辅助工具

提供四个工具函数，配合 map 步骤的 item_key + item_depends_on 机制并发生成
所有函数体，取代原 arch_fn_codegen_fanout 异步输入函数。

函数：
    arch_build_codegen_tasks      — 将 fn_stubs dict 转为任务列表，预计算静态上下文
    arch_prepare_codegen_vars     — 从任务和运行时依赖输出中提取 arch_codegen 所需变量
    arch_collect_fn_bodies        — 将 map keyed 输出聚合为 fn_bodies / file_imports 映射
    arch_enrich_tree_with_bodies  — 将 fn_bodies 写入 fn:: 节点的 code 字段，返回富化后的树
"""
from __future__ import annotations

import ast
import logging
import os
import re
import textwrap
from typing import Any

from utils import ToolResult
from utils.arch._common import _fn_name, _build_tree_maps, _build_fn_to_type_map

logger = logging.getLogger(__name__)


class ArchBuildCodegenTasksResult(ToolResult):
    tasks: list[dict]
    existing_bodies: dict[str, str]  # {fn_id: body} 已有 code 字段的函数，跳过 LLM 直接复用


class ArchPrepareCodegenVarsResult(ToolResult):
    fn_id:              str
    description:        str
    is_async:           str
    is_init:            str
    params_json:        list
    returns_json:       dict
    calls_context_json: list
    calls_bodies_json:  dict
    class_context_json: dict
    file_fns_json:      list
    reference_code:     str
    init_body:          str
    data_files_json:    list  # [{path, size_mb, ext}] 项目中可用的数据文件


class ArchCollectFnBodiesResult(ToolResult):
    fn_bodies:    dict[str, str]
    file_imports: dict[str, list[str]]


class ArchEnrichTreeResult(ToolResult):
    tree: list


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

def _extract_body_from_code(code: str) -> str:
    """从完整函数代码（含 def 行及可选装饰器）中提取函数体。

    支持多行函数签名：通过括号深度追踪找到签名结束位置（`):` 所在行），
    再取其后内容作为函数体。

    输入示例（textwrap.dedent 后）：
        def my_func(x: int) -> str:
            result = str(x)
            return result

    多行签名示例：
        def __init__(
                self, Z, Phi,
                max_iter,
        ):
            self.Z = Z

    输出（去掉 def 行 / 多行签名，再 dedent 一层）：
        result = str(x)
        return result
    """
    lines = code.split("\n")
    body_start = 0
    found_def = False
    paren_depth = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not found_def:
            # 跳过装饰器行
            if stripped.startswith("@"):
                continue
            if stripped.startswith("def ") or stripped.startswith("async def "):
                found_def = True
                paren_depth += line.count("(") - line.count(")")
                # 单行签名：括号已闭合且行尾为冒号
                if paren_depth <= 0 and stripped.rstrip().endswith(":"):
                    body_start = i + 1
                    break
        else:
            # 多行签名续行：追踪括号深度
            paren_depth += line.count("(") - line.count(")")
            if paren_depth <= 0 and stripped.rstrip().endswith(":"):
                body_start = i + 1
                break
    body = "\n".join(lines[body_start:])
    return textwrap.dedent(body).strip()

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


def _build_class_context(
    fn_id: str,
    types_map: dict[str, dict],
    fn_to_type: dict[str, str],
) -> dict:
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


def _build_file_fns(fn_id: str, file: dict, fns_map: dict[str, dict]) -> list[dict]:
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


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def arch_build_codegen_tasks(
    tree: list[dict],
    fn_stubs: dict,
    metric_name: str = "",
) -> ArchBuildCodegenTasksResult:
    """将 fn_stubs dict 和 tree 转换为 arch_codegen map 所需的任务列表。

    每个任务包含调用 arch_codegen agent 所需的所有静态上下文，
    以及依赖列表（calls 依赖 + init_fn 依赖），供 map 步骤的 item_depends_on 使用。

    Args:
        tree:        完整架构树（来自 arch_internal_fanout / backfill 等步骤）。
        fn_stubs:    scaffold 返回的 fn_stubs 映射 {fn_id: stub_dict}。
        metric_name: 评估指标名，透传给 arch_codegen。
    """
    _, files, types_map, fns_map = _build_tree_maps(tree)

    fn_to_type = _build_fn_to_type_map(types_map)

    tasks: list[dict] = []
    existing_bodies: dict[str, str] = {}

    for fn_id, stub in fn_stubs.items():
        fn      = fns_map.get(fn_id, {})
        file_id = stub.get("file_id", "")
        file    = files.get(file_id, {})

        # 若 fn 节点已有 code 字段（来自 arch_from_existing），直接提取 body，跳过 LLM 生成
        if fn.get("code"):
            existing_bodies[fn_id] = _extract_body_from_code(fn["code"])
            continue

        calls_list: list[str] = fn.get("calls", [])

        # init_fn 依赖（类方法需等待 init_fn 完成后才能获取 init_body 上下文）
        init_fn_id: str | None = None
        if not stub.get("is_init"):
            tid = fn_to_type.get(fn_id)
            if tid:
                candidate = types_map.get(tid, {}).get("init_fn")
                if candidate:
                    # 若 init_fn 反向 call 了本 fn，跳过（避免循环等待）
                    init_fn_data = fns_map.get(candidate, {})
                    if fn_id not in init_fn_data.get("calls", []):
                        init_fn_id = candidate

        # 合并所有依赖：calls 依赖 + init_fn 依赖
        # 只将需要 LLM 生成的函数（不在 existing_bodies 中）列为 map 依赖，
        # 对已有代码的函数直接提取 body 作为静态上下文
        new_gen_calls = [c for c in calls_list if not fns_map.get(c, {}).get("code")]
        all_deps = list(new_gen_calls)
        if init_fn_id and init_fn_id not in all_deps:
            if not fns_map.get(init_fn_id, {}).get("code"):
                all_deps.append(init_fn_id)

        # 预提取已有代码的被调用函数体（existing_bodies 里的函数不会进 map，
        # 但 arch_prepare_codegen_vars 需要它们的 body 作为 calls_bodies_json）
        existing_calls_bodies: dict[str, str] = {}
        for called_id in calls_list:
            called_fn = fns_map.get(called_id, {})
            if called_fn.get("code"):
                existing_calls_bodies[called_id] = _extract_body_from_code(called_fn["code"])

        tasks.append({
            "fn_id":         fn_id,
            "file_id":       file_id,
            "calls_list":    calls_list,
            "init_fn_id":    init_fn_id,
            "all_deps":      all_deps,
            "is_init":       stub.get("is_init", False),
            "metric_name":   metric_name,
            # 静态上下文（calls_bodies / init_body 在运行时从 _deps 获取）
            "fn_description":       fn.get("description", ""),
            "fn_is_async":          fn.get("is_async", False),
            "fn_params":            fn.get("params", []),
            "fn_returns":           fn.get("returns", {}),
            "calls_context":        _build_calls_context(fn, fns_map),
            "class_context":        _build_class_context(fn_id, types_map, fn_to_type),
            "file_fns":             _build_file_fns(fn_id, file, fns_map),
            # 已有代码的被调用函数体（不经过 map，直接作为静态上下文）
            "existing_calls_bodies": existing_calls_bodies,
        })

    return ArchBuildCodegenTasksResult(tasks=tasks, existing_bodies=existing_bodies)


def arch_prepare_codegen_vars(
    task: dict,
    deps: dict,
    data_files: list | None = None,
) -> ArchPrepareCodegenVarsResult:
    """从任务定义和运行时依赖输出中提取 arch_codegen agent 所需的所有变量。

    Args:
        task: arch_build_codegen_tasks 产出的单个任务 dict。
        deps: {fn_id: {body: str, imports: [...]}} 依赖项输出（来自 map _deps）。
    """
    calls_list = task.get("calls_list", [])
    init_fn_id = task.get("init_fn_id")

    # 从 deps 提取 calls bodies（{fn_id: body_str}）
    # 优先使用 map deps（本次 LLM 生成），再合并静态的已有函数体
    existing_calls_bodies: dict[str, str] = task.get("existing_calls_bodies", {})
    calls_bodies: dict[str, str] = dict(existing_calls_bodies)  # 先放已有实现
    for dep_id in calls_list:
        if dep_id in deps:
            body = (deps.get(dep_id) or {}).get("body", "")
            if body:
                calls_bodies[dep_id] = body  # LLM 新生成的覆盖已有（如重新生成场景）

    # 从 deps 提取 init_body（类方法上下文）
    init_body = "无"
    if init_fn_id:
        if init_fn_id in deps:
            init_body = (deps[init_fn_id] or {}).get("body", "无") or "无"
        elif init_fn_id in existing_calls_bodies:
            init_body = existing_calls_bodies[init_fn_id] or "无"

    return ArchPrepareCodegenVarsResult(
        fn_id              = task["fn_id"],
        description        = task.get("fn_description", ""),
        is_async           = str(task.get("fn_is_async", False)),
        is_init            = str(task.get("is_init", False)),
        params_json        = task.get("fn_params", []),
        returns_json       = task.get("fn_returns", {}),
        calls_context_json = task.get("calls_context", []),
        calls_bodies_json  = calls_bodies,
        class_context_json = task.get("class_context", {}),
        file_fns_json      = task.get("file_fns", []),
        reference_code     = "无",
        init_body          = init_body,
        data_files_json    = data_files or [],
    )


def arch_collect_fn_bodies(
    tasks: list[dict],
    keyed_results: dict,
    existing_bodies: dict[str, str] | None = None,
) -> ArchCollectFnBodiesResult:
    """将 map collect:keyed 输出聚合为 arch_assemble 所需的 fn_bodies / file_imports。

    Args:
        tasks:           arch_build_codegen_tasks 产出的任务列表（含 fn_id 和 file_id）。
        keyed_results:   map collect:keyed 输出 {fn_id: {body, imports}}。
        existing_bodies: arch_build_codegen_tasks 产出的已有函数体 {fn_id: body}，
                         直接合并进 fn_bodies，不经 LLM。
    """
    fn_bodies:    dict[str, str]       = {}
    file_imports: dict[str, list[str]] = {}

    # 优先填入已有函数体（来自 arch_from_existing，无需 LLM 生成）
    if existing_bodies:
        fn_bodies.update(existing_bodies)

    for task in tasks:
        fn_id   = task["fn_id"]
        file_id = task.get("file_id", "")
        result  = keyed_results.get(fn_id) or {}

        fn_bodies[fn_id] = result.get("body") or "pass  # generation failed"

        for stmt in (result.get("imports") or []):
            if stmt:
                if file_id not in file_imports:
                    file_imports[file_id] = []
                if stmt not in file_imports[file_id]:
                    file_imports[file_id].append(stmt)

    return ArchCollectFnBodiesResult(fn_bodies=fn_bodies, file_imports=file_imports)


def _python_type_local(t: str) -> str:
    """将 type:: 引用转换为 Python 类名，支持复合类型如 list[type::Foo]。"""
    return re.sub(r"type::(\w+)", r"\1", t)


def _params_str_local(params: list) -> str:
    parts = []
    for p in params:
        ptype = _python_type_local(p.get("type", "Any"))
        parts.append(f"{p['name']}: {ptype}")
    return ", ".join(parts)


def _build_fn_method_map(tree: list) -> dict[str, tuple[str, bool]]:
    """从 type 节点构建 {fn_id: (method_name, True)} 反查表。"""
    result: dict[str, tuple[str, bool]] = {}
    for node in tree:
        if node.get("kind") == "type":
            for ovl in node.get("overloads", []):
                fn_id = ovl.get("fn", "")
                method_name = ovl.get("method", "")
                if fn_id and method_name:
                    result[fn_id] = (method_name, True)
            init_fn_id = node.get("init_fn")
            if init_fn_id:
                result[init_fn_id] = ("__init__", True)
    return result


def _reconstruct_fn_code(fn_node: dict, fn_id: str, body: str, fn_method_map: dict) -> str:
    """将 body-only 字符串重建为含 def 行的完整函数（与 _extract_func_source 输出格式一致）。

    arch_codegen 输出的 body 不含 def 行，且顶层行无前缀缩进。
    重建后格式：def line\n    body_line1\n    body_line2...
    """
    async_kw = "async " if fn_node.get("is_async") else ""
    params = [p for p in fn_node.get("params", []) if p.get("name") != "self"]
    ret_type = _python_type_local((fn_node.get("returns") or {}).get("type", "None"))

    method_info = fn_method_map.get(fn_id)
    if method_info:
        method_name, _ = method_info
        params_str = _params_str_local(params)
        if params_str:
            def_line = f"{async_kw}def {method_name}(self, {params_str}) -> {ret_type}:"
        else:
            def_line = f"{async_kw}def {method_name}(self) -> {ret_type}:"
    else:
        fname = _fn_name(fn_id)
        params_str = _params_str_local(params)
        def_line = f"{async_kw}def {fname}({params_str}) -> {ret_type}:"

    indented_lines = []
    for line in body.split("\n"):
        if line.strip():
            indented_lines.append("    " + line)
        else:
            indented_lines.append("")

    return def_line + "\n" + "\n".join(indented_lines)


def _file_id_to_rel_path(file_id: str, root: bool = False) -> str:
    """将 file_id 转为相对路径。

    file::optimization/optimizer → optimization/optimizer.py
    file::trainer (root=True)    → trainer.py
    """
    parts = file_id.replace("file::", "").split("/")
    if root:
        return parts[-1] + ".py"
    return "/".join(parts) + ".py"


def _read_fn_bodies_from_disk(
    tree: list, output_dir: str,
) -> dict[str, str]:
    """从磁盘读取最新的函数体，返回 {fn_id: source_code}。

    在 verify_and_fix 修复循环之后调用，确保拿到修复后的代码。
    """
    # 构建 fn_id → file_id 映射（从 file 节点的 functions 列表反查）
    fn_to_file: dict[str, str] = {}
    file_paths: dict[str, str] = {}
    for node in tree:
        kind = node.get("kind")
        if kind == "file":
            fid = node.get("id", "")
            path = node.get("path") or _file_id_to_rel_path(fid, root=node.get("root", False))
            file_paths[fid] = path
            # 从 file 节点的 functions 列表建立 fn_id → file_id 反查
            for fn_id in node.get("functions", []):
                fn_to_file[fn_id] = fid
    # 补充：部分函数节点自带 file 字段（如 design_patch add_fn 创建的）
    for node in tree:
        if node.get("kind") == "function":
            fn_id = node.get("id", "")
            file_id = node.get("file", "")
            if fn_id and file_id and fn_id not in fn_to_file:
                fn_to_file[fn_id] = file_id

    bodies: dict[str, str] = {}
    parsed_cache: dict[str, tuple[str, ast.Module | None]] = {}

    for fn_id, file_id in fn_to_file.items():
        rel_path = file_paths.get(file_id, "")
        if not rel_path:
            continue
        abs_path = os.path.join(output_dir, rel_path)

        # 缓存文件读取和 AST 解析
        if abs_path not in parsed_cache:
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    source = f.read()
                parsed_cache[abs_path] = (source, ast.parse(source))
            except Exception:  # noqa: BLE001
                parsed_cache[abs_path] = ("", None)

        source, module = parsed_cache[abs_path]
        if module is None:
            continue

        # 从 fn_id 提取函数名（fn::file::mod/file::func_name → func_name）
        fn_name = fn_id.rsplit("::", 1)[-1] if "::" in fn_id else fn_id
        lines = source.splitlines(keepends=True)

        for node in ast.walk(module):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn_name:
                start = node.lineno - 1
                end = node.end_lineno
                fn_source = "".join(lines[start:end])
                bodies[fn_id] = fn_source
                break

    return bodies


def arch_enrich_tree_with_bodies(
    tree: list,
    fn_bodies: dict,
    output_dir: str = "",
) -> ArchEnrichTreeResult:
    """将 fn_bodies 写入架构树各 fn:: 节点的 code 字段，并为 file 节点补全 path 字段。

    若提供了 output_dir，会先从磁盘读取最新函数体（以获取 verify_and_fix
    修复后的代码），覆盖 fn_bodies 中的旧值。

    Args:
        tree:       原始架构树（list[dict]，arch_build 输出格式）。
        fn_bodies:  {fn_id: body_str} 映射，由 arch_collect_fn_bodies 产出。
        output_dir: 项目根目录。若非空则从磁盘重新读取函数体。

    Returns:
        tree: fn:: 节点增加 code 字段、file:: 节点增加 path 字段后的架构树。
    """
    # 若提供了 output_dir，从磁盘读取最新的（修复后的）函数体
    if output_dir:
        disk_bodies = _read_fn_bodies_from_disk(tree, output_dir)
        if disk_bodies:
            logger.info("enrich_tree: 从磁盘读取 %d 个修复后函数体", len(disk_bodies))
            fn_bodies = {**fn_bodies, **disk_bodies}

    fn_method_map = _build_fn_method_map(tree)

    for node in tree:
        kind = node.get("kind")
        if kind == "function":
            fn_id = node.get("id", "")
            if fn_id in fn_bodies:
                body = fn_bodies[fn_id]
                node["code"] = _reconstruct_fn_code(node, fn_id, body, fn_method_map)
        elif kind == "file":
            if "path" not in node:
                fid = node.get("id", "")
                node["path"] = _file_id_to_rel_path(fid, root=node.get("root", False))
    return ArchEnrichTreeResult(tree=tree)
