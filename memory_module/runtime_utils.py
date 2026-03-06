import logging
import os
import re
import threading
from typing import Dict, List, Optional

from dotenv import load_dotenv
import ast

_ENV_LOCK = threading.Lock()
_ENV_LOADED = False


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    with _ENV_LOCK:
        if not _ENV_LOADED:
            load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
            _ENV_LOADED = True


def _getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    _ensure_env_loaded()
    return os.getenv(name, default)


PAPER_SEARCH_AGENT_LLM = _getenv("PAPER_SEARCH_AGENT_LLM_NAME", "gpt-4.1-mini")
PDF_MD_TOKEN = _getenv("PDF_MD_TOKEN", "")


def setup_logging(log_path: Optional[str] = None) -> None:
    """Configure logging so that multiprocessing workers inherit sane defaults."""
    handlers = []
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(processName)s - %(message)s",
        handlers=handlers,
    )


IGNORED_DIR_NAMES = {
    ".git",
    ".github",
    ".gitlab",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
    "docs",
    "doc",
    "scripts",
    "tests",
    "test",
    "notebooks",
    "assets",
    "examples_data",
    "examples",
    "baseline",
    "problems"
}

IGNORED_FILE_NAMES = {
    ".gitignore",
    ".gitattributes",
    "README.md",
    "README",
    "LICENSE",
    "LICENSE.md",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "Makefile",
    "Dockerfile",
    "mkdocs.yml",
}

IGNORED_FILE_EXTENSIONS = {
    ".md",
    ".rst",
    ".json",
    ".yml",
    ".ini",
    ".cfg",
    ".xml",
    ".toml",
    ".lock",
    ".csv",
    ".tsv",
    ".xlsx",
    ".ipynb",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".mp4",
    ".mp3",
    ".wav",
    ".log",
    ".npy",
    ".npz",
    ".pkl",
    ".ckpt",
}

# Extra file extensions that should be treated as standalone code blocks.
# Extend this set if更多类型需要检索，如 ".md" 等。
ADDITIONAL_CODE_EXTENSIONS = {".txt",".yaml"}


def _analyze_python_file(filepath: str) -> Dict[str, List[Dict[str, object]]]:
    try:
        with open(filepath, "r", encoding="utf-8") as source:
            content = source.read()
    except UnicodeDecodeError:
        with open(filepath, "r", encoding="gbk", errors="ignore") as source:
            content = source.read()
    except OSError:
        return {"classes": [], "functions": []}

    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        return {"classes": [], "functions": []}

    classes: List[Dict[str, object]] = []
    functions: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = []
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(child.name)
            classes.append({"name": node.name, "methods": methods})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
    return {"classes": classes, "functions": functions}


