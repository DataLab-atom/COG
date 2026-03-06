from __future__ import annotations
from typing import  Dict, Iterable, List
from memory_module.paper_search_agent import PaperSearchAgent
from LLM_MODULE.tool_registry import ToolDefinition, ToolManager

def register(manager: ToolManager) -> None:
    def paper_search_tool(payload: Dict[str, Iterable[str]]) -> Dict[str, List[Dict[str, str]]]:
        questions = payload.get("questions") or []
        if isinstance(questions, str):
            questions = [questions]
        language = payload.get("language", "en")
        agent = PaperSearchAgent()
        return agent.answer(list(questions), language=language)

    # Register the tool to the manager
    manager.register_tool(
        ToolDefinition(
            name="paper_search_tool",
            description="Given one or more questions, read the user-provided relevant papers and output detailed answers with citations.",
            parameters={
                "type": "object",
                "properties": {
                    "questions": {
                        "description": "The question(s) to be answered: either a string or an array of strings.",
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ]
                    },
                    "language": {
                        "type": "string",
                        "description": "The language of the response, default is 'en'."
                    }
                },
                "required": ["questions"]
            },
            handler=paper_search_tool,
        ),
        toolsets=["memory_tools"],
    )