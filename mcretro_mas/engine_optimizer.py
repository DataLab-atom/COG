import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed

from mcretro_mas.engine_logging import log_dialog, log_event 


class CodeInjector:
    @staticmethod
    def clean_llm_output(raw_text: str) -> str:
        pattern = r"```(?:python|py)?\s*(.*?)```"
        match = re.search(pattern, raw_text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return raw_text.strip()

    @staticmethod
    def get_indentation(line):
        return line[: len(line) - len(line.lstrip())]

    @staticmethod
    def replace_code_block(file_path, target_type, target_name, new_code):
        log_event(
            "code_injector_replace_start",
            file_path=file_path,
            target_type=target_type,
            target_name=target_name,
            new_code_chars=len(new_code or ""),
        )
        if not os.path.exists(file_path):
            log_event("code_injector_replace_file_missing", file_path=file_path)
            return False, f"File not found: {file_path}"

        clean_code = CodeInjector.clean_llm_output(new_code)

        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        source = "".join(lines)

        try:
            tree = ast.parse(source)
        except SyntaxError:
            log_event("code_injector_replace_target_syntax_error", file_path=file_path)
            return False, "Syntax Error in target file (before injection)"

        target_node = None
        for node in ast.walk(tree):
            if target_type == "function" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == target_name:
                    target_node = node
                    break
            elif target_type == "class" and isinstance(node, ast.ClassDef):
                if node.name == target_name:
                    target_node = node
                    break

        if not target_node:
            log_event(
                "code_injector_replace_target_not_found",
                file_path=file_path,
                target_type=target_type,
                target_name=target_name,
            )
            return False, f"Target {target_type} '{target_name}' not found"

        start_lineno = target_node.lineno
        if target_node.decorator_list:
            start_lineno = target_node.decorator_list[0].lineno

        end_lineno = target_node.end_lineno
        base_indent = CodeInjector.get_indentation(lines[start_lineno - 1])
        dedented_code = textwrap.dedent(clean_code).strip()

        new_lines = []
        for line in dedented_code.split("\n"):
            new_lines.append(base_indent + line + "\n" if line.strip() else "\n")

        final_content = lines[: start_lineno - 1] + new_lines + lines[end_lineno:]

        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(final_content)

        log_event(
            "code_injector_replace_success",
            file_path=file_path,
            target_type=target_type,
            target_name=target_name,
            start_lineno=start_lineno,
            end_lineno=end_lineno,
        )
        return True, "Success"


class AutoOptimizer:
    def __init__(
        self,
        project_root,
        entry_point="main.py",
        timeout=300,
        ignore_list=None,
        history_dir="./history",
        score_fn=None,
    ):
        self.project_root = os.path.abspath(project_root)
        self.entry_point = entry_point
        self.timeout = timeout
        self.ignore_list = ignore_list or ["__pycache__", "*.pyc", ".git"]
        # score_fn(metrics: dict) -> float, defaults to single-value passthrough or sum
        self.score_fn = score_fn or (
            lambda m: next(iter(m.values())) if len(m) == 1 else sum(m.values())
        )

        self.history_dir = os.path.abspath(history_dir)
        os.makedirs(self.history_dir, exist_ok=True)

        if not os.path.exists(os.path.join(self.project_root, self.entry_point)):
            raise ValueError(f"Entry point '{self.entry_point}' not found in {self.project_root}")

    def _run_sandbox(self, candidate, trial_id):
        log_event(
            "sandbox_run_start",
            trial_id=trial_id,
            candidate_summary={
                "target_file": candidate.get("target_file") if candidate else None,
                "target_type": candidate.get("target_type") if candidate else None,
                "target_name": candidate.get("target_name") if candidate else None,
                "code_chars": len(candidate.get("code", "")) if candidate else None,
            },
        )
        with tempfile.TemporaryDirectory(prefix=f"opt_{trial_id}_") as temp_dir:
            sandbox_root = os.path.join(temp_dir, "sandbox_app")
            ignore_func = shutil.ignore_patterns(*self.ignore_list)
            try:
                shutil.copytree(self.project_root, sandbox_root, ignore=ignore_func)
            except Exception as e:
                log_event("sandbox_copy_failed", trial_id=trial_id, sandbox_root=sandbox_root, error=str(e))
                return {"id": trial_id, "score": -999, "error": f"Copy failed: {e}", "output": ""}

            if candidate is not None:
                target_rel_path = candidate["target_file"].replace("\\", "/")
                target_file_path = os.path.join(sandbox_root, target_rel_path)

                success, msg = CodeInjector.replace_code_block(
                    target_file_path,
                    candidate["target_type"],
                    candidate["target_name"],
                    candidate["code"],
                )
                if not success:
                    log_event(
                        "sandbox_injection_failed",
                        trial_id=trial_id,
                        sandbox_root=sandbox_root,
                        error=msg,
                        target_file=target_rel_path,
                        target_type=candidate.get("target_type"),
                        target_name=candidate.get("target_name"),
                    )
                    return {"id": trial_id, "score": -999, "error": f"Injection failed: {msg}", "output": ""}

            output = ""
            try:
                result = subprocess.run(
                    [sys.executable, self.entry_point],
                    cwd=sandbox_root,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                output = result.stdout
                log_dialog("sandbox.stdout", result.stdout or "", trial_id=trial_id, sandbox_root=sandbox_root)
                if result.stderr:
                    log_dialog("sandbox.stderr", result.stderr, trial_id=trial_id, sandbox_root=sandbox_root)
            except subprocess.TimeoutExpired:
                log_event("sandbox_timeout", trial_id=trial_id, sandbox_root=sandbox_root)
                return {"id": trial_id, "score": -999, "error": "Timeout", "output": output}
            except Exception as e:
                log_event("sandbox_exception", trial_id=trial_id, sandbox_root=sandbox_root, error=str(e))
                return {"id": trial_id, "score": -999, "error": str(e), "output": output}

            _PREFIX = "__METRICS__:"
            for line in reversed(output.splitlines()):
                if line.startswith(_PREFIX):
                    try:
                        metrics = json.loads(line[len(_PREFIX):])
                        score = self.score_fn(metrics)
                        log_event(
                            "sandbox_run_scored",
                            trial_id=trial_id,
                            sandbox_root=sandbox_root,
                            metrics=metrics,
                            score=score,
                        )
                        return {
                            "id": trial_id,
                            "score": score,
                            "metrics": metrics,
                            "candidate": candidate,
                            "output": output,
                        }
                    except Exception as e:
                        log_event("sandbox_metrics_parse_error", trial_id=trial_id, error=str(e))
                    break

            err_tail = output[-300:] if output else result.stderr[-300:]
            log_event(
                "sandbox_run_no_score",
                trial_id=trial_id,
                sandbox_root=sandbox_root,
                err_tail=err_tail,
            )
            return {
                "id": trial_id,
                "score": -999,
                "error": f"No __METRICS__ line found in stdout. Tail: {err_tail}",
                "output": output + "\nSTDERR:\n" + result.stderr,
            }

    def _save_history(self, res):
        trial_id = res["id"]
        score = res["score"]
        safe_score = f"{score:.4f}" if score != -999 else "FAILED"
        filename = f"trial_{trial_id}_score_{safe_score}.py"
        file_path = os.path.join(self.history_dir, filename)

        if res.get("candidate") and "code" in res["candidate"]:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(f"# Trial ID: {trial_id}\n")
                f.write(f"# Score: {score}\n")
                f.write(f"# Target: {res['candidate'].get('target_name')}\n")
                f.write("# " + "=" * 20 + "\n\n")
                f.write(res["candidate"]["code"])

        log_path = os.path.join(self.history_dir, "results_summary.jsonl")
        log_entry = {
            "timestamp": time.time(),
            "trial_id": trial_id,
            "score": score,
            "error": res.get("error"),
            "target_file": res.get("candidate", {}).get("target_file") if res.get("candidate") else "BASELINE",
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
        log_event(
            "history_saved",
            trial_id=trial_id,
            score=score,
            error=res.get("error"),
            target_file=res.get("candidate", {}).get("target_file") if res.get("candidate") else "BASELINE",
            history_file=file_path if res.get("candidate") else None,
            summary_log=log_path,
        )

    def get_baseline_score(self):
        print(">>> Running Baseline (Original Project)...")
        baseline_id = f"baseline_{str(uuid.uuid4())[:8]}"
        log_event("baseline_start", baseline_id=baseline_id, project_root=self.project_root, entry_point=self.entry_point)

        res = self._run_sandbox(candidate=None, trial_id=baseline_id)
        self._save_history(res)

        output_log = res.get("output", "")
        log_dialog("baseline.output", output_log, baseline_id=baseline_id)

        if res["score"] != -999:
            print(f">>> Baseline Score: {res['score']}")
            log_event("baseline_success", baseline_id=baseline_id, score=res.get("score"))
            return res["score"], output_log

        print(f">>> Baseline Failed: {res.get('error')}")
        log_event("baseline_failed", baseline_id=baseline_id, error=res.get("error"))
        return -float("inf"), output_log

    def run(self, target_info, code_candidates, current_best_score, max_workers=None):
        print(f"Target Project: {self.project_root}")
        print(f"Evaluating {len(code_candidates)} candidates...")
        log_event(
            "optimizer_run_start",
            target_info=target_info,
            candidates=len(code_candidates),
            current_best_score=current_best_score,
            max_workers=max_workers,
        )

        full_candidates = []
        for code in code_candidates:
            full_candidates.append(
                {
                    "target_file": target_info["file"],
                    "target_type": target_info["type"],
                    "target_name": target_info["name"],
                    "code": code,
                }
            )

        results = []
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for c in full_candidates:
                t_id = str(uuid.uuid4())
                futures[executor.submit(self._run_sandbox, c, t_id)] = t_id

            for future in as_completed(futures):
                res = future.result()
                status = "SUCCESS" if res["score"] != -999 else "FAIL"
                print(f"Trial {res['id'].split('-')[0]}...: {status} | Score: {res['score']}")
                log_event(
                    "optimizer_trial_done",
                    trial_id=res.get("id"),
                    score=res.get("score"),
                    status=status,
                    error=res.get("error"),
                    target_file=res.get("candidate", {}).get("target_file") if res.get("candidate") else None,
                    target_name=res.get("candidate", {}).get("target_name") if res.get("candidate") else None,
                )
                self._save_history(res)
                results.append(res)

        valid_results = [r for r in results if r["score"] != -999]
        if not valid_results:
            return None

        best = max(valid_results, key=lambda x: x["score"])
        print(f"\nBatch Best ID: {best['id']}, Score: {best['score']}")
        log_event(
            "optimizer_batch_best",
            best_id=best.get("id"),
            best_score=best.get("score"),
            current_best_score=current_best_score,
        )

        if best["score"] > current_best_score:
            print(f">>> Improvement! ({best['score']} > {current_best_score})")
            log_event("optimizer_improved", best_score=best.get("score"), current_best_score=current_best_score)
            return best

        print(">>> No improvement.")
        log_event("optimizer_no_improvement", best_score=best.get("score"), current_best_score=current_best_score)
        return best

    def apply_change(self, candidate_result):
        if not candidate_result or "candidate" not in candidate_result:
            return
        candidate = candidate_result["candidate"]
        target_path = os.path.join(self.project_root, candidate["target_file"].replace("\\", "/"))
        log_event(
            "apply_change_start",
            target_path=target_path,
            target_file=candidate.get("target_file"),
            target_type=candidate.get("target_type"),
            target_name=candidate.get("target_name"),
            score=candidate_result.get("score"),
        )
        shutil.copy(target_path, target_path + ".bak")
        CodeInjector.replace_code_block(target_path, candidate["target_type"], candidate["target_name"], candidate["code"])
        print(f">>> Changes applied to {target_path}")
        log_event("apply_change_done", target_path=target_path, backup_path=target_path + ".bak")
