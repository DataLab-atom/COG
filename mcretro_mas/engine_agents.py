import asyncio
import os
from typing import Any, Dict, List
import json
from mcretro_mas.engine_history import OptimizationProposal
from mcretro_mas.engine_logging import log_dialog, log_event

ENGINEER_SYS_PROMPT = """You are an expert-level Python algorithm engineer tasked with rewriting the given code snippet according to the user-provided optimization direction, while preserving the existing interface contract. Return only the new Python code snippet."""

def build_critic_agent_sys_prompt(task_metric: str, task_type: str, proposal_count: int = 3) -> str:
    example_modifications = []
    for i in range(1, proposal_count + 1):
        example_modifications.append({
            f"direction_{i}": "Upgrade Activation Function & Regularization" if i==1 else f"Strategy {i} description...",
            f"reasoning_{i}": "Currently using Tanh leads to vanishing gradients..." if i==1 else f"Reasoning {i}..."
        })

    raw_json_str = json.dumps(example_modifications, indent=8)
    escaped_json_str = raw_json_str.replace("{", "{{").replace("}", "}}") 

    return f"""
    # Role: You are an expert-level algorithm engineer
    **Objective:** Deeply analyze the project code to {task_type} the improvement of the evaluation metric **{task_metric}**.

    ## Core Workflow

    ### 1. Historical Review and Bottleneck Analysis
    *   **Log Analysis**: Carefully examine `optimization_history` and `project runtime logs` to learn from past experiences.
    *   **Avoid Local Optima**: If a function or class has been modified **multiple times** with stagnant metric improvements, it is considered to suffer from “diminishing marginal returns.” You **must** forcibly shift focus to under-explored modules (e.g., data preprocessing, feature engineering, loss function, or other model components).

    ### 2. Precise Targeting
    *   Based on the global code architecture, historical hotspots, and past lessons, identify **one most promising and under-explored** function or class.
    *   *Principle: Unless you are absolutely certain of a breakthrough improvement, repeated modifications to the same high-frequency file are strictly prohibited.*

    ### 3. Multidimensional Proposal Generation
    *   For the selected Target, devise **{proposal_count}** distinctly different and highly feasible optimization strategies.
    *   *Differentiation: Each proposal must be fundamentally different from all historical attempts.*

    ## Resources & Constraints
    *   **Hyperparameter Tuning**: Modifying hyperparameters in `main.py` is allowed, but **adjusting the number of epochs is strictly forbidden**. Explicitly specify the changes made.
    *   **Auxiliary Tools**:
        1.  The `read_file` function is used for you to read, understand the project code, and obtain relevant project information.
        2.  `data_analysis_report.md` and `project_architecture.json` contain data analysis and architecture design information regarding **this specific project** (if you wish to view these files, simply pass the filenames to the `read_file` function).
        3.  The code search tool and paper retrieval tool are used to retrieve external reference materials (e.g., state-of-the-art papers or algorithm implementations, which may or may not exist); these tools cannot access the internal code of this project. When analyzing the optimization history and identifying a performance bottleneck, you may invoke these two tools to seek external references for breakthrough ideas.

    ## Strict Output Standards
    **Once proposals are finalized, strictly adhere to the following rules:**

    1.  **Format Constraint**: Return **only a valid plain JSON string**.
    2.  **Content Prohibition**: **Do not** use Markdown code block markers (e.g., ```json), and **do not** include any explanatory text.
    3.  **Key Naming Convention**: Keys inside the `modifications` list **must** bear numbered suffixes (`_1`, `_2`, ..., `_{proposal_count}`).
    4.  **Single Target**: Each output must address improvements for **only one** Target.

    ### JSON Output Template
    {{
    "proposals": [
        {{
        "target_file": "src/model_definition.py",
        "target_type": "class or function",
        "target_name": "TimeSeriesPredictor",
        "modifications": {escaped_json_str}
        }}
    ]
    }}
"""
def compute_critic_temperature(
    stagnation_streak: int,
    base_temperature: float,
    increment_per_gen: float,
    min_temperature: float,
    max_temperature: float,
) -> float:
    extra = max(0, int(stagnation_streak) - 1)
    temperature = base_temperature + extra * increment_per_gen
    return float(min(max_temperature, max(min_temperature, temperature)))


