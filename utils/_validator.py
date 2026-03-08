"""
_validator.py — 配置合规性校验器

对图 / 流水线 JSON 配置进行静态合规性校验，不依赖执行层。

核心规则：
    含 long_running=true 节点的图 / 流水线，其顶层必须也标记 long_running=true。

━━ 用法 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    from utils import validate_long_running_propagation, validate_all_configs

    # 单文件校验
    import json
    cfg = json.loads(Path("graphs/my_graph.json").read_text())
    errors = validate_long_running_propagation(cfg)
    if errors:
        for e in errors:
            print("违规:", e)

    # 全项目扫描
    violations = validate_all_configs()
    for path, errs in violations.items():
        for e in errs:
            print(f"[{path}] {e}")
"""

from __future__ import annotations

import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
_INPUT_DIR = _PROJECT_ROOT / "input"
_CONFIGS_DIR = _PROJECT_ROOT / "configs"


# ── 核心判断逻辑 ───────────────────────────────────────────────────────────────

def _is_step_long_running(step: dict, _visited: set[str] | None = None) -> bool:
    """判断单个步骤是否为长期运行节点。

    判定条件（满足任一即为 long_running）：
        1. 步骤自身声明 long_running=true
        2. 步骤引用的 input/*.json 声明 long_running=true
        3. 步骤引用的 configs/*.json 声明 long_running=true
        4. 步骤引用的子流水线 / 子图本身声明 long_running=true
    """
    if step.get("long_running"):
        return True

    step_type = step.get("type")
    ref = step.get("ref", "")
    if not ref:
        return False

    if step_type == "input":
        # 解析 input 配置文件
        if Path(ref).suffix == ".json":
            p = _PROJECT_ROOT / ref if not Path(ref).is_absolute() else Path(ref)
        else:
            p = _INPUT_DIR / f"{ref}.json"
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            if cfg.get("long_running"):
                return True

    elif step_type == "agent":
        # 解析 agent 配置文件
        p = _PROJECT_ROOT / ref if not Path(ref).is_absolute() else Path(ref)
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            if cfg.get("long_running"):
                return True

    elif step_type in ("pipeline", "graph"):
        # 递归检查子图 / 子流水线（防循环）
        if _visited is None:
            _visited = set()
        abs_ref = str((_PROJECT_ROOT / ref if not Path(ref).is_absolute() else Path(ref)).resolve())
        if abs_ref not in _visited:
            _visited.add(abs_ref)
            p = Path(abs_ref)
            if p.exists():
                sub_cfg = json.loads(p.read_text(encoding="utf-8"))
                if sub_cfg.get("long_running"):
                    return True
                # 递归检查子配置内的步骤
                for sub_step in sub_cfg.get("steps", []):
                    if _is_step_long_running(sub_step, _visited):
                        return True

    return False


# ── 公开校验接口 ───────────────────────────────────────────────────────────────

def validate_long_running_propagation(config: dict) -> list[str]:
    """校验图 / 流水线配置的 long_running 传播合规性。

    规则：若任一步骤为长期运行节点，顶层配置必须声明 long_running=true。

    Args:
        config: 图或流水线的配置字典（已解析 JSON）。

    Returns:
        违规说明列表，空列表表示无违规。

    Example:
        >>> import json
        >>> cfg = json.loads(Path("graphs/channel_echo.json").read_text())
        >>> errors = validate_long_running_propagation(cfg)
        >>> assert errors == []   # channel_echo 正确标记了 long_running=true
    """
    top_long_running = config.get("long_running", False)
    errors: list[str] = []

    for step in config.get("steps", []):
        if _is_step_long_running(step):
            if not top_long_running:
                step_id = step.get("id", "<无id>")
                ref = step.get("ref", "")
                errors.append(
                    f"步骤 '{step_id}'（ref={ref!r}）为长期运行节点，"
                    f"但顶层配置未声明 long_running=true"
                )
    return errors


def validate_all_configs() -> dict[str, list[str]]:
    """扫描项目中所有图 / 流水线配置文件，返回所有违规信息。

    Returns:
        {文件相对路径: [违规说明]} 字典，无违规的文件不出现在结果中。

    Example:
        >>> violations = validate_all_configs()
        >>> if violations:
        ...     for path, errs in violations.items():
        ...         for e in errs:
        ...             print(f"[{path}] {e}")
        ... else:
        ...     print("所有配置合规")
    """
    violations: dict[str, list[str]] = {}
    for pattern in ("graphs/*.json", "pipelines/*.json"):
        for p in sorted(_PROJECT_ROOT.glob(pattern)):
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            errs = validate_long_running_propagation(cfg)
            if errs:
                violations[str(p.relative_to(_PROJECT_ROOT))] = errs
    return violations
