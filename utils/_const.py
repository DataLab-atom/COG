"""
_const.py — 项目级公共常量

提供 utils 包内各模块共享的路径常量和通用辅助，避免在每个模块重复定义。
"""

import sys
from pathlib import Path

# 项目根目录（utils/ 的上一级）
PROJECT_ROOT = Path(__file__).parent.parent

# ── 标准库模块名集合 ─────────────────────────────────────────────────────────────
# Python 3.10+ 直接使用 sys.stdlib_module_names；旧版本回退到内置集合。
# 覆盖范围：科学计算场景的常见标准库模块（超集，含 gen_requirements 和
# sandbox_tools 两处原有回退集合的并集）。

_STDLIB_FALLBACK: frozenset[str] = frozenset({
    "abc", "ast", "asyncio", "base64", "builtins", "cmath", "collections",
    "concurrent", "contextlib", "copy", "csv", "dataclasses", "datetime",
    "decimal", "dis", "email", "enum", "fnmatch", "fractions", "functools",
    "gc", "glob", "gzip", "hashlib", "heapq", "http", "inspect", "io",
    "itertools", "json", "keyword", "logging", "math", "multiprocessing",
    "operator", "os", "pathlib", "pickle", "platform", "pprint", "queue",
    "random", "re", "shutil", "signal", "socket", "sqlite3", "ssl",
    "stat", "statistics", "string", "struct", "subprocess", "sys",
    "tempfile", "textwrap", "threading", "time", "timeit", "tokenize",
    "traceback", "types", "typing", "unittest", "urllib", "uuid",
    "warnings", "weakref", "xml", "zipfile", "zlib", "__future__",
})


def stdlib_names() -> frozenset[str]:
    """返回标准库模块名集合（Python 3.10+ 原生；旧版本使用内置回退集合）。"""
    try:
        return sys.stdlib_module_names  # type: ignore[attr-defined]
    except AttributeError:
        return _STDLIB_FALLBACK
