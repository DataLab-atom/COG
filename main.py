import logging
import sys
from pathlib import Path
from types import SimpleNamespace


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return path.read_text(errors="replace")


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except Exception as e:
        raise RuntimeError("缺少依赖：PyYAML") from e
    data = yaml.safe_load(_read_text(path))
    return data or {}


def _to_namespace(obj):
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_namespace(v) for v in obj]
    return obj


def _resolve_config_path(repo_root: Path) -> Path:
    cfg1 = repo_root / "configs" / "config.yaml"
    if cfg1.exists():
        return cfg1
    return repo_root / "config.yaml"


def _load_config(repo_root: Path, config_path: Path):
    if not config_path.exists():
        raise FileNotFoundError(f"未找到配置文件: {config_path}")
    cfg = _load_yaml(config_path)

    problem_name = None
    defaults = cfg.get("defaults") or []
    for item in defaults:
        if isinstance(item, dict) and "problems" in item:
            problem_name = item.get("problems")
            break

    problem_cfg = {}
    if problem_name:
        problem_path = config_path.parent / "problems" / f"{problem_name}.yaml"
        if problem_path.exists():
            problem_cfg = _load_yaml(problem_path)

    merged_problem = {}
    if isinstance(problem_cfg, dict):
        merged_problem.update(problem_cfg)
    if isinstance(cfg.get("problems"), dict):
        merged_problem.update(cfg.get("problems"))
    if merged_problem:
        cfg["problems"] = merged_problem

    cfg_ns = _to_namespace(cfg)
    if getattr(cfg_ns, "problems", None) is not None:
        setattr(cfg_ns, "problem", cfg_ns.problems)
    return cfg_ns


def main():
    repo_root = Path(__file__).resolve().parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )

    from core.pipeline import Pipeline

    config_path = _resolve_config_path(repo_root)
    cfg = _load_config(repo_root, config_path)

    pipeline = Pipeline(cfg)
    pipeline.run()


if __name__ == "__main__":
    main()
