"""
text_utils — 文本处理工具函数

所有函数返回 ToolResult 子类，字段可直接访问，无需解析。

查看文档:
    python -m pydoc utils.text_utils
    >>> from utils import text_utils; help(text_utils)
"""

from __future__ import annotations

from ._base import ToolResult


# ── 返回值类型定义 ──────────────────────────────────────────────────────────

class EchoResult(ToolResult):
    """echo() 的返回值。"""
    text: str            # 原样返回的文本
    length: int          # 文本字符数


class TruncateResult(ToolResult):
    """truncate() 的返回值。"""
    text: str            # 截断后的文本
    was_truncated: bool  # 是否发生了截断
    original_length: int # 原始文本字符数


class RenderResult(ToolResult):
    """render_template() 的返回值。"""
    text: str               # 渲染后的文本
    replaced_keys: list[str]  # 实际被替换的变量名列表
    missing_keys: list[str]   # 模板中存在但未提供的变量名列表


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def truncate(text: str, max_chars: int, suffix: str = "...") -> TruncateResult:
    """将文本截断到指定字符数上限。

    Args:
        text: 待截断的原始文本。
        max_chars: 允许的最大字符数（含 suffix）。
        suffix: 超出时附加到末尾的省略符，默认为 "..."。

    Returns:
        TruncateResult:
            .text            截断后的字符串
            .was_truncated   是否发生了截断
            .original_length 原始文本字符数

    Raises:
        ValueError: 当 max_chars 小于 suffix 长度时抛出。

    Example:
        >>> r = truncate("你好世界，这是一段很长的文字", 8)
        >>> r.text
        '你好世界，...'
        >>> r.was_truncated
        True
        >>> r.model_dump()
        {'text': '你好世界，...', 'was_truncated': True, 'original_length': 14}
    """
    if max_chars < len(suffix):
        raise ValueError(f"max_chars ({max_chars}) 不能小于 suffix 长度 ({len(suffix)})")
    original_length = len(text)
    if original_length <= max_chars:
        return TruncateResult(text=text, was_truncated=False, original_length=original_length)
    return TruncateResult(
        text=text[: max_chars - len(suffix)] + suffix,
        was_truncated=True,
        original_length=original_length,
    )


def render_template(template: str, variables: dict[str, str]) -> RenderResult:
    """将模板字符串中的 {{变量名}} 占位符替换为实际值。

    未提供的占位符原样保留，记录在 missing_keys 中。

    Args:
        template: 包含 {{variable}} 占位符的模板字符串。
        variables: 变量名到替换值的映射字典。

    Returns:
        RenderResult:
            .text           替换后的字符串
            .replaced_keys  实际发生替换的变量名列表
            .missing_keys   模板中存在但未提供的变量名列表

    Example:
        >>> r = render_template("你好，{{name}}！今天是 {{date}}。", {"name": "张三"})
        >>> r.text
        '你好，张三！今天是 {{date}}。'
        >>> r.replaced_keys
        ['name']
        >>> r.missing_keys
        ['date']
    """
    import re
    all_keys = re.findall(r"\{\{(\w+)\}\}", template)
    replaced_keys, missing_keys = [], []
    result = template
    for key in all_keys:
        if key in variables:
            result = result.replace(f"{{{{{key}}}}}", variables[key])
            if key not in replaced_keys:
                replaced_keys.append(key)
        else:
            if key not in missing_keys:
                missing_keys.append(key)
    return RenderResult(text=result, replaced_keys=replaced_keys, missing_keys=missing_keys)


def echo(text: str) -> EchoResult:
    """原样返回输入文本，常用于调试、测试和透传场景。

    Args:
        text: 任意文本字符串。

    Returns:
        EchoResult:
            .text    原样返回的文本
            .length  文本字符数

    Example:
        >>> r = echo("hello")
        >>> r.text
        'hello'
        >>> r.length
        5
    """
    return EchoResult(text=text, length=len(text))
