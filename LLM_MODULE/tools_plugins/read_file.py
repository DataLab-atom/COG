from __future__ import annotations
import sys
import os
import json
import yaml
from pathlib import Path
from typing import Dict

# ==========================================
# 1. 核心修复：解决路径问题
# ==========================================
# 获取当前文件的绝对路径
_CURRENT_FILE = Path(__file__).resolve()

# 计算项目根目录 (HMACF_PRO_MAX)
# 结构: HMACF_PRO_MAX / LLM_MODULE / tools_plugins / read_file.py
_PROJECT_ROOT_PATH = _CURRENT_FILE.parent.parent.parent

# 将项目根目录加入 sys.path
if str(_PROJECT_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT_PATH))

_REPO_ROOT = _PROJECT_ROOT_PATH

# ==========================================
# 2. 正常导入
# ==========================================
try:
    from LLM_MODULE.tool_registry import ToolDefinition, ToolManager
except ImportError as e:
    print(f"Import Error: {e}")
    raise e

# ==========================================
# 3. 动态配置读取逻辑 (已修复路径)
# ==========================================
def _get_active_task_name(default_task="template") -> str:
    """
    尝试从 config.yaml 动态读取当前激活的 task_name。
    优先查找: _REPO_ROOT/configs/config.yaml
    其次查找: _REPO_ROOT/config.yaml
    """
    # 优先检查 configs 目录 (根据你的截图，文件在这里)
    config_locations = [
        _REPO_ROOT / "configs" / "config.yaml",
        _REPO_ROOT / "config.yaml"
    ]
    
    found_config_path = None
    for path in config_locations:
        if path.exists():
            found_config_path = path
            break
            
    if not found_config_path:
        print(f"[Info] Config not found in {_REPO_ROOT} or {_REPO_ROOT}/configs. Using default: {default_task}")
        return default_task

    try:
        # 1. 解析主配置
        with open(found_config_path, "r", encoding="utf-8") as f:
            main_cfg = yaml.safe_load(f)
        
        # 2. 寻找 active problem
        active_problem_file = None
        if "defaults" in main_cfg:
            for item in main_cfg["defaults"]:
                if isinstance(item, dict) and "problems" in item:
                    active_problem_file = item["problems"]
                    break
        
        if not active_problem_file:
            return default_task

        # 3. 解析具体的问题配置文件
        # 既然 config.yaml 在 configs/ 下，那么 problems 目录很可能也在 configs/problems/
        # 我们根据找到 config.yaml 的目录来推断 problems 的位置
        config_dir = found_config_path.parent
        
        # 尝试路径: configs/problems/longtail.yaml
        problem_cfg_path = config_dir / "problems" / f"{active_problem_file}.yaml"
        
        if problem_cfg_path.exists():
            with open(problem_cfg_path, "r", encoding="utf-8") as f:
                prob_cfg = yaml.safe_load(f)
                return prob_cfg.get("task_name", active_problem_file)
        
        return active_problem_file

    except Exception as e:
        print(f"[Warning] Failed to load task_name from config: {e}. Using default.")
        return default_task

# 使用动态获取的 Task Name
TASK_NAME = _get_active_task_name()

_PROBLEM_ROOT = _REPO_ROOT / "problems" / TASK_NAME

ALLOWED_DIRS = [
    str(_PROBLEM_ROOT / "reports"),
    str(_PROBLEM_ROOT / "output_programs"),
    str(_PROBLEM_ROOT / "reference_paper_and_code" / "code"),
    str(_PROBLEM_ROOT / "reference_paper_and_code" / "paper"),
    str(_REPO_ROOT / "memory_module"/ "project_trees"),
    str(_REPO_ROOT / "memory_module"),
]

def _get_allowed_dirs() -> list[str]:
    dirs = list(ALLOWED_DIRS)
    extra_env = os.environ.get("READ_FILE_EXTRA_DIRS", "")
    if extra_env:
        for p in extra_env.split(os.pathsep):
            p = p.strip()
            if p and p not in dirs:
                dirs.append(p)
    output_dir = os.environ.get("HMACF_OUTPUT_DIR", "").strip()
    if output_dir and output_dir not in dirs:
        dirs.append(output_dir)
    return dirs

def read_file(relative_file_path: str) -> str:
    """????????"""
    allowed_dirs = _get_allowed_dirs()
    # 1) ??????????????
    if os.path.isabs(relative_file_path):
        real_path = os.path.realpath(relative_file_path)
        for allowed_dir in allowed_dirs:
            real_allowed = os.path.realpath(allowed_dir)
            if real_path.startswith(real_allowed) and os.path.isfile(real_path):
                with open(real_path, "r", encoding="utf-8") as f:
                    return f.read()
        return f"read operation fail?file not found in allowed directories: {relative_file_path}"

    # 2) ?????????????????
    for allowed_dir in allowed_dirs:
        full_path = os.path.join(allowed_dir, relative_file_path)
        if os.path.exists(full_path):
            # ??????????(??? .. ??)
            try:
                real_path = os.path.realpath(full_path)
                real_allowed = os.path.realpath(allowed_dir)
                if real_path.startswith(real_allowed):
                    with open(full_path, "r", encoding="utf-8") as f:
                        return f.read()
            except Exception:
                continue

    # 3) ??????????????????
    try:
        for allowed_dir in allowed_dirs:
            base = Path(allowed_dir)
            if not base.exists():
                continue
            for candidate in base.rglob(relative_file_path):
                try:
                    real_path = candidate.resolve()
                    real_allowed = Path(allowed_dir).resolve()
                    if str(real_path).startswith(str(real_allowed)) and real_path.is_file():
                        with open(real_path, "r", encoding="utf-8") as f:
                            return f.read()
                except Exception:
                    continue
    except Exception:
        pass

    return f"read operation fail?file not found in allowed directories: {relative_file_path}"

def register(manager: ToolManager) -> str:
    def execute_read_file_tool(arguments: Dict[str, object]) -> str:
        file_path = arguments.get("file_path", "")
        if not isinstance(file_path, str):
            return json.dumps({"error": "file_path must be a string"}, ensure_ascii=False)
        
        content = read_file(file_path)
        
        if content.startswith("read operation fail"):
            return json.dumps({"error": content}, ensure_ascii=False)
        
        return content

    manager.register_tool(
        ToolDefinition(
            name="read_file",
            description="Read the content of a file under the project root directory. Only relative paths are allowed.",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The relative path to the target file, e.g., 'src/model.py'."
                    }
                },
                "required": ["file_path"],
            },
            handler=execute_read_file_tool,
        ),
        toolsets=["default", "memory_tools"]
    )

if __name__ == "__main__":
    print(f"REPO ROOT: {_REPO_ROOT}")
    print(f"TASK NAME: {TASK_NAME}")
    # 调试：打印搜索的配置文件路径
    print(f"Scanning for configs in: {_REPO_ROOT / 'configs'}")
    print("Module loaded successfully.")
