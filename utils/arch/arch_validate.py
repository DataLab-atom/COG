"""
arch_validate — 架构树校验工具集

包含四个层次的校验工具，由轻到重依次使用：

arch_syntax_check:
    对已写入磁盘的 Python 文件执行语法检查（py_compile）。
    用于 arch_assemble 写入后的最终验证，确保生成代码无语法错误。

arch_check_entrypoint:
    对已写入磁盘的 main.py 执行内容校验：
    - 必须包含 __METRICS__: 输出语句
    - 必须调用 compute_metrics 并将返回值传给 __METRICS__ 输出
    - metric_fn.py 必须定义 compute_metrics 且有非空 return 语句

arch_validate_dag:
    对架构树中指定的有向图执行环检测（DFS 拓扑排序）。
    可检测 module_imports、file_imports、fn_calls 三种图。

arch_static_check:
    对完整项目树执行全量静态分析：id 引用、循环依赖、overloads 一致性、
    needs 清空、entrypoint 可达性、exports 合法性、死函数检测、
    root 模块必须含 main/metric_fn 文件节点。
"""
from __future__ import annotations

import ast
import os
import py_compile

from utils import ToolResult


# ── arch_syntax_check ──────────────────────────────────────────────────────────

class ArchSyntaxCheckResult(ToolResult):
    ok: bool
    errors: list[str]


def arch_syntax_check(written: list[str]) -> ArchSyntaxCheckResult:
    """对已写入磁盘的 Python 文件逐一执行 py_compile 语法检查。

    errors 格式：["<filename>, line <N>: <message>", ...]
    """
    errors: list[str] = []
    for path in written:
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(str(e))
    return ArchSyntaxCheckResult(ok=len(errors) == 0, errors=errors)


# ── arch_check_entrypoint ──────────────────────────────────────────────────────

class ArchCheckEntrypointResult(ToolResult):
    ok: bool
    errors: list[str]


def _has_metrics_print(tree: ast.Module) -> bool:
    """检查 AST 中是否存在包含 '__METRICS__:' 的 print 调用。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "print"):
            continue
        # 检查任意参数中含有 __METRICS__: 字符串
        for arg in node.args:
            src = ast.unparse(arg)
            if "__METRICS__:" in src:
                return True
    return False


def _compute_metrics_is_used(tree: ast.Module) -> bool:
    """检查 compute_metrics 的返回值是否被赋值（而非直接丢弃）。"""
    for node in ast.walk(tree):
        # 赋值：x = compute_metrics(...)
        if isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Name) and call.func.id == "compute_metrics":
                    return True
                if isinstance(call.func, ast.Attribute) and call.func.attr == "compute_metrics":
                    return True
        # 带注解赋值：x: dict = compute_metrics(...)
        if isinstance(node, ast.AnnAssign) and node.value:
            call = node.value
            if isinstance(call, ast.Call):
                if isinstance(call.func, ast.Name) and call.func.id == "compute_metrics":
                    return True
    return False


def _metric_fn_has_return(source: str) -> bool:
    """检查 metric_fn.py 中 compute_metrics 函数是否有非空 return 语句。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != "compute_metrics":
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and child.value is not None:
                    return True
    return False


def arch_check_entrypoint(output_dir: str) -> ArchCheckEntrypointResult:
    """校验 main.py 和 metric_fn.py 的运行时可观测性。

    检查项：
    1. main.py 存在
    2. main.py 包含 __METRICS__: 输出（print 调用）
    3. main.py 调用 compute_metrics 且使用其返回值（非丢弃）
    4. metric_fn.py 存在
    5. metric_fn.py 的 compute_metrics 函数有非空 return 语句

    Args:
        output_dir: 项目根目录绝对路径（即包含 main.py 的目录）。
    """
    errors: list[str] = []

    main_path      = os.path.join(output_dir, "main.py")
    metric_fn_path = os.path.join(output_dir, "metric_fn.py")

    # 1. main.py 存在
    if not os.path.isfile(main_path):
        errors.append("main.py 不存在")
        return ArchCheckEntrypointResult(ok=False, errors=errors)

    try:
        with open(main_path, encoding="utf-8") as f:
            main_src = f.read()
        main_tree = ast.parse(main_src)
    except (OSError, SyntaxError) as e:
        errors.append(f"main.py 解析失败: {e}")
        return ArchCheckEntrypointResult(ok=False, errors=errors)

    # 2. __METRICS__: 输出
    if not _has_metrics_print(main_tree):
        errors.append('main.py 缺少 print(f"__METRICS__:{...}") 输出语句')

    # 3. compute_metrics 返回值被使用
    if not _compute_metrics_is_used(main_tree):
        errors.append("main.py 未将 compute_metrics() 的返回值赋给变量（返回值可能被丢弃）")

    # 4. metric_fn.py 存在
    if not os.path.isfile(metric_fn_path):
        errors.append("metric_fn.py 不存在")
    else:
        try:
            with open(metric_fn_path, encoding="utf-8") as f:
                metric_src = f.read()
        except OSError as e:
            errors.append(f"metric_fn.py 读取失败: {e}")
            metric_src = ""

        # 5. compute_metrics 有非空 return
        if metric_src and not _metric_fn_has_return(metric_src):
            errors.append("metric_fn.py 的 compute_metrics 函数缺少非空 return 语句")

    return ArchCheckEntrypointResult(ok=len(errors) == 0, errors=errors)


