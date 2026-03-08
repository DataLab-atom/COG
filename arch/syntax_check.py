"""
arch_syntax_check: 对已写入磁盘的 Python 文件执行语法检查（py_compile）。
- 逐文件编译，收集 SyntaxError
- 返回 ArchSyntaxCheckResult(ok, errors)
  errors 格式：["path/to/file.py:10: invalid syntax", ...]
"""
from __future__ import annotations
import py_compile

from utils._base import ToolResult


class ArchSyntaxCheckResult(ToolResult):
    ok: bool
    errors: list[str]


def arch_syntax_check(written: list[str]) -> ArchSyntaxCheckResult:
    errors: list[str] = []
    for path in written:
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(str(e))
    return ArchSyntaxCheckResult(ok=len(errors) == 0, errors=errors)
