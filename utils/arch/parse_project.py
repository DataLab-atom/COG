"""
arch/parse_project.py — 从现有 Python 项目反向解析生成架构树。

将 project_root 目录下的所有 .py 文件通过 AST 解析为 arch tree，
输出格式与 arch_build + arch_to_project 组合输出相同（含各函数 code 字段），
可直接替代 arch_build_and_gate 阶段接入 MCTS/U2E 优化流水线。

公开接口：
    arch_parse_project(project_root, metric_name, direction, ignore_patterns)
"""
from __future__ import annotations

import ast
import os
import textwrap
from pathlib import Path
from typing import Any

from utils import ToolResult


class ArchParseProjectResult(ToolResult):
    ok: bool
    errors: list[str]
    tree: list[dict]
    metric_name: str
    direction: str
    written: list[str]
    data_files: list[dict]  # [{path, size_mb, ext}] 项目中发现的数据文件


_DEFAULT_IGNORE: set[str] = {
    "__pycache__", ".git", ".svn", ".hg",
    "venv", ".venv", "env", ".env",
    "node_modules", ".tox", "dist", "build",
    ".eggs", ".mypy_cache", ".pytest_cache",
}

# 常见数据文件扩展名
_DATA_EXTENSIONS: set[str] = {
    ".csv", ".tsv", ".json", ".jsonl", ".ndjson",
    ".h5", ".h5ad", ".hdf5",
    ".npy", ".npz", ".pkl", ".pickle",
    ".parquet", ".feather", ".arrow",
    ".xlsx", ".xls",
    ".txt", ".dat", ".gz", ".zip",
    ".mtx", ".loom", ".zarr",
}

# 数据文件大小上限（收集文件列表时不读取内容，只记录路径和大小）
_DATA_SIZE_LIMIT_MB = 500


# ── 路径辅助 ──────────────────────────────────────────────────────────────────

def _collect_data_files(project_root: str, ignore_set: set[str]) -> list[dict]:
    """递归收集 project_root 下的数据文件（非 .py），返回 [{path, size_mb, ext}]。

    只收集常见数据格式的文件信息（路径和大小），不读取文件内容。
    用于让 codegen LLM 知道项目中有哪些数据可以加载。
    """
    result: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in ignore_set and not d.startswith(".")
        )
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _DATA_EXTENSIONS:
                continue
            abs_path = os.path.join(dirpath, fname)
            try:
                size_bytes = os.path.getsize(abs_path)
            except OSError:
                continue
            size_mb = round(size_bytes / (1024 * 1024), 2)
            if size_mb > _DATA_SIZE_LIMIT_MB:
                continue
            rel_path = os.path.relpath(abs_path, project_root).replace("\\", "/")
            result.append({
                "path":    rel_path,
                "size_mb": size_mb,
                "ext":     ext,
            })
    return result


def _collect_py_files(project_root: str, ignore_set: set[str]) -> list[str]:
    """递归收集 project_root 下所有 .py 文件（绝对路径，已排序）。"""
    result: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in ignore_set and not d.startswith(".")
        )
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                result.append(os.path.join(dirpath, fname))
    return result


def _rel(abs_path: str, root: str) -> str:
    """返回 abs_path 相对于 root 的路径，统一用 / 分隔。"""
    return os.path.relpath(abs_path, root).replace("\\", "/")


def _file_id(rel_py: str) -> str:
    """'optimization/optimizer.py' → 'file::optimization/optimizer'"""
    return "file::" + (rel_py[:-3] if rel_py.endswith(".py") else rel_py)


def _module_id(rel_dir: str) -> str:
    """'optimization' → 'mod::optimization'"""
    return f"mod::{rel_dir}"


# ── AST 辅助 ──────────────────────────────────────────────────────────────────

def _type_str(annotation: ast.expr | None) -> str:
    """将 AST annotation 转为类型字符串；无注解返回 'Any'。"""
    if annotation is None:
        return "Any"
    try:
        return ast.unparse(annotation)
    except Exception:
        return "Any"


