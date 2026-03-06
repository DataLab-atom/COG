import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from decision_making_module.plan_evolution import PlanEvolutionModule

# ======================
# 硬编码配置（请按需修改）
# ======================
TASK_NAME = "photocats"
DEMAND = """  ```markdown
  Design a system that utilizes the given 11 computed molecular descriptors to discover explicit mathematical formulas related to the target property — hydrogen release performance (HER) of organic molecules — through symbolic regression.

  # Requirements:
  - No need to design visualization or reporting modules.
  - The symbolic regression algorithm must be implemented manually; do not directly call existing libraries or pre-made algorithms.
  - The depth should not be overly deep.
  - Wrap every mathematical operator in a robust function to handle invalid inputs, and ensure the fitness function penalizes any unstable results.
  
    *   **Data Example**:
        ```csv
        ID,CAS,SMILES,HER,EA*,EA,Sr,Δσ,H_CT,ΔD,E_eb,E_sol,E_b,S1-S0,S1-T1
        1, 2144-08-3,O=Cc1ccc(O)c(O)c1O,0,0.958740415,-2.932459585,0.481,0.711,1.412,2.3943,1.221483537,-0.399406019,-0.494217733,3.8912,0.597741266
        ```
  ```"""  # 必填：从 problems/<task>/... 复制需求文本
RUBRIC_TYPE = "universal_problem_solving"
METRIC = "nmse"

MODEL_MODIFIER = "gpt-5-mini"
MODEL_JUDGE = "gemini-3-pro-preview"

JUDGE_ITER = 2
JUDGE_POPULATION = 2
REFINE_ITER = 1
REFINE_POPULATION = 2

# 若不想读文件，直接把分析报告文本写在这里；否则保持 None
ANALYSIS_REPORT_TEXT = """
# Data Analysis Report on Molecular Descriptors and Hydrogen Evolution Rate (HER)

## 1. Macro Features
- The dataset comprises 668 samples and 15 columns, including identifiers (ID, CAS, SMILES), the target variable HER, and 11 computed molecular descriptors.
- Data types are appropriate: ID is integer; CAS and SMILES are string types; HER and all molecular descriptors are continuous floating-point variables.
- There are no missing values across any columns, ensuring data completeness.

## 2. Micro Insights (Statistical Description)
- **Target Variable (HER):** Exhibits a zero-inflated, highly right-skewed distribution (skewness = 5.98) with heavy positive kurtosis (42.10). The median HER is zero, with a maximum of 28.28, indicating a heavy-tailed distribution.
- **Molecular Descriptors:**
  - Most descriptors show approximately symmetric distributions with near-zero skewness and moderate kurtosis.
  - Exceptions include H_CT and ΔD, which are positively skewed (skewness 1.84 and 1.61 respectively) and leptokurtic, indicating the presence of extreme high values.
  - Sr and E_sol show moderate negative skewness (-0.23 and -1.34 respectively), with E_sol also exhibiting high kurtosis.
  - Several descriptors (EA*, EA, E_eb, E_sol, E_b) include negative values, highlighting the need for careful handling in modeling.

## 3. Salient Phenomena (Abnormalities and Relationships)
- **Outliers:**
  - HER has 129 samples exceeding the upper IQR fence, confirming its heavy-tailed nature.
  - Multiple descriptors exhibit outliers, notably EA*, EA, Δσ, H_CT, ΔD, E_eb, E_sol, E_b, and S1-S0, with both high and low extremes detected.
  - Sr shows no outliers, indicating a more stable distribution.
- **Correlation Structure:**
  - Linear Pearson correlations between HER and descriptors are weak (highest absolute value ~0.22), suggesting limited linear predictability.
  - Strong inter-descriptor correlations exist, e.g., EA* and EA (0.48), EA and S1-S0 (-0.71), Sr and S1-T1 (0.63), indicating multicollinearity.
  - Spearman rank correlations reveal stronger monotonic relationships between HER and descriptors such as S1-S0 (-0.33), EA (0.27), Sr (-0.21), and ΔD (0.17), implying potential nonlinear monotonic dependencies.

## 4. Descriptor Value Domains and Modeling Implications
- Descriptor ranges vary widely, e.g., EA spans -5.67 to 0.38, ΔD from 0 to 10.33, and H_CT from 0.52 to 5.64.
- Negative values and wide dynamic ranges necessitate robust mathematical operator implementations in symbolic regression to handle invalid inputs and ensure numerical stability.

---

This objective data analysis establishes a comprehensive understanding of the dataset's statistical properties, distributional characteristics, and inter-variable relationships. These findings provide a factual foundation for the subsequent development of a symbolic regression model tailored to capture complex, potentially nonlinear relationships between molecular descriptors and hydrogen evolution performance.

"""
ANALYSIS_REPORT_PATH = ROOT / "problems" / TASK_NAME / "reports" / "data_analysis_report.md"

# 可选：传入初始计划（None 表示自动生成草案）
INITIAL_PLAN = None
EXTRA_CONTEXT = ""


def build_cfg():
    models = SimpleNamespace(
        text_agent=SimpleNamespace(
            modifier=MODEL_MODIFIER,
            llm_eval=MODEL_JUDGE,
        )
    )

    parameters = SimpleNamespace(
        text_eval=SimpleNamespace(
            JUDGE_ITER=JUDGE_ITER,
            JUDGE_POPULATION=JUDGE_POPULATION,
            REFINE_ITER=REFINE_ITER,
            REFINE_POPULATION=REFINE_POPULATION,
        )
    )

    problems = SimpleNamespace(
        task_name=TASK_NAME,
        demand=DEMAND,
        metric=METRIC,
        rubric_type=RUBRIC_TYPE,
    )

    cfg = SimpleNamespace(
        models=models,
        parameters=parameters,
        problems=problems,
        problem=problems,
    )

    return cfg


def main():
    if not DEMAND or not DEMAND.strip():
        raise RuntimeError("请先在 test_plan_evolution.py 填写 DEMAND")

    if ANALYSIS_REPORT_TEXT is None:
        if not ANALYSIS_REPORT_PATH.is_file():
            raise FileNotFoundError(
                "缺少分析报告文件，请先生成或填写后再运行：\n"
                f"{ANALYSIS_REPORT_PATH}"
            )
        analysis_report = None  # 让模块自行读取 data_analysis_report.md
    else:
        analysis_report = ANALYSIS_REPORT_TEXT

    cfg = build_cfg()
    module = PlanEvolutionModule(cfg, ROOT)
    result = module.run(
        analysis_report=analysis_report,
        extra_context=EXTRA_CONTEXT,
        initial_plan=INITIAL_PLAN,
    )

    print("==== PlanEvolutionModule 运行完成 ====")
    print(result[:1000])


if __name__ == "__main__":
    main()
