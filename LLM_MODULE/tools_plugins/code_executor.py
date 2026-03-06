from __future__ import annotations

import io
import json
import os
import re
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Any, Optional

# 假设这些是你项目里的模块，保留原样
from LLM_MODULE.tool_registry import ToolDefinition, ToolManager, ToolArgs

@dataclass
class ExecResult:
    success: bool
    stdout: str
    stderr: str
    error: Optional[str] = None
    exit_code: Optional[int] = None

    def to_json_str(self) -> str:
        """将结果转换为 JSON 字符串"""
        return json.dumps(asdict(self), ensure_ascii=False)

def _clean_code_markdown(code: str) -> str:
    """
    清洗代码中的 Markdown 标记（例如 ```python ... ```）
    """
    if not code:
        return ""
    # 移除开头的 ```python 或 ```
    code = re.sub(r"^```(python)?\s*", "", code.strip(), flags=re.IGNORECASE)
    # 移除结尾的 ```
    code = re.sub(r"\s*```$", "", code)
    return code.strip()

def code_executer(code: str, cwd: str | Path | None = None) -> str:
    """
    在本进程中执行 Python 代码，捕获 stdout/stderr，返回 JSON 字符串。
    """
    # 1. 清洗代码（防止 LLM 传入 Markdown 格式）
    code = _clean_code_markdown(code)
    
    # 2. 处理工作目录
    if cwd:
        cwd_path = Path(cwd)
        if not cwd_path.exists():
            return ExecResult(
                success=False, stdout="", stderr="", 
                error=f"指定的工作目录不存在: {cwd_path}", exit_code=-1
            ).to_json_str()
    else:
        cwd_path = Path(os.getcwd())

    f_stdout, f_stderr = io.StringIO(), io.StringIO()
    
    # 3. 准备执行环境
    # 复制当前的全局变量可能导致污染，通常新建一个干净的字典比较安全
    # 但为了让代码能用一些基础库，可以视情况预导入
    local_vars: Dict[str, Any] = {}
    global_vars: Dict[str, Any] = {
        "__name__": "__main__",
        "__file__": str(cwd_path / "<inline_script>"),
        # 如果需要，可以在这里预注入 pandas 等常用库，或者让 LLM 自己 import
    }

    prev_cwd = os.getcwd()
    
    try:
        # 切换目录
        os.chdir(cwd_path)
        
        # 4. 执行代码并捕获输出
        with redirect_stdout(f_stdout), redirect_stderr(f_stderr):
            # compile 能够提供更好的错误堆栈信息
            bytecode = compile(code, "<inline_script>", "exec")
            exec(bytecode, global_vars, local_vars)
            
        return ExecResult(
            success=True,
            stdout=f_stdout.getvalue(),
            stderr=f_stderr.getvalue(),
            error=None,
            exit_code=0,
        ).to_json_str()

    except SystemExit as e:
        # 捕获 sys.exit()
        return ExecResult(
            success=(e.code == 0 or e.code is None),
            stdout=f_stdout.getvalue(),
            stderr=f_stderr.getvalue(),
            error=str(e),
            exit_code=int(e.code) if isinstance(e.code, int) else 0,
        ).to_json_str()

    except Exception as e:
        # 捕获运行时异常
        tb = traceback.format_exc()
        return ExecResult(
            success=False,
            stdout=f_stdout.getvalue(),
            stderr=f_stderr.getvalue() + "\n" + tb, # 将堆栈信息放入 stderr
            error=str(e),
            exit_code=-1,
        ).to_json_str()
        
    finally:
        # 务必切回原目录，否则会影响主进程后续逻辑
        os.chdir(prev_cwd)


def register(manager: ToolManager) -> None:
    def _code_executer_tool(args: ToolArgs) -> str:
        # 这里加个容错，防止 args 里的 code 为 None
        code = args.get("code")
        if not code:
            return ExecResult(
                success=False, stdout="", stderr="Error: No code provided in arguments.", 
                exit_code=-1
            ).to_json_str()
            
        return code_executer(code, cwd=args.get("cwd"))

    manager.register_tool(
        ToolDefinition(
            name="code_executer",
            description=(
                "Executes Python code safely. "
                "Returns a JSON string with keys: 'success', 'stdout', 'stderr', 'error'. "
                "Suitable for data analysis, math, or file operations."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string", 
                        "description": "The Python code. Markdown tags like ```python are allowed and will be stripped."
                    },
                    "cwd": {
                        "type": "string", 
                        "description": "Optional working directory path."
                    },
                },
                "required": ["code"],
            },
            handler=_code_executer_tool,
        ),
        toolsets=["dataset_coder"]
    )