def _extract_params(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[dict]:
    """从函数节点提取参数列表，跳过 self / cls。"""
    args = func.args
    all_args = args.posonlyargs + args.args + args.kwonlyargs
    params: list[dict] = []
    for arg in all_args:
        if arg.arg in ("self", "cls"):
            continue
        params.append({
            "name": arg.arg,
            "type": _type_str(arg.annotation),
        })
    return params


def _extract_returns(func: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """提取函数返回类型注解。"""
    return {"type": _type_str(func.returns)}


def _extract_func_source(
    source_lines: list[str],
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    """提取函数完整源码（含 def 行及装饰器），去除公共前缀缩进。"""
    start = func.lineno - 1  # 转 0-indexed
    end   = func.end_lineno  # end_lineno 是 1-indexed，直接用作 slice 上界
    if func.decorator_list:
        start = func.decorator_list[0].lineno - 1
    raw = "".join(source_lines[start:end])
    return textwrap.dedent(raw).rstrip()


def _extract_class_source(
    source_lines: list[str],
    cls: ast.ClassDef,
) -> str:
    """提取类完整源码（含 class 行及装饰器），去除公共前缀缩进。"""
    start = cls.lineno - 1
    end   = cls.end_lineno
    if cls.decorator_list:
        start = cls.decorator_list[0].lineno - 1
    raw = "".join(source_lines[start:end])
    return textwrap.dedent(raw).rstrip()


def _make_unique_id(base_id: str, used: set[str]) -> str:
    """若 base_id 已被占用，自动添加数字后缀直到唯一。"""
    if base_id not in used:
        return base_id
    i = 2
    while f"{base_id}_{i}" in used:
        i += 1
    return f"{base_id}_{i}"


# ── 主函数 ────────────────────────────────────────────────────────────────────

def arch_parse_project(
    project_root: str,
    metric_name: str,
    direction: str,
    ignore_patterns: list[str] | None = None,
) -> ArchParseProjectResult:
    """从现有 Python 项目解析生成 arch tree（含各函数 code 字段）。

    Args:
        project_root:     项目根目录绝对路径。
        metric_name:      优化指标名称（如 'score'），传入 MCTS/U2E 使用。
        direction:        优化方向 'max' 或 'min'。
        ignore_patterns:  额外忽略的目录/文件名列表（在默认忽略集合之上追加）。

    Returns:
        ArchParseProjectResult — ok, errors, tree, metric_name, direction, written
    """
    root = os.path.abspath(project_root)
    ignore_set = _DEFAULT_IGNORE.copy()
    if ignore_patterns:
        ignore_set.update(ignore_patterns)

    errors: list[str] = []

    # 收集数据文件信息（供 codegen 知道可用的数据）
    data_files = _collect_data_files(root, ignore_set)

    py_files = _collect_py_files(root, ignore_set)
    if not py_files:
        return ArchParseProjectResult(
            ok=False,
            errors=[f"在 {root} 下未找到任何 .py 文件"],
            tree=[],
            metric_name=metric_name,
            direction=direction,
            written=[],
            data_files=data_files,
        )

    # ── 全局 ID 唯一性追踪 ────────────────────────────────────────────────────
    used_fn_ids:   set[str] = set()
    used_type_ids: set[str] = set()

    # ── 收集各类节点 ──────────────────────────────────────────────────────────
    dir_to_mod:   dict[str, dict] = {}   # rel_dir → module node
    file_nodes:   list[dict]      = []
    extra_nodes:  list[dict]      = []   # type / function 节点

    for abs_path in py_files:
        rel        = _rel(abs_path, root)
        fid        = _file_id(rel)
        is_root    = "/" not in rel
        rel_dir    = os.path.dirname(rel) if not is_root else ""

        # 确保对应目录的 module 节点存在
        if rel_dir and rel_dir not in dir_to_mod:
            mid = _module_id(rel_dir)
            dir_to_mod[rel_dir] = {
                "id":      mid,
                "kind":    "module",
                "root":    False,
                "exports": [],
            }
        # 根目录视为 root module
        if not rel_dir and "" not in dir_to_mod:
            dir_to_mod[""] = {
                "id":      "mod::root",
                "kind":    "module",
                "root":    True,
                "exports": [],
            }

        # 读取并解析源码
        try:
            source       = Path(abs_path).read_text(encoding="utf-8")
            source_lines = source.splitlines(keepends=True)
            parsed       = ast.parse(source, filename=abs_path)
        except Exception as exc:
            errors.append(f"解析失败 {rel}: {exc}")
            continue

        file_node: dict[str, Any] = {
            "id":                   fid,
            "kind":                 "file",
            "path":                 _rel(abs_path, root),
            "root":                 is_root,
            "imports":              [],
            "constants":            [],
            "types":                [],
            "functions":            [],
            "standalone_functions": [],   # 仅顶层独立函数，不含类方法
        }

        # ── imports（只收集模块顶层，不含函数/类内部的局部 import）────────────
        for node in parsed.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        # import pandas as pd → {"import": "pandas", "as": "pd"}
                        file_node["imports"].append({"import": alias.name, "as": alias.asname})
                    else:
                        # import os → {"import": "os"}
                        file_node["imports"].append({"import": alias.name})
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [alias.asname or alias.name for alias in node.names]
                file_node["imports"].append({
                    "from":  node.module,
                    "names": names,
                })
            elif isinstance(node, (ast.If, ast.Try)):
                # 处理 try/except 或 if TYPE_CHECKING 里的顶层 import
                for child in ast.walk(node):
                    if child is node:
                        continue
                    if isinstance(child, ast.Import):
                        for alias in child.names:
                            if alias.asname:
                                file_node["imports"].append({"import": alias.name, "as": alias.asname})
                            else:
                                file_node["imports"].append({"import": alias.name})
                    elif isinstance(child, ast.ImportFrom) and child.module:
                        i_names = [alias.asname or alias.name for alias in child.names]
                        file_node["imports"].append({
                            "from":  child.module,
                            "names": i_names,
                        })

        # ── 顶层语句 ──────────────────────────────────────────────────────────
        for stmt in parsed.body:

            # 顶层函数
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_id = _make_unique_id(f"fn::{stmt.name}", used_fn_ids)
                used_fn_ids.add(fn_id)

                _params = _extract_params(stmt)
                _returns = _extract_returns(stmt)
                _docstring = ast.get_docstring(stmt) or ""
                if not _docstring:
                    _param_str = ", ".join(p["name"] for p in _params)
                    _ret = _returns.get("type", "Any")
                    _docstring = f"{stmt.name}({_param_str}) -> {_ret}"
                fn_node: dict[str, Any] = {
                    "id":          fn_id,
                    "kind":        "function",
                    "params":      _params,
                    "returns":     _returns,
                    "is_async":    isinstance(stmt, ast.AsyncFunctionDef),
                    "description": _docstring,
                    "code":        _extract_func_source(source_lines, stmt),
                }
                extra_nodes.append(fn_node)
                file_node["functions"].append(fn_id)
                file_node["standalone_functions"].append(fn_id)  # 独立函数，可作模块导出

            # 类定义
            elif isinstance(stmt, ast.ClassDef):
                type_id = _make_unique_id(f"type::{stmt.name}", used_type_ids)
                used_type_ids.add(type_id)

                base_class = ast.unparse(stmt.bases[0]) if stmt.bases else ""
                fields:    list[dict] = []
                init_fn_id: str | None = None
                overloads:  list[dict] = []

                for item in stmt.body:
                    if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    mname  = item.name
                    mfn_id = _make_unique_id(f"fn::{mname}", used_fn_ids)
                    used_fn_ids.add(mfn_id)

                    _mparams = _extract_params(item)
                    _mreturns = _extract_returns(item)
                    _mdocstring = ast.get_docstring(item) or ""
                    if not _mdocstring:
                        _mparam_str = ", ".join(p["name"] for p in _mparams)
                        _mret = _mreturns.get("type", "Any")
                        _mdocstring = f"{item.name}({_mparam_str}) -> {_mret}"
                    mfn_node: dict[str, Any] = {
                        "id":          mfn_id,
                        "kind":        "function",
                        "params":      _mparams,
                        "returns":     _mreturns,
                        "is_async":    isinstance(item, ast.AsyncFunctionDef),
                        "description": _mdocstring,
                        "code":        _extract_func_source(source_lines, item),
                    }
                    extra_nodes.append(mfn_node)
                    file_node["functions"].append(mfn_id)

                    if mname == "__init__":
                        init_fn_id = mfn_id
                        # 从 __init__ 推断字段
                        for body_stmt in item.body:
                            if isinstance(body_stmt, ast.Assign):
                                for tgt in body_stmt.targets:
                                    if (
                                        isinstance(tgt, ast.Attribute)
                                        and isinstance(tgt.value, ast.Name)
                                        and tgt.value.id == "self"
                                    ):
                                        # 尝试从参数中匹配类型
                                        ftype = "Any"
                                        if isinstance(body_stmt.value, ast.Name):
                                            for p in mfn_node["params"]:
                                                if p["name"] == body_stmt.value.id:
                                                    ftype = p["type"]
                                                    break
                                        fields.append({"name": tgt.attr, "type": ftype})
                            elif isinstance(body_stmt, ast.AnnAssign):
                                if (
                                    isinstance(body_stmt.target, ast.Attribute)
                                    and isinstance(body_stmt.target.value, ast.Name)
                                    and body_stmt.target.value.id == "self"
                                ):
                                    fields.append({
                                        "name": body_stmt.target.attr,
                                        "type": _type_str(body_stmt.annotation),
                                    })
                    else:
                        overloads.append({"fn": mfn_id, "method": mname})

                type_node: dict[str, Any] = {
                    "id":          type_id,
                    "kind":        "type",
                    "base_class":  base_class,
                    "description": ast.get_docstring(stmt) or "",
                    "fields":      fields,
                    "constants":   [],
                    "overloads":   overloads,
                }
                if init_fn_id:
                    type_node["init_fn"] = init_fn_id
                extra_nodes.append(type_node)
                file_node["types"].append(type_id)

            # 模块级全大写常量
            elif isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id.isupper():
                        try:
                            val      = ast.literal_eval(stmt.value)
                            type_str = type(val).__name__
                        except Exception:
                            val      = ast.unparse(stmt.value)
                            type_str = "Any"
                        file_node["constants"].append({
                            "name":  tgt.id,
                            "type":  type_str,
                            "value": val,
                        })
            elif isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name) and stmt.target.id.isupper():
                    try:
                        val = ast.literal_eval(stmt.value) if stmt.value else None
                    except Exception:
                        val = ast.unparse(stmt.value) if stmt.value else None
                    file_node["constants"].append({
                        "name":  stmt.target.id,
                        "type":  _type_str(stmt.annotation),
                        "value": val,
                    })

        file_nodes.append(file_node)

        # 将 file 的公开 id 注册到所属 module 的 exports
        # 只导出类型（类本体）和独立顶层函数，不导出类方法（类方法不能从模块顶层 import）
        mod_node = dir_to_mod.get(rel_dir)
        if mod_node is not None:
            mod_node["exports"].extend(file_node["types"])
            mod_node["exports"].extend(file_node["standalone_functions"])

    # ── 检查 fn::main → entrypoint ────────────────────────────────────────────
    has_main = any(n.get("id") == "fn::main" for n in extra_nodes)

    # ── 组装最终 tree ──────────────────────────────────────────────────────────
    tree: list[dict] = []
    tree.extend(dir_to_mod.values())
    tree.extend(file_nodes)
    tree.extend(extra_nodes)
    if has_main:
        tree.append({"kind": "entrypoint", "fn": "fn::main"})

    # 将数据文件信息作为特殊节点加入树，供 codegen 感知
    if data_files:
        tree.append({
            "kind":       "data_files",
            "description": "项目中可用的数据文件，main 函数应优先加载这些文件而非生成合成数据",
            "files":       data_files,
        })

    return ArchParseProjectResult(
        ok=True,
        errors=errors,
        tree=tree,
        metric_name=metric_name,
        direction=direction,
        written=py_files,
        data_files=data_files,
    )
