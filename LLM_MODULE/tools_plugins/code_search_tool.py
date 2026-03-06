from __future__ import annotations
import json
from typing import Dict
from LLM_MODULE.tool_registry import ToolDefinition, ToolManager
from memory_module.code_search_agent import CodeSearchAgent


def register(manager: ToolManager) -> None:
    def execute_code_search_tool(arguments: Dict[str, object]) -> str:
        questions_arg = arguments.get("questions", [])
        if isinstance(questions_arg, str):
            questions = [questions_arg]
        else:
            questions = [str(q) for q in questions_arg if str(q).strip()]
        top_n = int(arguments.get("top_n", 5))
        candidate_pool = int(arguments.get("candidate_pool", 20))
        combine = bool(arguments.get("combine", False))
        if not questions:
            questions = ["请结合项目结构回答核心逻辑"]
        agent = CodeSearchAgent()
        if combine:
            payload = {
                "mode": "combined",
                "combined": agent.search(
                    questions=questions,
                    top_n=top_n,
                    candidate_pool=candidate_pool,
                ),
            }
        else:
            payload = {
                "mode": "separate",
                "separate": [
                    {
                        "question": question,
                        "result": agent.search(
                            questions=[question],
                            top_n=top_n,
                            candidate_pool=candidate_pool,
                        ),
                    }
                    for question in questions
                ],
            }
        return json.dumps(payload, ensure_ascii=False)

    # Register the tool
    manager.register_tool(
        ToolDefinition(
            name="code_search_tool",
            description="Search for relevant code blocks within the project based on natural language questions, returning candidate snippets and ranking results.",
            parameters={
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One or more natural language questions regarding the codebase.",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "The number of top relevant code blocks to return.",
                        "default": 5,
                    },
                    "candidate_pool": {
                        "type": "integer",
                        "description": "The size of the initial candidate pool for retrieval.",
                        "default": 20,
                    }
                },
                "required": ["questions"],
            },
            handler=execute_code_search_tool,
        ),
        toolsets=["memory_tools"],
    )
"""
    # Backward-compatible alias (older code/prompt may call it this way).
    manager.register_tool(
        ToolDefinition(
            name="code_search_agent",
            description="Alias of code_search_tool (backward-compatible).",
            parameters={
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One or more natural language questions regarding the codebase.",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "The number of top relevant code blocks to return.",
                        "default": 5,
                    },
                    "candidate_pool": {
                        "type": "integer",
                        "description": "The size of the initial candidate pool for retrieval.",
                        "default": 20,
                    },
                    "combine": {
                        "type": "boolean",
                        "description": "Whether to combine results into a single list.",
                        "default": False,
                    },
                },
                "required": ["questions"],
            },
            handler=execute_code_search_tool,
        ),
        toolsets=["memory_tools"],
    )
"""