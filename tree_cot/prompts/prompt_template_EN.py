# 移除 from user_input import ...
from typing import Any

def first_step_prompt(
    implementation_doc: str, 
    code_structure: str, 
    *, 
    demand: str  # <--- 新增参数：从外部传入 demand
) -> str:
    # 使用传入的 demand 参数，而不是全局变量 DEMAND
    prompt = f"{demand}\n"

    paper_prompt_str = (
        "implementation_plan document:\n"
        f"```markdown\n{implementation_doc}\n```\n"
    )

    structure_prompt_str = (
        "Information and Instructions(May be empty):\n"
        f"{code_structure}\n\n"
        "Please generate the first root node for solving the problem based on the tasks and requirements above.\n"
    )

    return "\n\n".join([prompt, paper_prompt_str, structure_prompt_str])


def dev_doc_first_prompt(
    currentnode: str,
    description: str,
    *,
    structure: str,
) -> str:
    # 这个函数原本就没有依赖 user_input，保持不变
    return (
        f"Here is the description for `{currentnode}`:\n"
        f"{description}\n\n"
        "# I will provide you a dataset introduction, and the dataset file for your reference.\n"
        "# The use of virtual assessments is absolutely prohibited\n\n"
        "# This is the complete design architecture. Please consider the direction of data flow when writing the documentation.\n"
        f"```json\n{structure}\n```\n\n"
        "# This project's relevant files(You can call the function directly to view it):\n"
        "data_analysis_report.md, implementation_plan.md\n\n"
        "Please write a development documentation for it.\n"
    )


def generate_first_code_prompt(
    *, 
    dev_doc: str, 
    structure: str, 
    dataset_path: str # <--- 新增参数：从外部传入数据集路径
) -> str:
    return (
        "# The following is the original task architecture tree. Each development document corresponds to a leaf node. "
        "Please consider the data flow during implementation.\n"
        f"```json\n{structure}\n```\n\n"
        "# Please strictly follow the structured development document below to implement it as code and generate test code for it.\n"
        f"```markdown\n{dev_doc}\n```\n\n"
        "# This project's relevant files(You can call the function directly to view it):\n"
        "data_analysis_report.md, implementation_plan.md\n\n"
        # 使用传入的 dataset_path 参数，而不是全局变量 DATASET_PATH
        f"# Dataset folder path: `{dataset_path}`\n"
        "# The use of virtual assessments is absolutely prohibited\n\n"
        "# Test code requirements:\n"
        "- Please write the test code under `if __name__ == \"__main__\":`. Do not include error-throwing mechanisms.\n"
        "- Please use the real data I provided for testing.\n"
    )