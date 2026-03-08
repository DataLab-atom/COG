"""
_oa_loader.py — 将 open-agents/utils 加载为 _oa_utils 命名空间包。

解决冲突：COG 的 utils/ 包与 open-agents 的 utils/ 包同名，
Python 只能选其一。此模块用 importlib 将 open-agents/utils 加载为
独立命名空间 _oa_utils，再由各 shim 文件按需重导出。

同时将 open-agents 框架内的 _PROJECT_ROOT 补丁为 COG 根目录，
确保 tool/input/graph JSON 路径解析以 COG 为基准。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_COG_ROOT = Path(__file__).parent.parent          # /home/user/COG
_OA_UTILS = _COG_ROOT.parent / "open-agents" / "utils"
_PKG = "_oa_utils"

# 执行顺序：依赖前置（被依赖的模块先 exec）
_EXEC_ORDER = [
    "_base",
    "_channel",
    "text_utils",
    "_pipeline",
    "input_utils",
    "_registry",
    "_validator",
    "_graph",
]


def _load_oa_utils():
    if _PKG in sys.modules:
        return sys.modules[_PKG]

    # 1. 创建并注册 package 对象
    pkg_spec = importlib.util.spec_from_file_location(
        _PKG,
        str(_OA_UTILS / "__init__.py"),
        submodule_search_locations=[str(_OA_UTILS)],
    )
    pkg = importlib.util.module_from_spec(pkg_spec)
    pkg.__package__ = _PKG
    sys.modules[_PKG] = pkg

    # 2. 预注册所有子模块（使 relative import 在 exec 时可找到）
    for stem in _EXEC_ORDER:
        name = f"{_PKG}.{stem}"
        spec = importlib.util.spec_from_file_location(
            name, str(_OA_UTILS / f"{stem}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = _PKG
        sys.modules[name] = mod

    # 3. 按依赖顺序执行子模块
    for stem in _EXEC_ORDER:
        name = f"{_PKG}.{stem}"
        sys.modules[name].__spec__.loader.exec_module(sys.modules[name])

    # 4. 将框架内路径基准补丁为 COG 根目录
    for attr in ("_registry", "_graph", "_pipeline"):
        mod = sys.modules[f"{_PKG}.{attr}"]
        mod._PROJECT_ROOT = _COG_ROOT
        if hasattr(mod, "_TOOLS_DIR"):
            mod._TOOLS_DIR = _COG_ROOT / "tools"
        if hasattr(mod, "_INPUT_DIR"):
            mod._INPUT_DIR = _COG_ROOT / "input"

    # 5. 执行 __init__.py（会 re-export 所有公开符号）
    pkg_spec.loader.exec_module(pkg)

    return pkg


oa_utils = _load_oa_utils()
