"""
_base.py — 工具函数返回值基类

所有 utils 模块中的返回值均继承 ToolResult，提供统一的访问接口:

    result.some_field          # 直接属性访问
    result.model_dump()        # → dict
    result.model_dump_json()   # → JSON 字符串
    dict(result)               # → dict（同 model_dump）
"""

from pydantic import BaseModel


class ToolResult(BaseModel):
    """所有工具函数返回值的基类。

    继承 pydantic.BaseModel，字段值在构造时自动验证类型。
    子类只需声明字段，无需额外实现。

    Example:
        class MyResult(ToolResult):
            value: str
            count: int

        r = MyResult(value="hello", count=3)
        r.value            # "hello"
        r.model_dump()     # {"value": "hello", "count": 3}
    """

    model_config = {"frozen": True}  # 返回值不可变，避免意外修改
