import os
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
load_dotenv()
from LLM_MODULE.llm import OpenAILLM


class TextModifierAgent:
    def __init__(self, system_prompt: Optional[str] = None, model_name: Optional[str] = None):
        default_sys = (
            "You are an expert writing editor. Revise the given text to better satisfy the rubric and suggestions. "
            "Preserve factual correctness, avoid hallucinations, keep consistent tone and structure. "
            "Return ONLY the revised text."
        )
        self.system_prompt = system_prompt or default_sys
        self.llm = OpenAILLM(
            model= model_name or os.getenv("TextModifierAgent_model_name", "gpt-4.1-mini"),
            agent_name="TextModifierAgent",
            module_name="decision_making_module",
        )

    def modify(
        self,
        current_text: str,
        task_input: str,
        rubric: Dict,
        judge_suggestions: List[str],
        temperature: float = 0.5,
        use_judge_suggestions: bool = True,
    ) -> Dict[str, Any]:
        if use_judge_suggestions and (judge_suggestions or []):
            sug = "\n".join(f"- {s}" for s in (judge_suggestions or []))
            user = (
                "Task input (Contains STRICT format constraints and context):\n"
                + task_input
                + "\n\nRubric (JSON):\n"
                + str(rubric)
                + "\n\nCurrent text:\n"
                + current_text
                + "\n\nRevise the text. Strict rules:\n- Address the suggestions below.\n- Improve to reach higher rubric level(s).\n- [CRITICAL] You MUST NOT violate the format constraints defined in the Task input (e.g., Max 4 modules, NO code blocks). If a suggestion requires breaking the format, you MUST adapt the suggestion to fit within the allowed structure.\n- Do not add external unverifiable facts.\n- Return ONLY revised text.\n\nSuggestions:\n"
                + sug
            )
        else:
            user = (
                "Task input (Contains STRICT format constraints and context):\n"
                + task_input
                + "\n\nRubric (JSON):\n"
                + str(rubric)
                + "\n\nCurrent text:\n"
                + current_text
                + "\n\nRevise the text to better satisfy the rubric without using judge suggestions.\n"
                + "Strict rules:\n"
                + "- [CRITICAL] You MUST NOT violate the format constraints defined in the Task input (e.g., Max 4 modules, NO code blocks). If a suggestion requires breaking the format, you MUST adapt the suggestion to fit within the allowed structure.\n"
                + "- Make a direct mutation (e.g., refine structure, reasoning depth, style, or specificity) while keeping factual correctness.\n"
                + "- Do not add external unverifiable facts.\n"
                + "- Return ONLY revised text."
            )


        messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user}]
        resp = self.llm.chat(messages=messages, temperature=temperature)

        return {"text": resp}