# ── arch_validate_dag ──────────────────────────────────────────────────────────

class ArchValidateDagResult(ToolResult):
    ok: bool
    errors: list[str]


def _detect_cycle(graph: dict[str, list[str]]) -> list[str]:
    """拓扑排序检测有向图中的环，返回成环的节点列表。"""
    visited: set[str] = set()
    in_stack: set[str] = set()
    cycle_nodes: list[str] = []

    def dfs(node: str) -> bool:
        visited.add(node)
        in_stack.add(node)
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in in_stack:
                cycle_nodes.append(neighbor)
                return True
        in_stack.discard(node)
        return False

    for node in list(graph.keys()):
        if node not in visited:
            if dfs(node):
                break

    return cycle_nodes


def arch_validate_dag(tree: list[dict], check_targets: list[str]) -> ArchValidateDagResult:
    """对架构树中的有向图执行环检测。

    check_targets 可选值：
        - "module_imports"：模块间导入依赖图
        - "file_imports"：文件间导入依赖图
        - "fn_calls"：函数调用图
    """
    errors: list[str] = []

    modules   = {n["id"]: n for n in tree if n.get("kind") == "module"}
    files     = {n["id"]: n for n in tree if n.get("kind") == "file"}
    functions = {n["id"]: n for n in tree if n.get("kind") == "function"}

    if "module_imports" in check_targets:
        graph = {mid: m.get("imports", []) for mid, m in modules.items()}
        cycle = _detect_cycle(graph)
        if cycle:
            errors.append(f"module_imports 有环，涉及节点: {cycle}")

    if "file_imports" in check_targets:
        graph = {fid: [imp["from"] for imp in f.get("imports", []) if isinstance(imp, dict) and "from" in imp]
                 for fid, f in files.items()}
        cycle = _detect_cycle(graph)
        if cycle:
            errors.append(f"file_imports 有环，涉及节点: {cycle}")

    if "fn_calls" in check_targets:
        graph = {fid: fn.get("calls", []) for fid, fn in functions.items()}
        cycle = _detect_cycle(graph)
        if cycle:
            errors.append(f"fn_calls 有环，涉及节点: {cycle}")

    return ArchValidateDagResult(ok=len(errors) == 0, errors=errors)


# ── arch_static_check ──────────────────────────────────────────────────────────

class ArchStaticCheckResult(ToolResult):
    ok: bool
    errors: list[str]
    warnings: list[str]


