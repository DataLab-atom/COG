import json
import os
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
load_dotenv()
from LLM_MODULE.llm import OpenAILLM
from LLM_MODULE.tools import tool_spec,make_tool_handler

class TextEvalAgent:
    """
    LLM 文本评估 Agent（支持 function call 工具），按给定 rubric 对文本打分并给出修改建议。
    """

    def __init__(self, model_name: str = "gpt-4.1-mini"):
        
        # 优先使用传入的参数，如果没传（None），再由调用方负责或给默认值
        self.model_name = model_name 

        self.llm = OpenAILLM(
            model=self.model_name,  # 👈 这里使用传入的参数
            agent_name="TextEvalAgent",
            module_name="decision_making_module",
        )

    def _build_messages(
        self, text: str, rubric: Dict[str, Any], task_input: Optional[str] = None
    ) -> List[Dict[str, str]]:
        rubric_json = json.dumps(rubric, ensure_ascii=False)
        system = (
            """You are a senior data scientist and technical review expert.
            Score the response using the provided rubric only. 
            The user will provide reference project code and papers; you may call tools to view them if you cannot provide better suggestions.
            You MAY call the provided tools only if it helps verify claims or gather missing context; otherwise, answer directly.
            Return STRICT JSON with keys: scores (int[]), mean_score (float), reasons (str[]), suggestions (str[]).
            Scores must be integers in [1,10]. Suggestions must be concrete and actionable. No extra text."""
        )
        task_part = "\n\nOriginal task/input:\n" + (task_input or "") if task_input is not None else ""
        user = (
            "Rubric JSON:\n"
            + rubric_json
            + task_part
            + "\n\nResponse to evaluate (plain text):\n"
            + text
            + "\n\nRules:\n"
            + "- If rubric provides a single criterion with 1..10 descriptions, output a single score in 'scores'.\n"
            + "- Base your reasons on mismatches between response and the rubric's level descriptions.\n"
            + "- [FORMAT POLICE]: Check the 'Response to evaluate' against the format constraints in the 'Original task/input' (e.g., Max 4 modules, NO code, NO specific variable names). If the response violates THESE FORMAT RULES, you MUST deduct points heavily (score <= 4) regardless of technical depth, and explicitly state 'Format Violation' in reasons and suggestions.\n"
            + "- Suggestions should directly guide revision to reach higher score levels while maintaining the strict format.\n"
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    @staticmethod
    def _safe_parse_json(text: str) -> Dict[str, Any]:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def evaluate(self, text: str, rubric: Dict[str, Any], task_input: Optional[str] = None) -> Dict[str, Any]:
        messages = self._build_messages(text, rubric, task_input)

        res = self.llm.chat_with_tools(
            messages,
            tools=tool_spec(toolset="memory_tools"),
            tool_handler=make_tool_handler(toolset="memory_tools", enforce_policy=True),
            need_append_msg= False,
            temperature=0.0
        )

        data = self._safe_parse_json(res)

        scores = data.get("scores")
        if not isinstance(scores, list) or not scores:
            scores = [0]

        norm_scores: List[int] = []
        for s in scores:
            try:
                si = int(s)
            except Exception:
                si = 0
            si = max(0, min(10, si))
            norm_scores.append(si)

        reasons = data.get("reasons")
        if not isinstance(reasons, list):
            reasons = [str(reasons)] if reasons is not None else []

        suggestions = data.get("suggestions")
        if not isinstance(suggestions, list):
            suggestions = [str(suggestions)] if suggestions is not None else []

        mean_score = sum(norm_scores) / len(norm_scores) if norm_scores else 0.0
        mean_score = round(float(mean_score), 2)

        return {
            "scores": norm_scores,
            "mean_score": mean_score,
            "reasons": reasons,
            "suggestions": suggestions,
            "raw": res
        }