def extract_r_code_with_structures(file_path: str, keep_last: bool = True) -> List[dict]:
    """
    从 .R/.r 文件中抽取 R 语言的函数代码块，返回结构与 tree_cot.utils.init_utils.extract_code_with_structures 类似。

    目前支持的函数定义形态：
    - name <- function(...) { ... }
    - name = function(...) { ... }
    - `name-with-dash` <- function(...) { ... }

    若未检测到函数，则返回一个 script 块（整文件内容），以便检索系统仍可获取上下文。
    """

    def _read_text(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="gbk", errors="ignore") as f:
                return f.read()

    def _find_matching(text: str, start_idx: int, open_ch: str, close_ch: str) -> int:
        depth = 0
        in_comment = False
        in_string: Optional[str] = None
        escape = False
        i = start_idx
        while i < len(text):
            ch = text[i]
            if in_comment:
                if ch == "\n":
                    in_comment = False
                i += 1
                continue

            if in_string:
                if escape:
                    escape = False
                    i += 1
                    continue
                if ch == "\\":
                    escape = True
                    i += 1
                    continue
                if ch == in_string:
                    in_string = None
                i += 1
                continue

            if ch == "#":
                in_comment = True
                i += 1
                continue
            if ch == "'" or ch == '"':
                in_string = ch
                i += 1
                continue

            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return len(text)

    def _skip_ws_and_comments(text: str, idx: int) -> int:
        while idx < len(text):
            ch = text[idx]
            if ch.isspace():
                idx += 1
                continue
            if ch == "#":
                nl = text.find("\n", idx)
                if nl == -1:
                    return len(text)
                idx = nl + 1
                continue
            return idx
        return idx

    def _include_roxygen(text: str, line_start: int) -> int:
        start = line_start
        while start > 0:
            prev_nl = text.rfind("\n", 0, start - 1)
            prev_line_start = 0 if prev_nl == -1 else prev_nl + 1
            prev_line = text[prev_line_start:start].rstrip("\n")
            if re.match(r"^\s*#'", prev_line):
                start = prev_line_start
                continue
            break
        return start

    text = _read_text(file_path)
    if not text.strip():
        return []

    func_pattern = re.compile(
        r"(?m)^(?P<indent>\s*)(?P<name>(`[^`]+`|[A-Za-z.][\w.]*))\s*(?P<op><-|=)\s*function\s*\("  # noqa: E501
    )
    raw_results: List[dict] = []

    for match in func_pattern.finditer(text):
        assign_line_start = text.rfind("\n", 0, match.start())
        line_start = 0 if assign_line_start == -1 else assign_line_start + 1
        start_idx = _include_roxygen(text, line_start)

        args_open_idx = match.end() - 1  # points to '('
        args_end = _find_matching(text, args_open_idx, "(", ")")
        body_start = _skip_ws_and_comments(text, args_end)
        if body_start < len(text) and text[body_start] == "{":
            end_idx = _find_matching(text, body_start, "{", "}")
        else:
            nl = text.find("\n", body_start)
            end_idx = len(text) if nl == -1 else nl + 1

        code = text[start_idx:end_idx].rstrip()
        code_name = match.group("name").strip("`")
        lineno = text.count("\n", 0, start_idx) + 1
        raw_results.append(
            {
                "type": "function",
                "code_name": code_name,
                "code_source": f"```r\n{code}\n```",
                "lineno": lineno,
            }
        )

    if not raw_results:
        code_name = os.path.splitext(os.path.basename(file_path))[0] or "script"
        raw_results.append(
            {
                "type": "script",
                "code_name": code_name,
                "code_source": f"```r\n{text.rstrip()}\n```",
                "lineno": 1,
            }
        )

    sorted_results = sorted(raw_results, key=lambda item: int(item.get("lineno", 0)))
    if not keep_last:
        return sorted_results

    seen: Dict[tuple, int] = {}
    for idx, item in enumerate(sorted_results):
        key = (item.get("type"), item.get("code_name"))
        seen[key] = idx

    final_results: List[dict] = []
    for idx, item in enumerate(sorted_results):
        key = (item.get("type"), item.get("code_name"))
        if seen.get(key) == idx:
            final_results.append(item)
    return final_results
def generate_project_tree_text(
    project_path: str,
    indent_char: str = "    ",
    prefix: str = "",
) -> List[str]:
    lines: List[str] = []
    try:
        items = sorted(os.listdir(project_path))
    except FileNotFoundError:
        return [f"{prefix}[Error: Directory not found - {project_path}]"]
    except PermissionError:
        return [f"{prefix}[Error: Permission denied - {project_path}]"]

    filtered: List[str] = []
    for item in items:
        full_path = os.path.join(project_path, item)
        if os.path.isdir(full_path):
            if item in IGNORED_DIR_NAMES or item.startswith("."):
                continue
        else:
            if (
                item in IGNORED_FILE_NAMES
                or any(item.endswith(ext) for ext in IGNORED_FILE_EXTENSIONS)
            ):
                continue
        filtered.append(item)

    for idx, name in enumerate(filtered):
        full_path = os.path.join(project_path, name)
        is_last = idx == len(filtered) - 1
        connector = "+-- " if is_last else "|-- "
        if os.path.isdir(full_path):
            lines.append(f"{prefix}{connector}{name}/")
            child_prefix = prefix + (indent_char if is_last else "|   ")
            lines.extend(generate_project_tree_text(full_path, indent_char, child_prefix))
        else:
            lines.append(f"{prefix}{connector}{name}")
            ext = os.path.splitext(name)[1].lower()
            if name.endswith(".py"):
                analysis = _analyze_python_file(full_path)
                child_prefix = prefix + (indent_char if is_last else "|   ")
                for cls_index, cls in enumerate(analysis["classes"]):
                    cls_last = cls_index == len(analysis["classes"]) - 1 and not analysis["functions"]
                    cls_connector = "+-- " if cls_last else "|-- "
                    lines.append(f"{child_prefix}{cls_connector}[Class] {cls['name']}")
                    method_prefix = child_prefix + (indent_char if cls_last else "|   ")
                    for method_idx, method in enumerate(cls["methods"]):
                        method_last = method_idx == len(cls["methods"]) - 1
                        method_connector = "+-- " if method_last else "|-- "
                        lines.append(f"{method_prefix}{method_connector}{method}()")
                for func_index, func_name in enumerate(analysis["functions"]):
                    func_last = func_index == len(analysis["functions"]) - 1
                    func_connector = "+-- " if func_last else "|-- "
                    lines.append(f"{child_prefix}{func_connector}[Func] {func_name}()")
            elif ext in ADDITIONAL_CODE_EXTENSIONS:
                child_prefix = prefix + (indent_char if is_last else "|   ")
                lines.append(f"{child_prefix}+-- [TextFile]")
    return lines
