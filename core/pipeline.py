import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, Tuple

from dotenv import load_dotenv

from dataset_report_module.core import Reporter, reporter_config
from decision_making_module.plan_evolution import PlanContext, PlanEvolutionModule
from tree_cot.initial_cot import TreeCoTModule
from verify.verify_module import VerifyModule

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _clip(text: Any, limit: int = 600) -> str:
    s = "" if text is None else str(text)
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"...(truncated, len={len(s)})"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return path.read_text(errors="replace")


def _get_problem_cfg(cfg: Any) -> Tuple[Any, str]:
    # 兼容历史写法：cfg.problems / cfg.problem
    problem_cfg = getattr(cfg, "problems", None) or getattr(cfg, "problem", None)
    if problem_cfg is None:
        raise AttributeError("配置缺少 `problems`（或兼容的 `problem`）节点，无法定位 task_name 等字段。")

    task_name = (
        getattr(problem_cfg, "task_name", None)
        or getattr(problem_cfg, "name", None)
        or getattr(cfg, "task_name", None)
    )
    if not task_name:
        raise AttributeError("配置缺少 `problems.task_name`（或兼容字段），无法确定任务名。")
    return problem_cfg, str(task_name)


class Pipeline:
    def __init__(self, cfg):
        self.cfg = cfg
        self.logger = logging.getLogger("Pipeline")
        self.repo_root = _REPO_ROOT

        problem_cfg, task_name = _get_problem_cfg(cfg)
        self.task_name = task_name

        self.logger.info(
            "Pipeline init | task=%s | cwd=%s | repo_root=%s",
            self.task_name,
            os.getcwd(),
            str(self.repo_root),
        )

        rep_conf = reporter_config(
            problem_name=self.task_name,
            dataset_dir=str(getattr(problem_cfg, "dataset_path", "")),
            demand=str(getattr(problem_cfg, "demand", "")),
            max_turns=int(getattr(getattr(cfg, "parameters", None).dataset_report, "max_turns", 4)),
            dataset_brief_info=str(getattr(problem_cfg, "brief_dataset_description", "")),
            sci_model=getattr(getattr(cfg, "models", None).dataset_report, "sci_model", None),
            temperature_sci=float(getattr(getattr(cfg, "models", None).dataset_report, "temperature_sci", 0.2)),
            coder_model=getattr(getattr(cfg, "models", None).dataset_report, "coder_model", None),
            temperature_coder=float(getattr(getattr(cfg, "models", None).dataset_report, "temperature_coder", 0.2)),
        )

        self.logger.info(
            "Reporter config | dataset=%s | max_turns=%s | sci_model=%s | coder_model=%s",
            rep_conf.dataset_dir,
            rep_conf.max_turns,
            rep_conf.sci_model,
            rep_conf.coder_model,
        )
        self.reporter = Reporter(config=rep_conf, workspace=self.repo_root)

        self.plan_evolver = PlanEvolutionModule(self.cfg, project_root=self.repo_root)
        self.tree_cot = TreeCoTModule(cfg=self.cfg, project_root=self.repo_root)

        self.logger.info("Initializing VerifyModule ...")
        self.verifier = VerifyModule(cfg=self.cfg)

        self.logger.info(
            "Pipeline initialized | task=%s | reporter_outputs=%s",
            self.task_name,
            str(self.reporter.outputs_dir),
        )

    def run(self) -> Optional[dict]:
        cfg = self.cfg
        problem_cfg, task_name = _get_problem_cfg(cfg)

        max_retry = int(getattr(getattr(cfg, "parameters", None), "global_max_retry", 1))
        extra_context = str(getattr(problem_cfg, "extra_context", "") or "")

        self.logger.info("Pipeline run start | task=%s | max_retry=%s", task_name, max_retry)

        # ------------------------------------------------------------------
        # 1) dataset_report
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        self.logger.info("[1/4] dataset_reporter start ...")
        try:
            analysis_report = self.reporter.run(verbose=True) or ""
        except Exception:
            self.logger.exception("[1/4] dataset_reporter failed")
            raise
        self.logger.info(
            "[1/4] dataset_reporter done | sec=%.2f | report_len=%s",
            time.perf_counter() - t0,
            len(analysis_report),
        )

        # ------------------------------------------------------------------
        # 2) decision(plan evolution) + 3) treecot + verify loop
        # ------------------------------------------------------------------
        current_plan: Optional[str] = None
        last_failure: str = ""
        last_output_dir: Optional[Path] = None
        last_verify: Optional[dict] = None

        for attempt in range(1, max(1, max_retry) + 1):
            self.logger.info("=== Try %s/%s ===", attempt, max_retry)

            if current_plan is None:
                self.logger.info("[2/4] plan_evolution generate start ...")
                t_plan = time.perf_counter()
                current_plan = self.plan_evolver.run(
                    analysis_report=analysis_report,
                    extra_context=extra_context,
                    initial_plan=None,
                )
                self.logger.info(
                    "[2/4] plan_evolution generate done | sec=%.2f | plan_len=%s | plan_preview=%s",
                    time.perf_counter() - t_plan,
                    len(current_plan or ""),
                    _clip(current_plan, 300),
                )
            else:
                self.logger.warning("[2/4] plan_evolution refine start | last_failure=%s", _clip(last_failure, 400))
                t_refine = time.perf_counter()
                ctx = PlanContext(
                    user_demand=str(getattr(problem_cfg, "demand", "")),
                    analysis_report=str(analysis_report or ""),
                    extra_context=extra_context,
                )
                current_plan = self.plan_evolver.refine_plan(current_plan=current_plan, feedback=last_failure, ctx=ctx)
                self.logger.info(
                    "[2/4] plan_evolution refine done | sec=%.2f | plan_len=%s | plan_preview=%s",
                    time.perf_counter() - t_refine,
                    len(current_plan or ""),
                    _clip(current_plan, 300),
                )

            self.logger.info("[3/4] TreeCoT start ...")
            t_tree = time.perf_counter()
            try:
                output_path = self.tree_cot.run(imply_doc=current_plan)
            except Exception:
                self.logger.exception("[3/4] TreeCoT failed")
                raise
            last_output_dir = Path(output_path).resolve()
            try:
                os.environ["HMACF_OUTPUT_DIR"] = str(last_output_dir)
                extra_dirs = [d for d in os.environ.get("READ_FILE_EXTRA_DIRS", "").split(os.pathsep) if d]
                if str(last_output_dir) not in extra_dirs:
                    extra_dirs.append(str(last_output_dir))
                    os.environ["READ_FILE_EXTRA_DIRS"] = os.pathsep.join(extra_dirs)
                self.logger.info("Updated READ_FILE_EXTRA_DIRS with output_dir=%s", str(last_output_dir))
            except Exception as e:
                self.logger.warning("Failed to update READ_FILE_EXTRA_DIRS: %s", e)
            self.logger.info(
                "[3/4] TreeCoT done | sec=%.2f | output_dir=%s",
                time.perf_counter() - t_tree,
                str(last_output_dir),
            )

            self.logger.info("[4/4] Verify start | timeout=%s", getattr(self.verifier, "timeout", None))
            t_verify = time.perf_counter()
            try:
                last_verify = self.verifier.run(last_output_dir)
            except Exception:
                self.logger.exception("[4/4] Verify failed (exception) | output_dir=%s", str(last_output_dir))
                raise
            self.logger.info(
                "[4/4] Verify done | sec=%.2f | passed=%s | total=%s",
                time.perf_counter() - t_verify,
                bool(last_verify.get("is_passed")) if isinstance(last_verify, dict) else None,
                last_verify.get("total_score") if isinstance(last_verify, dict) else None,
            )

            if isinstance(last_verify, dict) and last_verify.get("is_passed"):
                self.logger.info("Verify PASSED | attempt=%s | output_dir=%s", attempt, str(last_output_dir))
                break

            last_failure = str(last_verify.get("failure_reason", "")) if isinstance(last_verify, dict) else "Unknown failure"
            self.logger.warning("Verify FAILED | attempt=%s | reason=%s", attempt, _clip(last_failure, 800))

        if last_output_dir is None:
            self.logger.error("Pipeline aborted: TreeCoT 从未生成输出目录（last_output_dir=None）")
            return None

        if not (isinstance(last_verify, dict) and last_verify.get("is_passed")):
            self.logger.error("Verify failed after max retries. Skip MCR/auto_opt and end pipeline.")
            return {
                "task_name": task_name,
                "output_dir": str(last_output_dir),
                "verify": last_verify,
            }

        # ------------------------------------------------------------------
        # 5) mcretromas + (optional) auto_opt
        # ------------------------------------------------------------------
        try:
            from mcretro_mas.engine import main as run_mcr  # 局部导入：避免缺依赖时整个 pipeline 不能 import
        except Exception:
            self.logger.exception("无法导入 mcretro_mas.engine，跳过 MCR 阶段")
            run_mcr = None

        if run_mcr is not None:
            self.logger.info("MCR start | project_root=%s", str(last_output_dir))
            t_mcr = time.perf_counter()
            try:
                run_mcr(cfg=cfg, project_root=str(last_output_dir))
            except Exception:
                self.logger.exception("MCR failed | project_root=%s", str(last_output_dir))
                raise
            self.logger.info("MCR done | sec=%.2f", time.perf_counter() - t_mcr)

        auto_opt_enabled = False
        try:
            auto_opt_enabled = bool(cfg.parameters.mcr.auto_opt.AUTO_OPT_ENABLED)
        except Exception:
            auto_opt_enabled = False

        if auto_opt_enabled:
            try:
                from mcretro_mas.auto_opt import main as run_auto_opt  # 局部导入：同上
            except Exception:
                self.logger.exception("无法导入 mcretro_mas.auto_opt，跳过 auto_opt 阶段")
                run_auto_opt = None

            if run_auto_opt is None:
                self.logger.info("auto_opt skipped (import failed)")
            else:
                self.logger.info("auto_opt start | project_root=%s", str(last_output_dir))
                t_opt = time.perf_counter()
                try:
                    run_auto_opt(cfg=cfg, project_root=str(last_output_dir))
                except Exception:
                    self.logger.exception("auto_opt failed | project_root=%s", str(last_output_dir))
                    raise
                self.logger.info("auto_opt done | sec=%.2f", time.perf_counter() - t_opt)
        else:
            self.logger.info("auto_opt skipped (AUTO_OPT_ENABLED=False)")

        self.logger.info("Pipeline run finished | task=%s | output_dir=%s", task_name, str(last_output_dir))
        return {
            "task_name": task_name,
            "output_dir": str(last_output_dir),
            "verify": last_verify,
        }
