from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

# --- Logging Configuration ---
# Log file path: current_dir/verify_execution.log
_LOG_FILE = Path(__file__).resolve().parent / "verify_execution.log"

def setup_logging():
    """Configure logging: DEBUG to file, INFO to stderr"""
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    
    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(funcName)s:%(lineno)d] %(message)s',
        datefmt='%H:%M:%S'
    )

    # 1. File Handler (Detailed, DEBUG)
    file_handler = logging.FileHandler(_LOG_FILE, mode='w', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 2. Console Handler (Stderr only, INFO)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.info(f"=== Logging initialized. Log file: {_LOG_FILE} ===")

logger = logging.getLogger(__name__)

# --- Path Correction ---
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
    logger.debug(f"Added repo root to sys.path: {_REPO_ROOT}")

# --- Core Modules ---
try:
    from LLM_MODULE.llm import OpenAILLM
    from LLM_MODULE.tools import (
        load_plugins, 
        make_tool_handler, 
        tool_spec, 
        dispatch_tool
    )
    logger.debug("Successfully imported LLM_MODULE")
except ImportError as e:
    logger.critical(f"Failed to import LLM_MODULE: {e}", exc_info=True)
    raise

# --- User Input Import ---
PROJECT_ROOT = None
DEMAND = None
TASK_METRIC = None
TASK_NAME = None

try:
    import user_input
    PROJECT_ROOT = getattr(user_input, "PROJECT_ROOT", None)
    DEMAND = getattr(user_input, "DEMAND", "")
    TASK_METRIC = getattr(user_input, "TASK_METRIC", "")
    TASK_NAME = getattr(user_input, "TASK_NAME", "")
    logger.info(f"Imported user_input. PROJECT_ROOT={PROJECT_ROOT}, TASK={TASK_NAME}")
except ImportError as e:
    logger.critical(f"Failed to import user_input: {e}", exc_info=True)
    raise RuntimeError(f"Failed to import user_input: {e}")

try:
    from json_repair import repair_json  # type: ignore
except Exception:
    logger.warning("json_repair not installed, using fallback.")
    def repair_json(text: str) -> str:
        return text

# ==========================================
# Helper Classes & Functions
# ==========================================

@dataclass(frozen=True)
class _RunLog:
    returncode: int
    duration_sec: float
    stdout: str
    stderr: str

def _require_llm_env() -> None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        logger.error("Missing OPENAI_API_KEY")
        raise RuntimeError("Missing OPENAI_API_KEY: LLM evaluation required.")
    masked_key = key[:6] + "****" if len(key) > 6 else "****"
    logger.debug(f"Env check passed. API KEY: {masked_key}")

def _safe_json_loads(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        snippet = text[start : end + 1]
        try:
            res = json.loads(snippet)
            logger.debug("Parsed JSON via snippet extraction")
            return res
        except Exception:
            try:
                res = json.loads(repair_json(snippet))
                logger.debug("Parsed JSON via json_repair")
                return res
            except Exception as e:
                logger.warning(f"JSON parsing failed: {e}")
                return None
    return None

def _ensure_project_in_allowed_dirs(project_path: str) -> None:
    """Ensure PROJECT_ROOT is in user_input.ALLOWED_DIRS"""
    if not hasattr(user_input, "ALLOWED_DIRS"):
        setattr(user_input, "ALLOWED_DIRS", [])
    
    allowed = getattr(user_input, "ALLOWED_DIRS")
    abs_path = os.path.abspath(project_path)
    
    is_included = any(os.path.abspath(p) == abs_path for p in allowed)
    if not is_included:
        allowed.insert(0, abs_path)
        logger.info(f"Added {abs_path} to ALLOWED_DIRS")
    else:
        logger.debug(f"Path {abs_path} already in ALLOWED_DIRS")

def _internal_read_file(rel_path: str) -> str:
    """Internal helper to read files via tools"""
    logger.debug(f"Attempting to read file: {rel_path}")
    result = dispatch_tool("read_file", {"file_path": rel_path})
    
    if result.strip().startswith("{"):
        try:
            obj = json.loads(result)
            if isinstance(obj, dict) and "error" in obj:
                err_msg = obj['error']
                logger.error(f"Read failed: {rel_path} -> {err_msg}")
                return f"[Read Failed: {err_msg}]"
        except:
            pass
            
    preview = result[:100].replace("\n", "\\n") + "..." if len(result) > 100 else result
    logger.debug(f"Read success (len={len(result)}): {preview}")
    return result

# ==========================================
# Run Log Management
# ==========================================

def _run_entrypoint(output_dir: Path, entry_file: Path, *, timeout_sec: int) -> _RunLog:
    python_cmd = os.environ.get("PYTHON", "python")
    cmd = [python_cmd, str(entry_file)]
    
    logger.info(f"Executing: {' '.join(cmd)} (cwd={output_dir}, timeout={timeout_sec})")
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(output_dir),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        duration = time.time() - start
        logger.info(f"Execution done. ReturnCode={proc.returncode}, Duration={duration:.2f}s")
        logger.debug(f"Stdout len: {len(proc.stdout)}, Stderr len: {len(proc.stderr)}")
        
        return _RunLog(
            returncode=int(proc.returncode),
            duration_sec=float(duration),
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    except subprocess.TimeoutExpired as e:
        duration = time.time() - start
        logger.error(f"Execution timed out ({timeout_sec}s).")
        return _RunLog(
            returncode=124,
            duration_sec=float(duration),
            stdout=e.stdout or "",
            stderr=(e.stderr or "") + "\n[verify] Execution Timed Out",
        )
    except Exception as e:
        duration = time.time() - start
        logger.exception("Unknown exception during execution")
        return _RunLog(
            returncode=125,
            duration_sec=float(duration),
            stdout="",
            stderr=f"[verify] Execution Exception: {type(e).__name__}: {e}",
        )

def _get_run_log(output_dir: Path, entry_file: Path, timeout_sec: int) -> _RunLog:
    verify_dir = output_dir / ".verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    
    first_success = verify_dir / "first_success_run.json"
    latest = verify_dir / "latest_run.json"

    def _read_log(p: Path) -> Optional[_RunLog]:
        if not p.exists(): return None
        try:
            logger.debug(f"Reading cached log: {p}")
            d = json.loads(p.read_text(encoding="utf-8"))
            return _RunLog(**d)
        except Exception as e:
            logger.warning(f"Failed to read cached log {p}: {e}")
            return None

    log = _read_log(first_success)
    if log:
        logger.info("Found first_success_run.json, skipping re-run")
        return log
    
    log = _read_log(latest)
    if log:
        logger.info("Found latest_run.json, skipping re-run")
        return log

    logger.info("No cached logs found. Starting execution...")
    log = _run_entrypoint(output_dir, entry_file, timeout_sec=timeout_sec)
    
    log_dict = {
        "returncode": log.returncode, "duration_sec": log.duration_sec,
        "stdout": log.stdout, "stderr": log.stderr
    }
    dump_str = json.dumps(log_dict, ensure_ascii=False, indent=2)
    latest.write_text(dump_str, encoding="utf-8")
    
    if log.returncode == 0:
        first_success.write_text(dump_str, encoding="utf-8")
        logger.info("Execution successful, saved to first_success_run.json")
    else:
        logger.warning(f"Execution failed (code={log.returncode}), saved to latest_run.json")
    
    return log

# ==========================================
# LLM Scoring Logic (English Prompts)
# ==========================================

def _llm_score_pure(llm: OpenAILLM, dim_name: str, system: str, user: str) -> Dict[str, Any]:
    """Basic scoring (No tools), with logs"""
    logger.info(f"--- Evaluating Dimension: {dim_name} ---")
    logger.debug(f"[Prompt System]: {system}")
    user_preview = user if len(user) < 500 else user[:500] + "\n...[TRUNCATED]..."
    logger.debug(f"[Prompt User]: {user_preview}")

    start_t = time.time()
    reply = llm.chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    cost_t = time.time() - start_t
    
    logger.debug(f"[LLM Raw Reply] ({cost_t:.2f}s): {reply}")
    
    parsed = _safe_json_loads(reply) or {}
    logger.info(f"[{dim_name}] Result: {parsed}")
    return parsed

def verify(
    model: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    _require_llm_env()
    
    if not PROJECT_ROOT:
        raise ValueError("user_input.PROJECT_ROOT is undefined")
    
    out_dir = Path(PROJECT_ROOT).resolve()
    if not out_dir.is_dir():
        logger.error(f"PROJECT_ROOT not found: {out_dir}")
        raise FileNotFoundError(f"PROJECT_ROOT not found: {out_dir}")

    _ensure_project_in_allowed_dirs(str(out_dir))

    logger.info("Loading LLM tool plugins...")
    load_plugins(on_error="raise")

    entry_file = out_dir / "main.py"
    if not entry_file.exists():
         logger.critical(f"main.py missing: {entry_file}")
         raise FileNotFoundError("main.py missing")

    # Get Run Logs (Dim 1)
    run_log = _get_run_log(out_dir, entry_file, timeout_sec)
    stdout_stderr = (run_log.stdout or "") + "\n" + (run_log.stderr or "")

    # Read Files (Dim 2, 3, 4)
    main_py_text = _internal_read_file("main.py")
    project_tree_text = _internal_read_file("project_tree.txt")

    logger.info(f"Initializing LLM, Model: {model}")
    llm = OpenAILLM(model=model, module_name="verify_module", agent_name="verifier")

    # --- Dimension 1: Convergence (20 pts) ---
    conv_res = _llm_score_pure(
        llm, "Convergence",
        system="""You are a strict evaluator. Please output JSON: {"score": 0|20, "reason": "Reason in English"}
Rules:
1. If the `TASK_METRIC` in the training logs shows an improving trend (regardless of absolute value), give 20 points.
2. If there is no metric output or no improvement trend, give 0 points.""",
        user=f"TASK={TASK_NAME}, METRIC={TASK_METRIC}\n\n[Log Output]\n{stdout_stderr}"
    )

    # --- Dimension 2: Anti-Fake (20 pts) ---
    fake_res = _llm_score_pure(
        llm, "Anti-Fake",
        system="""You are a code auditor. Please output JSON: {"score": 0|20, "reason": "Reason in English", "has_fake_metric": bool}
Rules:
1. Check `main.py` for "metric falsification" (e.g., hardcoded results, artificial addition of scores, faking test sets).
2. Give 20 points if no falsification found; give 0 points and set `has_fake_metric=true` if falsification is detected.""",
        user=f"METRIC={TASK_METRIC}\n\n[main.py]\n{main_py_text}"
    )

    # --- Dimension 3: Task Alignment (30 pts) ---
    align_res = _llm_score_pure(
        llm, "TaskAlignment",
        system="""You are a task evaluator. Please output JSON: {"score": 0|30, "reason": "Reason in English"}
Rules:
1. Determine if the logic in `main.py` addresses the problem defined in `DEMAND`.
2. Give 30 points if aligned; give 0 points if irrelevant.""",
        user=f"DEMAND={DEMAND}\n\n[main.py]\n{main_py_text}"
    )

    # --- Dimension 4: Implementation Integrity (30 pts) ---
    logger.info("--- Evaluating Dimension: Implementation Integrity (With Tools) ---")
    dim4_system = """You are an implementation integrity auditor.
Your task is to verify if the project code contains "implementation hallucinations" (e.g., importing non-existent files, calling non-existent methods).

You have the tool:
- read_file: Read file content.

Process:
1. I will provide `project_tree.txt` and `main.py`.
2. Analyze imports and core calls in `main.py`.
3. **You MUST use the tool (read_file)** to verify if the referenced core files exist and if their content matches the usage.
4. Do not check all files; check only 1-3 key files in the core logic chain.
5. Output JSON scoring after verification.

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
    logger.debug("Entering Function Calling loop...")
    try:
        raw_dim4_reply = llm.chat_with_tools(
            messages=dim4_messages,
            tools=tool_spec(toolset="default"),
            tool_handler=make_tool_handler(toolset="default", enforce_policy=True),
        )
        logger.debug(f"[Dim 4 Raw Reply]: {raw_dim4_reply}")
    except Exception as e:
        logger.error("Dim 4 Function Call Error", exc_info=True)
        raw_dim4_reply = ""

    integrity_res = _safe_json_loads(raw_dim4_reply) or {}
    logger.info(f"[Implementation Integrity] Result: {integrity_res}")

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
    
    # Priority for failure reasons:
    # 1. Fake Metric -> Total 0, Fail
    # 2. Misalignment -> Total 0, Fail
    # 3. Score too low -> Fail
    
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
    
    logger.info(f"=== Verification Ended. Total={total}, Passed={is_passed} ===")
    if failure_reason:
        logger.info(f"Failure Reason: {failure_reason}")

    return {
        "convergence_score": {"score": s1, "reason": str(conv_res.get("reason", ""))},
        "no_hallucination_score": {"score": s2, "reason": str(fake_res.get("reason", ""))},
        "task_alignment_score": {"score": s3, "reason": str(align_res.get("reason", ""))},
        "implementation_integrity_score": {"score": s4, "reason": str(integrity_res.get("reason", ""))},
        "total_score": total,
        "is_passed": is_passed,
        "failure_reason": failure_reason
    }

def main() -> None:
    setup_logging()
    logger.info("Script started")
    
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.environ.get("VERIFY_MODEL", "gpt-4o"))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    
    logger.info(f"Args: model={args.model}, timeout={args.timeout}")

    try:
        report = verify(model=args.model, timeout_sec=args.timeout)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        logger.info("JSON report output to stdout")
        
    except Exception as e:
        logger.critical("Uncaught exception in main", exc_info=True)
        err_report = {
            "is_passed": False,
            "total_score": 0,
            "failure_reason": f"Verify script internal error: {str(e)}"
        }
        print(json.dumps(err_report, ensure_ascii=False, indent=2))
        sys.exit(1)

if __name__ == "__main__":
    main()