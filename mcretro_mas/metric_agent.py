import re

from LLM_MODULE.llm import OpenAILLM

_SYSTEM_PROMPT = """\
You are an expert Python programmer specializing in scientific computing and ML evaluation metrics.
Your task is to write a standalone Python module `metric_fn.py`.

Requirements:
- Define ONE function named `compute_metrics` that computes the requested evaluation metrics.
- The function must return a dict[str, float] whose keys are exactly the metric names specified by the user.
- Choose a sensible function signature based on the task (e.g. compute_metrics(y_true, y_pred) or compute_metrics(y_true, y_pred, model) etc.).
- Handle edge cases (NaN, infinity, division by zero) gracefully; never raise on bad inputs.
- Include a clear docstring: describe every parameter, what each metric measures, and an example call.
- Do NOT add any if __name__ == "__main__" block.
- Output ONLY raw Python code — no markdown fences, no explanation text."""


def infer_task_type(metric_description: str) -> str:
    """Infer optimization direction from metric description text.

    Returns "maximize" or "minimize" (default).
    """
    text = metric_description.lower()
    maximize_kws = ("higher", "maximize", "larger", "greater", "maximum", "increase")
    minimize_kws = ("lower", "minimize", "smaller", "less", "minimum", "decrease", "reduce")
    for kw in maximize_kws:
        if kw in text:
            return "maximize"
    for kw in minimize_kws:
        if kw in text:
            return "minimize"
    return "minimize"


def _user_prompt(demand: str, metric_description: str, dataset_description: str) -> str:
    return f"""\
Task demand:
{demand}

Dataset description:
{dataset_description}

Metric description (includes optimization direction):
{metric_description}

Write the complete `metric_fn.py` with a `compute_metrics` function returning {{"metric_name": float_value, ...}}.\
"""


def _clean_code(raw: str) -> str:
    match = re.search(r"```(?:python|py)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return raw.strip()


class MetricAgent:
    def __init__(self, cfg):
        model = getattr(getattr(cfg, "models", None), "metric_agent", "gpt-4.1-mini")
        self.llm = OpenAILLM(model=model, module_name="metric_agent", agent_name="metric_agent")

        problem_cfg = getattr(cfg, "problems", None) or getattr(cfg, "problem", None)
        self.metric_description = str(
            getattr(problem_cfg, "metric", "")
            or getattr(problem_cfg, "task_metric", "")
            or ""
        ).strip()
        self.demand = str(getattr(problem_cfg, "demand", ""))
        self.dataset_description = str(getattr(problem_cfg, "brief_dataset_description", ""))

    def run(self) -> str:
        """Call the LLM and return the content of metric_fn.py as a plain string."""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _user_prompt(
                    demand=self.demand,
                    metric_description=self.metric_description,
                    dataset_description=self.dataset_description,
                ),
            },
        ]
        raw = self.llm.chat(messages, temperature=0.2)
        return _clean_code(raw)
