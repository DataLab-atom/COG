from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def get_task_name(default: str = "default_task") -> str:
    try:
        import user_input  # type: ignore

        value = getattr(user_input, "TASK_NAME", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass

    env_value = os.getenv("TASK_NAME") or os.getenv("TASK") or os.getenv("PROBLEM_NAME")
    if env_value and env_value.strip():
        return env_value.strip()

    return default


def problem_dir(task_name: Optional[str] = None) -> Path:
    name = (task_name or "").strip() or get_task_name()
    return repo_root() / "problems" / name


def reports_dir(task_name: Optional[str] = None) -> Path:
    return problem_dir(task_name) / "reports"


def reference_paper_and_code_dir(task_name: Optional[str] = None) -> Path:
    return problem_dir(task_name) / "reference_paper_and_code"


def reference_code_root(task_name: Optional[str] = None) -> Path:
    return reference_paper_and_code_dir(task_name) / "code"


def reference_paper_root(task_name: Optional[str] = None) -> Path:
    return reference_paper_and_code_dir(task_name) / "paper"

