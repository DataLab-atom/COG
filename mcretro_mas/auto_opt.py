from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

# 引入配置类型提示
from omegaconf import DictConfig
from dotenv import load_dotenv

from mcretro_mas.metric_agent import infer_task_type


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent

def _load_env_from_dotenv() -> None:
    env_path = _repo_root() / ".env"
    if not env_path.exists():
        return
    try:
        load_dotenv(env_path)
    except ImportError:
        pass

def _ensure_optuna_installed() -> None:
    try:
        import optuna  # noqa: F401
    except ImportError:
        print("[auto_opt] Optuna not found, installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "optuna>=3.0.0"])

def _import_llm_or_raise():
    try:
        sys.path.append(str(_repo_root()))
        from LLM_MODULE.llm import OpenAILLM
        return OpenAILLM
    except ImportError as e:
        raise ImportError(f"[auto_opt] Critical: LLM_MODULE not found. {e}")

def _locate_main_py(project_root: Path) -> Path:
    cand = project_root / "main.py"
    if cand.exists(): return cand
    for p in project_root.rglob("main.py"): return p
    raise FileNotFoundError(f"Cannot find main.py in {project_root}")

def _generate_optuna_script(
    main_code: str, 
    mode: str, 
    task_type: str, 
    trials: int, 
    demand: str, 
    task_metric: str,
    llm_model: str,
    llm_temperature: float
) -> str:
    """让 LLM 将任意 Python 脚本重构为 Optuna 调优脚本"""
    OpenAILLM = _import_llm_or_raise()
    _load_env_from_dotenv()
    
    print(f"[auto_opt] Using LLM: {llm_model} | Mode: {mode} | Task: {task_type} | Metric: {task_metric}")

    llm = OpenAILLM(model=llm_model, module_name="MCRetroMAS", agent_name="auto_opt")
    
    direction = "maximize" if "max" in task_type.lower() else "minimize"

    system_prompt = (
        "You are an expert Python Refactoring & Optimization Specialist. "
        "Your goal is to convert a user's static script into a dynamic Optuna hyperparameter optimization script. "
        "You must align the optimization logic strictly with the USER DEMAND and TARGET METRIC."
    )

    user_prompt = f"""
    **Context Info:**
    1. **User Demand (Original Goal):** "{demand}"
        *(Use this to understand the logic, constraints, and physical meaning of parameters.)*

    2. **Target Metric (Core KPI):**
        "{task_metric}"
        *(The optimization MUST focus on improving this specific metric.)*
    
    3. **Source Code (`main.py`):**
    ```python
    {main_code}
    ```

    **Your Task:**
    Rewrite the source code into a standalone script that uses `optuna` to optimize its parameters.

    **Requirements:**
    1. **Function Wrapping**: Encapsulate the main logic into `def objective(trial):`.
    
    2. **Smart Parameter Identification**: 
        - Analyze `User Demand` and `Source Code` to find impactful variables.
        - **Context-Aware Ranges**: Use the semantics from `User Demand` to set realistic boundaries.
        - Replace constants with `trial.suggest_float`, `trial.suggest_int`, etc.
        - **Tuning Mode**: **{mode}** (Fast=narrow/safe range, Aggressive=wide/exploratory range).
    
    3. **Return Metric**: 
        - Locate the calculation of **"{task_metric}"** in the code.
        - The `objective` function MUST return this value as a float.
        - If the code calculates multiple stats, ensure the return value corresponds to **{task_metric}**.
    
    4. **Execution Block**: Add `if __name__ == "__main__":`:
        - Create Study: `optuna.create_study(direction="{direction}")`.
        - Run Optimization: `study.optimize(objective, n_trials={trials})`.
        - **JSON Persistence**: Save results to `opt_results.json`:
          `{{'best_params': study.best_params, 'best_value': study.best_value, 'metric': '{task_metric}'}}`
    
    5. **Cleanliness**: Ensure loops are finite (replace `while True` with fixed steps if needed).
    
    **Output:**
    Return ONLY the valid Python code.
    """

    content = llm.chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], temperature=llm_temperature)

    # 提取代码
    match = re.search(r"```python\s*([\s\S]*?)\s*```", content)
    if match: return match.group(1)
    if "def objective" in content and "import optuna" in content: return content.replace("```", "")
    
    raise RuntimeError("LLM failed to generate valid Python code.")

def main(cfg: DictConfig, project_root: str) -> int:
    """
    Auto Optimizer 主入口，由 Config 驱动
    :param cfg: Hydra/OmegaConf 配置对象
    :param project_root: 目标项目的根目录路径
    """
    _load_env_from_dotenv()
    _ensure_optuna_installed()
    
    # 1. 提取配置
    try:
        # 路径解析
        proj_path = Path(project_root).resolve()
        
        # 功能开关
        enabled = cfg.parameters.mcr.auto_opt.AUTO_OPT_ENABLED
        if not enabled:
            print("[auto_opt] Disabled in config. Skipping.")
            return 0

        # 核心参数
        raw_mode = cfg.parameters.mcr.auto_opt.AUTO_OPT_MODE
        mode_map = {"激进": "Aggressive", "平衡": "Balanced", "快速": "Fast"}
        mode = mode_map.get(raw_mode, raw_mode)
        
        task_metric = cfg.problems.metric
        task_type = infer_task_type(task_metric)
        demand = cfg.problems.demand
        
        # LLM 参数
        llm_model = cfg.models.mcr.auto_opt
        llm_temp = cfg.parameters.mcr.auto_opt.temperature

        # 确定 Trials 次数 (如果在 cfg 中有定义 trials 可以直接取，否则根据模式推断)
        # 这里暂时保留根据 mode 推断的逻辑，也可以加到 config parameters 中
        if mode == "Fast":
            trials = 20
        elif mode == "Aggressive":
            trials = 100
        else:
            trials = 50

    except Exception as e:
        print(f"[auto_opt] Config Extraction Error: {e}")
        return 1

    try:
        main_py = _locate_main_py(proj_path)
        print(f"[auto_opt] Targeting: {main_py}")
        
        # 2. 生成优化脚本
        gen_code = _generate_optuna_script(
            main_code=main_py.read_text("utf-8"),
            mode=mode,
            task_type=task_type,
            trials=trials,
            demand=demand,
            task_metric=task_metric,
            llm_model=llm_model,
            llm_temperature=llm_temp
        )
        
        # 3. 写入文件
        gen_file = proj_path / "main_opt_generated.py"
        gen_file.write_text(gen_code, "utf-8")
        print(f"[auto_opt] Script generated: {gen_file.name}")

        # 4. 运行优化
        print("[auto_opt] Starting optimization...")
        # 注意：这里在 project_root 目录下运行
        proc = subprocess.run([sys.executable, str(gen_file)], cwd=str(proj_path))
        
        if proc.returncode != 0:
            print("[auto_opt] Optimization process failed.")
            return 1
            
        # 5. 验证结果
        res_file = proj_path / "opt_results.json"
        if res_file.exists():
            data = json.loads(res_file.read_text("utf-8"))
            print(f"\n[auto_opt] DONE. Best Value: {data.get('best_value')}")
            print(f"[auto_opt] Saved to: {res_file}")
        else:
            print("[auto_opt] Warning: No result JSON found.")

    except Exception as e:
        print(f"[auto_opt] Execution Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

if __name__ == "__main__":
    print("Please run this module via the Pipeline using configuration.")