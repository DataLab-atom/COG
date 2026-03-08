"""
arch_syntax_check: 对已写入磁盘的 Python 文件执行语法检查（py_compile）。

逐文件调用 py_compile.compile(doraise=True)，捕获 PyCompileError 并收集到 errors。
用于 arch_assemble 写入后的最终验证，确保生成代码无语法错误。

返回 ArchSyntaxCheckResult(ok, errors)
  - errors 格式：["<filename>, line <N>: <message>", ...]
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