def arch_static_check(tree: list[dict]) -> ArchStaticCheckResult:
    """对完整项目树执行全量静态检查。

    检查项：
    1. 所有 id 引用可解析
    2. module.imports 无环
    3. file.imports 无环
    4. fn.calls 无环
    5. overloads.args 与 fn:: params 名称一致
    6. needs 全部清空（arch_resolve_needs 已统一处理，此处为最终兜底校验）
    7. entrypoint 存在且 fn 可达
    8. exports 合法性（只能包含该 module 旗下 file 中的 id）
    9. 死函数检测（warning：不可达的 fn）
    """
    errors: list[str] = []
    warnings: list[str] = []

    all_ids = {n["id"] for n in tree if "id" in n}
    modules   = {n["id"]: n for n in tree if n.get("kind") == "module"}
    files     = {n["id"]: n for n in tree if n.get("kind") == "file"}
    types_map = {n["id"]: n for n in tree if n.get("kind") == "type"}
    fns_map   = {n["id"]: n for n in tree if n.get("kind") == "function"}
    entrypoint = next((n for n in tree if n.get("kind") == "entrypoint"), None)

    file_contains: dict[str, set[str]] = {
        fid: set(f.get("types", [])) | set(f.get("functions", []))
        for fid, f in files.items()
    }
    module_contains: dict[str, set[str]] = {
        mid: {eid for fid in m.get("files", []) for eid in file_contains.get(fid, set())}
        for mid, m in modules.items()
    }

    # 1. id 引用可解析
    for fid, f in files.items():
        for imp in f.get("imports", []):
            if not isinstance(imp, dict):
                continue
            imp_from = imp.get("from", "")
            if not imp_from:
                continue
            if imp_from not in files:
                errors.append(f"{fid} imports.from={imp_from!r} 不存在")
            for name in imp.get("names", []):
                matched = any(
                    i.split("::")[-1] == name
                    for i in file_contains.get(imp_from, set())
                )
                if not matched:
                    errors.append(
                        f"{fid} imports name={name!r} 在 {imp_from} 中未找到"
                    )

    for fnid, fn in fns_map.items():
        for called in fn.get("calls", []):
            if called not in all_ids:
                errors.append(f"{fnid} calls={called!r} 不存在")
        for param in fn.get("params", []):
            t = param.get("type", "")
            if t.startswith("type::") and t not in all_ids:
                errors.append(f"{fnid} param type={t!r} 不存在")

    for tid, t in types_map.items():
        for ovl in t.get("overloads", []):
            fn_id = ovl.get("fn", "")
            if fn_id and fn_id not in all_ids:
                errors.append(f"{tid} overload fn={fn_id!r} 不存在")

    # 2-4. 环检测（复用 arch_validate_dag 逻辑）
    dag_result = arch_validate_dag(tree, ["module_imports", "file_imports", "fn_calls"])
    errors.extend(dag_result.errors)

    # 5. overloads.args 与 fn params 一致
    for tid, t in types_map.items():
        for ovl in t.get("overloads", []):
            fn_id = ovl.get("fn", "")
            fn = fns_map.get(fn_id)
            if not fn:
                continue
            fn_param_names = [p["name"] for p in fn.get("params", [])]
            ovl_args = ovl.get("args", [])
            if ovl_args and ovl_args != fn_param_names:
                errors.append(
                    f"{tid} overload.args={ovl_args} 与 {fn_id} params={fn_param_names} 不一致"
                )

    # 6. needs 全部清空
    for fn in fns_map.values():
        if fn.get("needs"):
            errors.append(f"{fn['id']} needs 未清空: {fn['needs']}")

    # 7. entrypoint 存在且可达
    if not entrypoint:
        errors.append("缺少 entrypoint 节点")
    else:
        ep_fn = entrypoint.get("fn", "")
        if ep_fn not in all_ids:
            errors.append(f"entrypoint fn={ep_fn!r} 不存在")
        else:
            reachable: set[str] = set()
            queue = [ep_fn]
            while queue:
                cur = queue.pop()
                if cur in reachable:
                    continue
                reachable.add(cur)
                fn = fns_map.get(cur, {})
                queue.extend(fn.get("calls", []))

            dead = set(fns_map.keys()) - reachable
            if dead:
                warnings.append(f"死函数（不可达）: {sorted(dead)}")

    # 8. exports 合法性
    for mid, m in modules.items():
        for exp_id in m.get("exports", []):
            if exp_id not in module_contains.get(mid, set()):
                errors.append(
                    f"{mid} exports={exp_id!r} 不在该模块旗下任何 file 中"
                )

    # 9. root 模块必须包含 main.py 和 metric_fn.py
    for mid, m in modules.items():
        if not m.get("root"):
            continue
        module_files = m.get("files", [])
        has_main      = any("main"      in fid for fid in module_files)
        has_metric_fn = any("metric_fn" in fid for fid in module_files)
        if not has_main:
            errors.append(
                f"{mid} 为 root 模块但缺少 main.py 文件节点（需含 'main' 的 file id）"
            )
        if not has_metric_fn:
            errors.append(
                f"{mid} 为 root 模块但缺少 metric_fn.py 文件节点（需含 'metric_fn' 的 file id）"
            )

    return ArchStaticCheckResult(ok=len(errors) == 0, errors=errors, warnings=warnings)
