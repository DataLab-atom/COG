from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List, Dict

from LLM_MODULE.tool_registry import ToolDefinition, ToolManager, ToolArgs
from memory_module.runtime_utils import ADDITIONAL_CODE_EXTENSIONS, extract_r_code_with_structures
from memory_module.task_context import reference_code_root
from tree_cot.utils import extract_code_with_structures


def _read_text(file_path: Path) -> str:
    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return file_path.read_text(encoding="gbk", errors="ignore")


def _strip_code_fence(code: str) -> str:
    if not code:
        return ""
    stripped = code.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def fetch_code_blocks(
    project: str,
    file_path: str,
    symbol_names: list[str] | None = None,
    symbol_types: list[str] | None = None,
) -> str:
    """
    从给定的项目和文件路径中提取代码块。
    """
    project = project.strip()
    file_path = file_path.strip()
    
    if not project or not file_path:
        return json.dumps({"ok": False, "error": "missing project or file_path"}, ensure_ascii=False)

    normalized = file_path.replace("\\", "/").strip("/ ")
    repo_root = reference_code_root()
    project_dir = repo_root / project
    target_path = project_dir / Path(normalized)

    if not target_path.exists() or not target_path.is_file():
        return json.dumps({"ok": False, "error": f"file not found: {project}/{normalized}"}, ensure_ascii=False)

    suffix = target_path.suffix.lower()
    relative_path = os.path.relpath(target_path, project_dir).replace("\\", "/")

    # 处理纯文本扩展名
    if suffix in ADDITIONAL_CODE_EXTENSIONS:
        return json.dumps(
            {
                "ok": True,
                "blocks": [
                    {
                        "project": project,
                        "relative_path": relative_path,
                        "symbol_type": "text",
                        "symbol_name": target_path.stem,
                        "code": _read_text(target_path),
                    }
                ],
            },
            ensure_ascii=False,
        )

    # 提取结构化代码
    if suffix == ".r":
        entries = extract_r_code_with_structures(str(target_path), keep_last=True)
    else:
        entries = extract_code_with_structures(str(target_path), keep_last=True)

    wanted_names = {str(x) for x in (symbol_names or []) if str(x).strip()}
    wanted_types = {str(x) for x in (symbol_types or []) if str(x).strip()}

    blocks: List[Dict[str, Any]] = []
    for entry in entries:
        code_name = entry.get("code_name")
        code_type = entry.get("type")
        
        if wanted_names and code_name not in wanted_names:
            continue
        if wanted_types and code_type not in wanted_types:
            continue
            
        code_body = _strip_code_fence(str(entry.get("code_source") or ""))
        if not code_body:
            continue
            
        blocks.append(
            {
                "project": project,
                "relative_path": relative_path,
                "symbol_type": code_type or "unknown",
                "symbol_name": code_name or target_path.stem,
                "code": code_body,
            }
        )

    return json.dumps({"ok": True, "blocks": blocks}, ensure_ascii=False)


def register(manager: ToolManager) -> None:
    def _fetch_code_blocks_tool(args: ToolArgs) -> str:
        return fetch_code_blocks(
            project=args.get("project", ""),
            file_path=args.get("file_path", ""),
            symbol_names=args.get("symbol_names"),
            symbol_types=args.get("symbol_types"),
        )

    manager.register_tool(
        ToolDefinition(
            name="fetch_code_blocks",
            description="Extract complete class/function (or whole text) code blocks from a given project and file path under the task's reference code directory.",
            parameters={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project folder name under reference_paper_and_code/code.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path within the project (use '/' separators).",
                    },
                    "symbol_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional symbol names to extract; empty means all top-level symbols.",
                    },
                    "symbol_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["class", "function"]},
                        "description": "Optional symbol type filter.",
                    },
                },
                "required": ["project", "file_path"],
            },
            handler=_fetch_code_blocks_tool,
        ),
        toolsets=["code_search"],
    )