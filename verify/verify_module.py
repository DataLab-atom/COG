import json
import logging
import os
import subprocess
import time
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# --- 尝试导入核心模块，兼容 Pipeline 环境 ---
try:
    from LLM_MODULE.llm import OpenAILLM
    from LLM_MODULE.tools import (
        load_plugins, 
        make_tool_handler, 
        tool_spec, 
        dispatch_tool
    )
except ImportError:
    # 如果路径未设置，尝试添加父目录（仅用于本地调试时的fallback）
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from LLM_MODULE.llm import OpenAILLM
    from LLM_MODULE.tools import (
        load_plugins, 
        make_tool_handler, 
        tool_spec, 
        dispatch_tool
    )

try:
    from json_repair import repair_json  # type: ignore
except Exception:
    def repair_json(text: str) -> str:
        return text

@dataclass(frozen=True)
class _RunLog:
    returncode: int
    duration_sec: float
    stdout: str
    stderr: str

class VerifyModule:
    def __init__(self, cfg):
        """
        初始化验证模块
        :param cfg: Hydra/Omegaconf 配置对象，包含 problems.demand, problems.metric, models.verify 等
        :param project_root: 项目根目录路径 (pathlib.Path)
        """
        self.cfg = cfg
        self.logger = logging.getLogger("VerifyModule")
        
        # 从配置中读取模型名称
        # 假设 config 结构中 verify 模型定义在 cfg.models.verify
        self.model_name = getattr(cfg.models, "verify", "gpt-4o")
        self.timeout = cfg.parameters.verify.timeout
        problem_cfg = getattr(cfg, "problems", None) or getattr(cfg, "problem", None)
        self.task_name = getattr(problem_cfg, "task_name", None) or getattr(problem_cfg, "name", "")

    def _safe_json_loads(self, text: str) -> Any:
        text = (text or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass
        
        # 尝试提取 JSON 片段
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            snippet = text[start : end + 1]
            try:
                res = json.loads(snippet)
                # self.logger.debug("Parsed JSON via snippet extraction")
                return res
            except Exception:
                try:
                    res = json.loads(repair_json(snippet))
                    # self.logger.debug("Parsed JSON via json_repair")
                    return res
                except Exception as e:
                    self.logger.warning(f"JSON parsing failed: {e}")
                    return None
        return None

    def _internal_read_file(self, rel_path: str) -> str:
        """
        内部辅助函数：通过 tools 读取文件。
        注意：tool 的执行通常依赖当前工作目录，调用前需确保 cwd 正确。
        """
        self.logger.debug(f"Attempting to read file: {rel_path}")
        try:
            # 尝试使用工具读取
            result = dispatch_tool("read_file", {"file_path": rel_path})
            
            # 简单的错误检查 (假设工具返回 JSON 格式的错误信息)
            if result.strip().startswith("{"):
                try:
                    obj = json.loads(result)
                    if isinstance(obj, dict) and "error" in obj:
                        err_msg = obj['error']
                        self.logger.error(f"Read failed: {rel_path} -> {err_msg}")
                        return f"[Read Failed: {err_msg}]"
                except:
                    pass
            
            preview = result[:100].replace("\n", "\\n") + "..." if len(result) > 100 else result
            self.logger.debug(f"Read success (len={len(result)}): {preview}")
            return result
        except Exception as e:
            self.logger.warning(f"Tool read failed, falling back to os.read: {e}")
            # Fallback: 直接读取文件（防止工具系统未初始化）
            if os.path.exists(rel_path):
                with open(rel_path, 'r', encoding='utf-8') as f:
                    return f.read()
            return "[Read Failed]"

    def _run_entrypoint(self, output_dir: Path, entry_file: Path, timeout_sec: int) -> _RunLog:
        python_cmd = os.environ.get("PYTHON", "python")
        cmd = [python_cmd, str(entry_file)]
        
        self.logger.info(f"Executing: {' '.join(cmd)} (cwd={output_dir}, timeout={timeout_sec})")
        start = time.time()
        try:
            # 复制当前环境变量，确保 PYTHONPATH 等被传递
            env = os.environ.copy()
            
            proc = subprocess.run(
                cmd,
                cwd=str(output_dir),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env=env
            )
            duration = time.time() - start
            self.logger.info(f"Execution done. ReturnCode={proc.returncode}, Duration={duration:.2f}s")
            self.logger.debug(f"Stdout len: {len(proc.stdout)}, Stderr len: {len(proc.stderr)}")
            
            return _RunLog(
                returncode=int(proc.returncode),
                duration_sec=float(duration),
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
            )
        except subprocess.TimeoutExpired as e:
            duration = time.time() - start
            self.logger.error(f"Execution timed out ({timeout_sec}s).")
            return _RunLog(
                returncode=124,
                duration_sec=float(duration),
                stdout=e.stdout or "",
                stderr=(e.stderr or "") + "\n[verify] Execution Timed Out",
            )
        except Exception as e:
            duration = time.time() - start
            self.logger.exception("Unknown exception during execution")
            return _RunLog(
                returncode=125,
                duration_sec=float(duration),
                stdout="",
                stderr=f"[verify] Execution Exception: {type(e).__name__}: {e}",
            )

    def _get_run_log(self, output_dir: Path, entry_file: Path, timeout_sec: int) -> _RunLog:
        verify_dir = output_dir / ".verify"
        verify_dir.mkdir(parents=True, exist_ok=True)
        
        first_success = verify_dir / "first_success_run.json"
        latest = verify_dir / "latest_run.json"

        def _read_log(p: Path) -> Optional[_RunLog]:
            if not p.exists(): return None
            try:
                self.logger.debug(f"Reading cached log: {p}")
                d = json.loads(p.read_text(encoding="utf-8"))
                return _RunLog(**d)
            except Exception as e:
                self.logger.warning(f"Failed to read cached log {p}: {e}")
                return None

        log = _read_log(first_success)
        if log:
            self.logger.info("Found first_success_run.json, skipping re-run")
            return log
        
        log = _read_log(latest)
        if log:
            self.logger.info("Found latest_run.json, skipping re-run")
            return log

        self.logger.info("No cached logs found. Starting execution...")
        log = self._run_entrypoint(output_dir, entry_file, timeout_sec=timeout_sec)
        
        log_dict = {
            "returncode": log.returncode, "duration_sec": log.duration_sec,
            "stdout": log.stdout, "stderr": log.stderr
        }
        dump_str = json.dumps(log_dict, ensure_ascii=False, indent=2)
        latest.write_text(dump_str, encoding="utf-8")
        
        if log.returncode == 0:
            first_success.write_text(dump_str, encoding="utf-8")
            self.logger.info("Execution successful, saved to first_success_run.json")
        else:
            self.logger.warning(f"Execution failed (code={log.returncode}), saved to latest_run.json")
        
        return log

    def _llm_score_pure(self, llm: OpenAILLM, dim_name: str, system: str, user: str) -> Dict[str, Any]:
        """Basic scoring (No tools), with logs"""
        self.logger.info(f"--- Evaluating Dimension: {dim_name} ---")
        self.logger.debug(f"[Prompt System]: {system}")
        user_preview = user if len(user) < 500 else user[:500] + "\n...[TRUNCATED]..."
        self.logger.debug(f"[Prompt User]: {user_preview}")

        start_t = time.time()
        reply = llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        cost_t = time.time() - start_t
        
        self.logger.debug(f"[LLM Raw Reply] ({cost_t:.2f}s): {reply}")
        
        parsed = self._safe_json_loads(reply) or {}
        self.logger.info(f"[{dim_name}] Result: {parsed}")
        return parsed

    def run(self,project_path) -> Dict[str, Any]:
        """
        执行 Verify 逻辑的主入口。
        对应原脚本中的 verify() 函数，但由 Pipeline 调用。
        """
        # 1. 准备路径和配置
        # 在 Pipeline 中，代码通常生成在 project_root/problems/task_name 下
        out_dir = Path(project_path).resolve()
        
        if not out_dir.is_dir():
            self.logger.error(f"Task directory not found: {out_dir}")
            return {
                "is_passed": False, 
                "total_score": 0,
                "failure_reason": f"Task directory not found: {out_dir}"
            }

        entry_file = out_dir / "main.py"
        if not entry_file.exists():
             self.logger.critical(f"main.py missing: {entry_file}")
             return {
                 "is_passed": False, 
                 "total_score": 0,
                 "failure_reason": "main.py missing"
             }

        # 2. 从 Config 中提取上下文 (替代原来的 user_input)
        demand = self.cfg.problems.demand
        task_metric = self.cfg.problems.metric
        # task_name 已经作为参数传入

        self.logger.info(f"Initializing LLM, Model: {self.model_name}")
        llm = OpenAILLM(model=self.model_name, module_name="verify_module", agent_name="verifier")

        # 3. 获取运行日志 (Dim 1)
        run_log = self._get_run_log(out_dir, entry_file, self.timeout)
        stdout_stderr = (run_log.stdout or "") + "\n" + (run_log.stderr or "")

        # 4. 读取文件内容 (Dim 2, 3, 4)
        # 为了让 tools 能够正确通过相对路径读取文件，我们临时切换 cwd
        # 或者确保 tool 实现支持绝对路径。这里为了保险，临时切换。
        original_cwd = os.getcwd()
        os.chdir(out_dir)
        try:
            main_py_text = self._internal_read_file("main.py")
            project_tree_text = self._internal_read_file("project_tree.txt")
        finally:
            os.chdir(original_cwd)

        # --- Dimension 1: Convergence (20 pts) ---
        conv_res = self._llm_score_pure(
            llm, "Convergence",
            system="""You are a strict evaluator. Please output JSON: {"score": 0|20, "reason": "Reason in English"}
Rules:
1. If the `TASK_METRIC` in the training logs shows an improving trend (regardless of absolute value), give 20 points.
2. If there is no metric output or no improvement trend, give 0 points.""",
            user=f"TASK={self.task_name}, METRIC={task_metric}\n\n[Log Output]\n{stdout_stderr}"
        )

        # --- Dimension 2: Anti-Fake (20 pts) ---
        fake_res = self._llm_score_pure(
            llm, "Anti-Fake",
            system="""You are a code auditor. Please output JSON: {"score": 0|20, "reason": "Reason in English", "has_fake_metric": bool}
Rules:
1. Check `main.py` for "metric falsification" (e.g., hardcoded results, artificial addition of scores, faking test sets).
2. Give 20 points if no falsification found; give 0 points and set `has_fake_metric=true` if falsification is detected.""",
            user=f"METRIC={task_metric}\n\n[main.py]\n{main_py_text}"
        )

        # --- Dimension 3: Task Alignment (30 pts) ---
        align_res = self._llm_score_pure(
            llm, "TaskAlignment",
            system="""You are a task evaluator. Please output JSON: {"score": 0|30, "reason": "Reason in English"}
Rules:
1. Determine if the logic in `main.py` addresses the problem defined in `DEMAND`.
2. Give 30 points if aligned; give 0 points if irrelevant.""",
            user=f"DEMAND={demand}\n\n[main.py]\n{main_py_text}"
        )

        # --- Dimension 4: Implementation Integrity (30 pts) ---
        self.logger.info("--- Evaluating Dimension: Implementation Integrity (With Tools) ---")
        dim4_system = r"""
You are an implementation integrity auditor.
Your task is to verify if the project code contains "implementation hallucinations" (e.g., importing non-existent files, calling non-existent methods).

You have the tool:
- read_file: Read file content.

Process:
1. I will provide `project_tree.txt` and `main.py`.
2. Analyze imports and core calls in `main.py`.
3. **You MUST use the tool (read_file)** to verify if the referenced core files exist and if their content matches the usage.
4. Do not check all files; check  3-5 key files in the core logic chain.
5. Relative Path Requirement:
The file path used in the function call **must be a relative path**. Please extract only the portion strictly following the `output_programs` directory.
**Example:**
* **Original Absolute Path:** `C:\Users\17640\Desktop\HMACF_PRO_MAX\problems\longtail\output_programs\mini_list_2025-11-14\data\config.py`
* **Required Relative Path:** `mini_list_2025-11-14\data\config.py`

6. Output JSON scoring after verification.

Output Format: {"score": 0|30, "reason": "Reason in English", "has_hallucination": bool}
Scoring Rules: Give 30 points if implementation is real and valid; give 0 points if hallucinations are found.
"""
        dim4_messages = [
            {"role": "system", "content": dim4_system},
            {"role": "user", "content": (
                f"[project_tree.txt]\n{project_tree_text}\n\n"
                f"[main.py]\n{main_py_text}\n\n"
                "Please start the audit. If you find it necessary to verify the concrete implementation of a file, use the tool to read it."
            )}
        ]
        
        self.logger.debug("Entering Function Calling loop for Dim 4...")
        
        # Tool 调用同样需要在正确的目录下进行，以便 read_file 能找到相对路径
        raw_dim4_reply = ""
        os.chdir(out_dir)
        try:
            raw_dim4_reply = llm.chat_with_tools(
                messages=dim4_messages,
                tools=tool_spec(toolset="default"),
                tool_handler=make_tool_handler(toolset="default", enforce_policy=True),
            )
            self.logger.debug(f"[Dim 4 Raw Reply]: {raw_dim4_reply}")
        except Exception as e:
            self.logger.error("Dim 4 Function Call Error", exc_info=True)
            raw_dim4_reply = ""
        finally:
            os.chdir(original_cwd)

        integrity_res = self._safe_json_loads(raw_dim4_reply) or {}
        self.logger.info(f"[Implementation Integrity] Result: {integrity_res}")

        # ==========================================
        # Summary & Calculation
        # ==========================================
        def _score(obj, full): return int(obj.get("score")) if int(obj.get("score", 0)) == full else 0

        s1 = _score(conv_res, 20)
        s2 = _score(fake_res, 20)
        s3 = _score(align_res, 30) # Task Alignment
        s4 = _score(integrity_res, 30)
        
        is_fake = bool(fake_res.get("has_fake_metric"))
        is_misaligned = (s3 == 0) # Zero tolerance for misalignment

        total = s1 + s2 + s3 + s4
        is_passed = (total >= 60) and (not is_fake) and (not is_misaligned)
        
        failure_reason = None
        
        if is_fake:
            total = 0
            is_passed = False
            failure_reason = "Metric falsification detected. Immediate failure."
        elif is_misaligned:
            total = 0
            is_passed = False
            failure_reason = "Task logic is completely misaligned (Task Alignment Score is 0). Immediate failure."
        elif not is_passed:
            failure_reason = f"Total score {total} < 60. Details: Convergence({s1}), Anti-Fake({s2}), Alignment({s3}), Integrity({s4})"
        
        self.logger.info(f"=== Verification Ended. Total={total}, Passed={is_passed} ===")
        if failure_reason:
            self.logger.info(f"Failure Reason: {failure_reason}")

        return {
            "convergence_score": {"score": s1, "reason": str(conv_res.get("reason", ""))},
            "no_hallucination_score": {"score": s2, "reason": str(fake_res.get("reason", ""))},
            "task_alignment_score": {"score": s3, "reason": str(align_res.get("reason", ""))},
            "implementation_integrity_score": {"score": s4, "reason": str(integrity_res.get("reason", ""))},
            "total_score": total,
            "is_passed": is_passed,
            "failure_reason": failure_reason
        }
