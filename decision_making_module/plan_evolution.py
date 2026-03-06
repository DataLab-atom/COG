import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging
from dotenv import load_dotenv
load_dotenv()
# === 引入依赖 (保持你原本的 import 路径) ===
# 请确保这些模块在 Python 路径下
from .text_eval_agent import TextEvalAgent
from .text_agents import TextModifierAgent
from LLM_MODULE.llm import OpenAILLM  
from .decision_maker_prompts import final_sys_p 

@dataclass
class PlanContext:
    user_demand: str
    analysis_report: str
    extra_context: str

class PlanEvolutionModule:
    def __init__(self, cfg, project_root: Path):
        """
        初始化计划进化模块
        :param cfg: Hydra 配置对象
        :param project_root: 项目根目录 Path
        """
        self.cfg = cfg
        self.logger = logging.getLogger("PlanEvolution")
        self.project_root = project_root

        # 1. 模型配置
        self.modifier_model = cfg.models.text_agent.modifier
        self.judge_model = cfg.models.text_agent.llm_eval
        # 恢复：用于初始生成的模型 (通常和 modifier 一样，或者指定 gpt-4)
        self.draft_model = cfg.models.text_agent.modifier 
        
        # 2. 算法参数
        self.iters = cfg.parameters.text_eval.JUDGE_ITER
        self.population = cfg.parameters.text_eval.JUDGE_POPULATION

        self.refine_iters = getattr(cfg.parameters.text_eval, "REFINE_ITER", 1)
        self.refine_population = getattr(cfg.parameters.text_eval, "REFINE_POPULATION", 2)

        self.rubric_type = getattr(cfg.problems, "rubric_type", "general_ml_project")

        # 3. 归档路径
        task_name = cfg.problems.task_name
        self.demand_text = cfg.problems.demand
        self.archive_dir = self.project_root / "problems" / task_name / "decision_evolution_records"
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        ##dataset_report
        self.dataset_report = self.project_root / "problems"  / task_name/ "reports" / "data_analysis_report.md"
        
        self.logger.info(f"Initialized PlanEvolutionModule for task: {task_name}")

    def run(self, analysis_report: Optional[str] = None, extra_context: str = "", initial_plan: Optional[str] = None) -> str:
        """
        [主入口] 执行完整的计划生成与进化流程
        如果 initial_plan 为空，则调用原始逻辑生成初版，然后进入进化循环。
        """

        if analysis_report is None:
            # 确保文件存在，避免报错
            if os.path.exists(self.dataset_report):
                with open(self.dataset_report, "r", encoding="utf-8") as f:
                    analysis_report = f.read()
            else:
                analysis_report = "" # 或者根据需要抛出异常或处理默认值


        ctx = PlanContext(
            user_demand=self.demand_text,
            analysis_report=analysis_report,
            extra_context=extra_context
        )
        

        # 1. 恢复原有逻辑：如果没有传入 initial_plan，则自动生成
        if not initial_plan:
            self.logger.info("No initial plan provided. Generating draft using OpenAILLM...")
            initial_plan = self.generate_draft(ctx)
        
        # 2. 准备进化环境
        self.logger.info("Starting plan evolution...")
        task_input = self._build_task_input(initial_plan, ctx)
        rubric = self._build_rubric(self.rubric_type)
        
        # 3. 执行进化循环
        best_text, best_score = self._evolve_loop(
            implementation_doc=initial_plan,
            task_input=task_input,
            rubric=rubric,
            iters=self.iters,
            population=self.population,
            tag_prefix="init"
        )
        # 保存最终计划书到 data_analysis_report.md 同目录
        try:
            report_path = Path(self.dataset_report) if self.dataset_report else None
            output_dir = report_path.parent if report_path is not None else self.project_root
            output_dir.mkdir(parents=True, exist_ok=True)
            final_path = output_dir / "implementation_plan.md"
            with open(final_path, "w", encoding="utf-8") as f:
                f.write(best_text)
            self.logger.info(f"Saved final plan to: {final_path}")
        except Exception as e:
            self.logger.warning(f"Failed to save final plan: {e}")

        self.logger.info(f"Initial evolution finished. Final Score: {best_score:.2f}")
        return best_text

    def refine_plan(self, current_plan: str, feedback: str, ctx: PlanContext) -> str:
        """
        [新增功能] 根据 Verify 反馈修正计划 (Refinement Loop)
        """
        self.logger.warning("Refining plan based on verification feedback...")

        # 构造带有反馈的 Context
        feedback_section = (
            f"\n\n[CRITICAL EXECUTION FEEDBACK]\n"
            f"The code generated from the previous plan FAILED verification.\n"
            f"Error Logs / Feedback:\n{feedback}\n\n"
            f"**Refinement Directive**:\n"
            f"1. Modify the plan to address the specific errors above.\n"
            f"2. Do not remove essential logic; fix the configuration or strategy instead.\n"
            f"3. Ensure internal consistency."
        )
        
        task_input_with_feedback = self._build_task_input(current_plan, ctx) + feedback_section
        rubric = self._build_rubric(self.rubric_type)

        # 只进行短迭代修复
        best_text, best_score = self._evolve_loop(
            implementation_doc=current_plan,
            task_input=task_input_with_feedback,
            rubric=rubric,
            iters=self.refine_iters,            # <--- 动态配置
            population=self.refine_population,  # <--- 动态配置       
            tag_prefix="refine",
            force_suggestion=f"Fix the error described in logs: {feedback[:200]}..."
        )
        
        self.logger.info(f"Plan refined. New Score: {best_score:.2f}")
        return best_text

    # =========================================================================
    # 👇 恢复原始逻辑：生成初版计划 (generate_initial_plan)
    # =========================================================================
    def generate_draft(self, ctx: PlanContext) -> str:
        """使用 OpenAILLM 生成初始计划草稿"""
        messages = self._initial_plan_messages(ctx)
        # 使用 OpenAILLM 类 (保持和你原脚本一致的调用方式)
        # module_name 和 agent_name 可根据你的 LLM_MODULE 实际情况调整，或保持默认
        llm = OpenAILLM(
            model=self.draft_model, 
            module_name="decision_making_module", 
            agent_name="DecisionEvolutionPlanner"
        )
        return llm.chat(messages)

    def _initial_plan_messages(self, ctx: PlanContext) -> List[Dict[str, str]]:
        """构建生成初版计划的 Prompt"""
        demand = (ctx.user_demand or "").strip()
        analysis = (ctx.analysis_report or "").strip()
        extra = (ctx.extra_context or "").strip()
        
        user_content = (
            "[Original User Requirement]\n"
            f"{demand}\n\n"
            "[Data Analysis Report]\n"
            f"{analysis}\n"
            f"core metric is {self.cfg.problems.metric}"
        )
        if extra:
            user_content += "\n[Additional Context]\n" + extra + "\n"
        
        user_content += "\nBased on the above, output a single-path implementation blueprint (follow the specified Markdown structure)."
        
        return [
            {"role": "system", "content": final_sys_p},
            {"role": "user", "content": user_content},
        ]

    # =========================================================================
    # 👇 核心进化循环 (Logic preserved)
    # =========================================================================
    def _evolve_loop(
        self, 
        implementation_doc: str, 
        task_input: str, 
        rubric: Dict[str, str], 
        iters: int, 
        population: int,
        tag_prefix: str = "run",
        force_suggestion: str = None
    ) -> Tuple[str, float]:
        
        judge = TextEvalAgent(model_name=self.judge_model)
        modifier = TextModifierAgent(model_name=self.modifier_model)

        best_text = implementation_doc
        
        # Initial Evaluation
        ev = judge.evaluate(implementation_doc, rubric, task_input)
        best_score = float(ev.get("mean_score", 0.0))
        suggestions = ev.get("suggestions", [])
        
        if force_suggestion:
            suggestions.insert(0, force_suggestion)

        self._archive_candidate(best_text, 0, 0, best_score, f"{tag_prefix}_base")

        for iter_idx in range(iters):
            self.logger.info(f"Evolution {tag_prefix} - Iter {iter_idx+1}/{iters} (Current Best: {best_score:.2f})")
            
            # 本轮最佳追踪
            iter_best_text = None
            iter_best_score = -1.0
            iter_best_suggestions = []

            for cand_idx in range(max(1, population)):
                # Modify
                mod_result = modifier.modify(
                    current_text=best_text,
                    task_input=task_input,
                    rubric=rubric,
                    judge_suggestions=suggestions,
                    temperature=1.0,
                    use_judge_suggestions=True,
                )
                cand_text = mod_result.get("text", best_text)

                # Evaluate
                ev2 = judge.evaluate(cand_text, rubric, task_input)
                score2 = float(ev2.get("mean_score", 0.0))
                suggestions2 = ev2.get("suggestions", [])

                # Archive
                self._archive_candidate(
                    cand_text, iter_idx + 1, cand_idx + 1, score2, tag_prefix
                )

                if score2 > iter_best_score:
                    iter_best_score = score2
                    iter_best_text = cand_text
                    iter_best_suggestions = suggestions2

            # Selection Strategy: Greedy (Strict Improvement or Maintenance)
            if iter_best_text and iter_best_score >= best_score:
                best_text = iter_best_text
                best_score = iter_best_score
                suggestions = iter_best_suggestions
            else:
                # 即使没变好，也更新建议，防止死循环
                suggestions = iter_best_suggestions or suggestions

        return best_text, best_score

    # =========================================================================
    # 👇 辅助函数与完整 Rubric
    # =========================================================================
    def _build_task_input(self, implementation_doc: str, ctx: PlanContext) -> str:
        guidance = f"""
    You will polish a complete implementation blueprint document (Markdown).
    Your primary goal is to engineer a high-performance solution that maximizes quantitative metrics.

    [CRITICAL FORMAT CONSTRAINTS (MUST OBEY)]
    {final_sys_p}

    Strict requirements:
    - **Preserve Structure**: Keep the Markdown structure and headings as-is.
    - **Deterministic Technical Route**: Provide a single, definitive, end-to-end technical path. Do NOT offer options or placeholders.
    - **Data Pipeline Fit**: The data processing section must fuse concrete facts from BOTH the demand document and the analysis report—no templated preprocessing.
    - **Architecture Fit**: Model backbones must be tailored to dataset characteristics (sequence length, modality quirks, imbalance, etc.) rather than generic textbook choices.
    - **Training & Evaluation Customization**: Specify loss design, sampling, metrics, and validation splits that directly attack the stated data issues (e.g., long-tail, class overlap). Include concrete hyperparameters.
    - **Optimization**: Explicitly prescribe advanced, high-performance techniques suited to the task and push toward state-of-the-art results.
    - **Decisions Only**: Only output final implementation directives. Omit rationale or discussion.
    - **Directly Executable**: Use imperative, self-contained language so the document reads as a blueprint ready for immediate execution.
    - **No Visualization or Reporting Modules**: Do not introduce data visualization components or standalone statistical reporting modules.
    - **Internal Consistency**: Ensure every stage of the pipeline aligns logically with previous decisions—no conflicting steps.
"""
        ctx_blob_parts = []
        if ctx.user_demand:
            ctx_blob_parts.append(f"[Original Demand]\n{ctx.user_demand}")
        if ctx.analysis_report:
            ctx_blob_parts.append(f"[Data Analysis Report]\n{ctx.analysis_report}")
        if ctx.extra_context:
            ctx_blob_parts.append(f"[Additional Context]\n{ctx.extra_context}")
        
        ctx_blob = "\n\n".join(ctx_blob_parts).strip() or "(no additional context)"
        return f"Implementation document:\n{implementation_doc}\n\n{guidance}\n\nContext bundle:\n{ctx_blob}"

    def _archive_candidate(self, text: str, iter_idx: int, cand_idx: int, score: float, tag: str):
        if not self.archive_dir: return
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"iter{iter_idx:02d}_cand{cand_idx:02d}_score{score:.2f}_{tag}_{timestamp}.md"
        (self.archive_dir / filename).write_text(text.strip() + "\n", encoding="utf-8")

    def _build_rubric(self, problem_type: str) -> Dict[str, str]:
        rubric_key = (problem_type or "general_ml_project").strip().lower()

        # 1. Molecular Prediction (原样保留)
        molecular_prediction = {
            "criteria": (
                "The blueprint must provide a deterministic, high-performance solution tailored to specific data characteristics. Core requirements:\n"
                "**Data Adaptation**: Adjusts model architecture and training strategy based on modality, data scale, and distribution. Prohibits default configurations.\n"
                "**Architecture Determinism**: Provides a single-path SOTA architecture variant. No ambiguous alternatives.\n"
                "**Occam's Razor**: Selects the solution with the shortest logical path and lowest computational cost for the performance goal. Rejects ineffective modular stacking.\n"
                "**Engineering Depth**: Includes targeted architectural micro-adjustments, mathematical strategy improvements, and robustness design.\n"
                "**Direct Execution**: Provides conclusions and configuration details directly. Excludes theoretical argumentation."
            ),
            "score1_description": "Contains only broad technical terms. Fails to define a concrete model architecture or execution strategy. Cannot be implemented.",
            "score2_description": "Proposes a concrete model but uses unmodified default parameters and structures. Fails to address data distribution or modality constraints. Unusable for high-performance contexts.",
            "score3_description": "Contains technical flaws. Characterized by: mutually exclusive strategies; inclusion of complex modules irrelevant to the task (computational redundancy); or mismatch between architectural inductive bias and data modality.",
            "score4_description": "Logically self-consistent. Uses standard off-the-shelf industrial components. Lacks customization for specific data characteristics. Performance is baseline level with no competitive edge.",
            "score5_description": "Applies general optimization techniques (e.g., optimizer selection, basic regularization). Demonstrates basic professionalism but solves specific constraints using only generic tools. Lacks depth.",
            "score6_description": "Identifies the core data challenge and provides a specialized solution. The remainder of the system (architecture or augmentation) remains in standard configuration. Optimization is not system-wide.",
            "score7_description": "Modifies both model architecture and training strategy. The plan is complete and logical but relies on conventional methods. Lacks cutting-edge fine-tuning or optimization techniques.",
            "score8_description": "Meets all evaluation criteria. Includes targeted modifications to architecture structure, loss functions, and regularization mechanisms. Modifications directly address data constraints with no redundant steps. Achieves high performance.",
            "score9_description": "Reduces complexity based on Score 8. Replaces independent functional modules with mathematical improvements or parameter initialization. Minimizes code volume and computational load while maintaining equivalent performance.",
            "score10_description": "Achieves the optimal ratio of theoretical performance to computational cost. Contains no components that can be removed and requires no additional components. Represents the optimal path under the given constraints."
        }

        # 2. General ML Project (原样保留)
        general_ml_project = {
            "criteria": """            
    **Evaluation Objective**:
    Judge whether the blueprint provides a deterministic, single-path ML project design tied to the provided demand + data analysis bundle.

    **Key Evaluation Dimensions**:
    1. **Context Coupling**: References concrete dataset findings (missing values, imbalance, latency, etc.) in preprocessing and feature strategy.
    2. **Decisive Architecture**: Chooses one backbone and training graph with explicit hyperparameters; no placeholders.
    3. **Cohesive Training Loop**: Loss, optimizer, scheduler, and evaluation plan must support the declared objective and metrics.
    4. **Operational Readiness**: Output must be immediately actionable as a project plan without further deliberation.
    """,
            "score1_description": "Off-topic or empty buzzwords.",
            "score2_description": "Keeps multiple choices or fails to nail down training details.",
            "score3_description": "Conflicting techniques break the plan.",
            "score4_description": "Generic cookbook flow that ignores dataset-specific evidence.",
            "score5_description": "Solid but safe defaults with minimal tailoring.",
            "score6_description": "Clear linkage between dataset insights and at least one customized component.",
            "score7_description": "Several modules are optimized for the dataset while keeping lean execution.",
            "score8_description": "System-level synergy and advanced techniques yield a SOTA-ready plan.",
            "score9_description": "Micro-control of hyperparameters and training tricks demonstrates expert-level readiness.",
            "score10_description": "Flawless execution blueprint with exhaustive, data-justified decisions.",
        }

        # 3. Symbolic Regression (原样保留)
        symbolic_regression = {
            "criteria": """
    **Evaluation Objective**:
    Assess whether the blueprint designs a deterministic symbolic regression system that reliably discovers interpretable equations aligned with domain constraints.

    **Key Evaluation Dimensions**:
    1. **Search Space Engineering**:
       - Basis operators, constants, and grammar should derive from the demand + data report (units, invariances, noise profile).
       - Generic “use standard operators” plans score <=4.

    2. **Modeling Strategy**:
       - Specifies a single symbolic regression engine (e.g., neural-guided equation discovery, differentiable programming, genetic programming) with concrete hyperparameters and stopping criteria.

    3. **Regularization & Prior Injection**:
       - Details complexity penalties, sparsity enforcement, or physics-inspired constraints to prevent overfitting and ensure interpretability.

    4. **Evaluation Protocol**:
       - Requires deterministic validation focusing on extrapolation error, residual diagnostics, and symbolic simplicity; mere training error reporting is insufficient.
    """,
            "score1_description": "Unusable or hallucinated plan unrelated to symbolic regression.",
            "score2_description": "Ambiguous instructions or multiple competing search strategies with no final choice.",
            "score3_description": "Inconsistent pipeline (e.g., no link between search operators and evaluation).",
            "score4_description": "Template symbolic regression workflow without tailoring to the provided domain facts.",
            "score5_description": "Executable yet conservative plan lacking advanced priors or validation depth.",
            "score6_description": "Incorporates at least one domain-driven search space or regularization adjustment.",
            "score7_description": "Multiple components (search + regularization + evaluation) are co-designed for the task.",
            "score8_description": "State-of-the-art integration of neural and symbolic techniques with rigorous validation.",
            "score9_description": "Specifies fine-grained controls (curriculum, expression caching, staged simplification) grounded in data traits.",
            "score10_description": "Complete automation-ready spec covering search, pruning, validation, and reporting without gaps.",
        }

        # 4. Time Series Forecasting (原样保留)
        universal_problem_solving = {
    "criteria": """
**Evaluation Objective**:
Assess whether the blueprint delivers a deterministic, high-efficacy solution strictly tailored to the problem’s core constraints and context, while maintaining an **appropriate level of abstraction**. It must retain only high signal-to-noise ratio (SNR) information critical to understanding the architecture and executing core decisions, and eliminate all redundant low-level implementation details that can be autonomously determined by the implementer.

**Key Evaluation Dimensions**:
1.  **Contextual Binding**:
    - The solution must be directly derived from the problem’s core constraints, data characteristics, or business requirements.
    - Only context-adaptive details strongly correlated with architecture selection and strategy design shall be retained; reject the inclusion of scenario information irrelevant to the core logic (e.g., specific variable naming, temporary file path formats).
    - Rejects generic "one-size-fits-all" templates that ignore specific context.

2.  **Architectural Determinism**:
    - Must select a single, optimal core execution path, with clear responsibilities, interaction logic, and core algorithms/tools for key modules.
    - Critical parameters (e.g., random seed, core model hyperparameters, evaluation metrics) must be explicitly defined; while reasonable abstraction is allowed for non-critical implementation details (e.g., internal logic of helper functions, non-core code structure).
    - Rejects two extremes: ambiguous "try X or Y" suggestions, and over-prescription of line-by-line code-level implementation details.

3.  **Logical Efficiency & Signal-to-Noise Ratio**:
    - Every component must have a clear, justified purpose.
    - Penalizes unnecessary complexity, redundancy, or "feature bloat" (adding steps solely to appear complex).
    - **High SNR Mandate**: The content of the blueprint must be information essential to understanding the solution and executing core decisions. All low-level details that do not affect the core logic and can be determined autonomously by the implementer (e.g., specific logging formats, variable names for non-core functions, redundant code-level step descriptions) shall be eliminated.

4.  **Execution Readiness**:
    - The output must be an **architecture-level actionable playbook** that clearly defines "what to do (core steps)", "what to implement with (key tools/algorithms)", and "what the critical constraints are (core parameters/rules)".
    - Line-by-line code-level implementation guides are not required. The implementer shall be able to define the overall architecture and core decisions based on the blueprint, while autonomously determining specific code implementation details under the constraints of the blueprint.
    """,

    "score1_description": "Irrelevant content, hallucinations, or empty buzzwords. Fails to address the core problem.",
    "score2_description": "Either vague and indecisive (offers multiple potential paths without committing to a strategy), or **overloaded with redundant low-level implementation details** (core architecture is buried in code-level descriptions), making it impossible to directly grasp the critical execution path.",
    "score3_description": "Technically flawed or contradictory. The proposed steps are mutually exclusive or logically incoherent. Fundamental misunderstanding of the problem constraints, or detail overload leading to conflicting requirements that cannot be implemented.",
    "score4_description": "Either a generic 'Cookie-Cutter' approach that applies a standard textbook workflow while completely ignoring the specific nuances or constraints of the user's request, or **context-specific but overly verbose with low-level details**, lacking architecture-level abstraction, resulting in a lengthy and non-reusable blueprint.",
    "score5_description": "Safe but mediocre. Uses standard defaults and conservative strategies. Solves the basic problem but lacks optimization or insight, and has poor control over the level of abstraction—either slightly ambiguous or marginally redundant. A 'minimum viable product' level plan.",
    "score6_description": "Context-Aware with Reasonable Abstraction. Identifies key constraints and customizes at least one major component to address them, while **consciously controlling the granularity of details**—critical decisions are explicit, and non-critical details are kept concise. Moves beyond the generic template.",
    "score7_description": "Cohesive Strategy. The solution is logically sound and complete, with multiple components adapted to work in synergy. The critical execution path is clear. Meets good professional standards, but has minor room for optimization in detail trimming (a small amount of omissible non-critical descriptions remain), or relies on conventional methods without innovation.",
    "score8_description": "Highly Optimized & Targeted. Demonstrates deep insight into the problem, with core decisions directly targeting the most stringent constraints. **Exceptional detail control**—only critical information is retained, no redundant descriptions, and high performance/efficiency is achieved through specific, non-standard engineering decisions.",
    "score9_description": "Elegant Efficiency. Achieves Score 8 results with reduced complexity. **Eliminates all redundant steps and descriptions**, and uses advanced techniques (mathematical shortcuts, system design patterns) to maximize ROI. The blueprint is clear, concise, and easy to interpret.",
    "score10_description": "The Optimal Path. A flawless, surgically precise blueprint that perfectly balances **determinism and abstraction**. The critical execution path, core methods, and parameters are absolutely explicit with no ambiguity; meanwhile, there are no redundant low-level implementation details—every statement is essential to understanding and executing the solution. The architecture is clear, ready to directly guide implementation, and easy to maintain."
}

        rubric_map = {
            "molecular_prediction": molecular_prediction,
            "general_ml_project": general_ml_project,
            "symbolic_regression": symbolic_regression,
            "universal_problem_solving":universal_problem_solving
        }

        return rubric_map.get(rubric_key, general_ml_project)