def build_messages(
    tree_text: Any,
    history_prompt: str,
    baseline_log: str,
    demand: str,
    critic_system_prompt: str,
    stagnation_streak: int = 0,
) -> List[Dict[str, str]]:
    if isinstance(tree_text, list):
        tree_lines = len(tree_text)
        tree_str = "\n".join(str(x) for x in tree_text)
    else:
        tree_lines = None
        tree_str = "" if tree_text is None else str(tree_text)

    user_content = (
    "Below is the user's requirement:"
    + "\n\n"
    + demand
    + "\n\n"
    + "Below is the textual representation of the project tree:"
    + "\n\n"
    + f"```markdown\n{tree_str}\n```"
    + "\n\n"
    + "Below is the historical optimization trajectory of this project:"
    + "\n\n"
    + f"```markdown\n{history_prompt}\n```"
    + "\n\n"
    + "Below is the output from running the original project code this time:"
    + "\n\n"
    + f"```markdown\n{baseline_log}\n```"
)

    if stagnation_streak >= 5:
        user_content += (
            "\n\n"
            "[Stagnation Alert]\n"
            f"The current best solution has not been improved for {stagnation_streak} consecutive generations. To break this deadlock, strictly adhere to the following principles:\n"
            "1) Do not repeat the main optimization strategies or implementation patterns that have appeared in [Failed Lessons] and [Recent Trajectory];\n"
            "2) Prioritize new Targets (different files, functions, or modules) or propose structurally novel breakthroughs;\n"
            "3) The three proposed directions must be significantly distinct from each other, and clearly explain how each differs from past failures.\n"
        )

    messages = [
        {"role": "system", "content": critic_system_prompt},
        {"role": "user", "content": user_content},
    ]

    log_event(
        "build_messages",
        tree_lines=tree_lines,
        tree_chars=len(tree_str),
        history_chars=len(history_prompt),
        baseline_log_chars=len(baseline_log),
        messages_count=len(messages),
    )

    if not getattr(build_messages, "_logged_system", False):
        log_dialog("critic.system", critic_system_prompt)
        build_messages._logged_system = True

    log_dialog(
        "critic.user",
        user_content,
        tree_chars=len(tree_str),
        history_chars=len(history_prompt),
        baseline_log_chars=len(baseline_log),
    )

    return messages


class MonteCarloEngineerAgent:
    """并发生成多份候选代码的工程师智能体。"""

    def __init__(self, llm_client: Any, base_temperature: float = 0.8) -> None:
        self.llm = llm_client
        self.base_temperature = base_temperature

    async def generate_variants(self, proposal: OptimizationProposal, n: int = 5) -> List[str]:
        n = max(1, n)
        log_event(
            "engineer_generate_variants_start",
            n=n,
            target_file=proposal.target_file,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
            direction=proposal.direction,
        )
        tasks = [self._generate_single_variant(proposal, idx, n) for idx in range(n)]
        variants = await asyncio.gather(*tasks, return_exceptions=False)
        final_variants = [variant for variant in variants if variant]
        log_event(
            "engineer_generate_variants_done",
            requested_n=n,
            returned_n=len(final_variants),
            target_file=proposal.target_file,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
        )
        return final_variants

    async def _generate_single_variant(self, proposal: OptimizationProposal, idx: int, total: int) -> str:
        temp_min = float(os.environ.get("MCR_ENGINEER_TEMP_MIN", "0.4"))
        temp_max = float(os.environ.get("MCR_ENGINEER_TEMP_MAX", "1.2"))
        if temp_max < temp_min:
            temp_min, temp_max = temp_max, temp_min

        if total <= 1:
            temperature = min(temp_max, max(temp_min, float(self.base_temperature)))
        else:
            alpha = idx / (total - 1)
            temperature = temp_min + (temp_max - temp_min) * alpha
        messages = [
                    {"role": "system", "content": ENGINEER_SYS_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Target file: {proposal.target_file}\n"
                            f"Target type: {proposal.target_type}\n"
                            f"Target name: {proposal.target_name}\n"
                            f"Optimization direction: {proposal.direction}\n"
                            f"Reasoning hint: {proposal.reasoning}\n"
                            "Below is the current implementation:\n"
                            f"{proposal.original_code or '[Failed to automatically extract the original code]'}\n"
                            "Please output the complete replacement code, strictly wrapped in ```python ... ```."
                        ),
                    },
                ]

        loop = asyncio.get_running_loop()
        log_event(
            "engineer_variant_request",
            idx=idx,
            total=total,
            temperature=temperature,
            target_file=proposal.target_file,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
            direction=proposal.direction,
        )
        log_dialog("engineer.user", messages[1]["content"], idx=idx, total=total, temperature=temperature)
        response = await loop.run_in_executor(None, lambda: self.llm.chat(messages, temperature=temperature))
        log_dialog("engineer.assistant", response.strip(), idx=idx, total=total, temperature=temperature)
        log_event(
            "engineer_variant_response",
            idx=idx,
            total=total,
            temperature=temperature,
            response_chars=len(response or ""),
            target_file=proposal.target_file,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
        )
        return response.strip()

