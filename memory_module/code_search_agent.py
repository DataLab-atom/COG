from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

from LLM_MODULE.llm import OpenAILLM
from LLM_MODULE.tools import load_plugins, make_tool_handler, tool_spec, tools_manifest_json
from .runtime_utils import (
    ADDITIONAL_CODE_EXTENSIONS,
    IGNORED_DIR_NAMES,
    IGNORED_FILE_EXTENSIONS,
    IGNORED_FILE_NAMES,
    generate_project_tree_text,
)
from .task_context import reference_code_root


_LOGGER = logging.getLogger("code_search_agent")

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

PROJECT_TREE_DIR = Path(__file__).resolve().parent / "project_trees"
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_CODE_SEARCH_TOOLS_READY = False


def _ensure_code_search_tools_ready() -> None:
    global _CODE_SEARCH_TOOLS_READY
    if _CODE_SEARCH_TOOLS_READY:
        return
    loaded = load_plugins(on_error="skip")
    _LOGGER.info("已加载工具插件: %s", loaded)
    _LOGGER.info("工具清单: %s", tools_manifest_json())
    _CODE_SEARCH_TOOLS_READY = True


def _configure_logger(prefix: str) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{prefix}_{timestamp}.log"
    stable_log_path = LOG_DIR / f"{prefix}.log"

    for handler in list(_LOGGER.handlers):
        _LOGGER.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    # Windows/PowerShell/Notepad 对无 BOM 的 UTF-8 支持不一致，这里用 utf-8-sig 便于直接打开查看。
    handlers = [
        logging.FileHandler(log_path, mode="w", encoding="utf-8-sig"),
        logging.FileHandler(stable_log_path, mode="w", encoding="utf-8-sig"),
    ]
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    for h in handlers:
        h.setFormatter(formatter)
        _LOGGER.addHandler(h)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False
    return log_path


def _default_max_workers() -> int:
    cpu = os.cpu_count() or 4
    return max(4, min(32, cpu + 4))


@dataclass
class ProjectStructure:
    name: str
    path: Path
    tree_text: str
    tree_file: Path
    source_files: List[Path]


@dataclass
class CodeBlock:
    project: str
    file_path: Path
    relative_path: str
    symbol_type: str
    symbol_name: str
    code: str
    score: float
    tree_file: Path
    document: str
    rerank_score: Optional[float] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "project": self.project,
            "file_path": str(self.file_path),
            "relative_path": self.relative_path,
            "symbol_type": self.symbol_type,
            "symbol_name": self.symbol_name,
            "score": self.score,
            "rerank_score": self.rerank_score,
            "code": self.code,
            "tree_file": str(self.tree_file),
            "document": self.document,
        }


@dataclass
class RerankResult:
    index: int
    score: float


class ProjectTreeManager:
    def __init__(
        self,
        reference_root: Optional[Path] = None,
        tree_dir: Path = PROJECT_TREE_DIR,
    ):
        self.reference_root = Path(reference_root) if reference_root else reference_code_root()
        self.tree_dir = Path(tree_dir)
        self.tree_dir.mkdir(parents=True, exist_ok=True)

    def sync_structures(self) -> List[ProjectStructure]:
        self._reset_tree_dir()
        _LOGGER.info("开始扫描代码参考目录: %s", self.reference_root)

        structures: List[ProjectStructure] = []
        if not self.reference_root.exists():
            _LOGGER.warning("代码参考目录不存在: %s", self.reference_root)
            return structures

        for project_dir in sorted(self.reference_root.iterdir()):
            if not project_dir.is_dir():
                continue
            structure = self._build_project_structure(project_dir)
            if not structure.source_files:
                continue
            _LOGGER.info(
                "项目 '%s' 结构生成完成，源文件数=%d，树缓存: %s",
                structure.name,
                len(structure.source_files),
                structure.tree_file,
            )
            structures.append(structure)

        return structures

    def _reset_tree_dir(self) -> None:
        if self.tree_dir.exists():
            shutil.rmtree(self.tree_dir, ignore_errors=True)
        self.tree_dir.mkdir(parents=True, exist_ok=True)

    def _build_project_structure(self, project_path: Path) -> ProjectStructure:
        lines = generate_project_tree_text(str(project_path))
        tree_text = "\n".join(lines) if lines else project_path.name
        tree_file = self.tree_dir / f"{project_path.name}_tree.txt"
        tree_file.write_text(tree_text, encoding="utf-8")
        source_files = list(self._iter_source_files(project_path))
        return ProjectStructure(
            name=project_path.name,
            path=project_path,
            tree_text=tree_text,
            tree_file=tree_file,
            source_files=source_files,
        )

    def _iter_source_files(self, project_path: Path) -> Iterable[Path]:
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES and not d.startswith(".")]
            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if not (filename.endswith(".py") or ext == ".r" or ext in ADDITIONAL_CODE_EXTENSIONS):
                    continue
                if filename in IGNORED_FILE_NAMES:
                    continue
                if any(filename.endswith(x) for x in IGNORED_FILE_EXTENSIONS):
                    continue
                yield Path(root) / filename


class ChatCompletionRerankClient:
    _RERANK_TEMPERATURES: Tuple[float, float, float] = (0.2, 0.7, 1.2)

    def __init__(self, llm: OpenAILLM, max_workers: Optional[int] = None):
        self.llm = llm
        self.max_workers = max_workers or _default_max_workers()

    def _score_single(self, idx: int, query: str, document: Dict[str, object]) -> RerankResult:
        code = str(document.get("code", "") or "")
        meta = (
            f"Project: {document.get('project')}\n"
            f"Path: {document.get('relative_path')}::{document.get('symbol_name')}\n"
            f"Type: {document.get('symbol_type')}"
        )
        snippet = code[:4000]
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        """You are an expert in code retrieval and reranking. Your task is to evaluate the relevance between a given [Code Snippet/Text] and a [User Query].
                            Please assign a score from 0.000 to 1.000 based on the following criteria:
                            - 1.000: The code perfectly addresses the query or contains the exact logic/answer required.
                            - 0.700-0.999: Highly relevant; contains core functionality but may need slight modification or context.
                            - 0.400-0.699: Semantically related (e.g., uses the same library or concepts) but does not directly solve the specific problem.
                            - 0.000-0.399: Irrelevant, off-topic, or misleading noise.

Focus on semantic and logical alignment rather than syntax perfection.
Output Constraint: Return ONLY a single floating-point number with exactly 3 decimal places. Do NOT include any explanations, labels, markdown formatting, or extra characters."""
                    ),
                },
                {
                    "role": "user",
                    "content": f"User Query:\n{query}\n\nCode Metadata:\n{meta}\n\nCode:\n```python\n{snippet}\n```",
                },
            ]

            scores: List[float] = []
            for attempt_idx, temp in enumerate(self._RERANK_TEMPERATURES, start=1):
                content = self.llm.chat(messages, temperature=float(temp)).strip()
                match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", content)
                single = float(match.group()) if match else 0.0
                if single > 1.0:
                    single = min(single, 100.0) / 100.0
                if single < 0.0:
                    single = 0.0
                if match is None:
                    _LOGGER.warning(
                        "rerank chat score 解析失败 idx=%d attempt=%d temp=%.3f raw=%r",
                        idx,
                        attempt_idx,
                        float(temp),
                        content,
                    )
                _LOGGER.info(
                    "rerank chat score idx=%d attempt=%d temp=%.3f raw=%r score=%.4f",
                    idx,
                    attempt_idx,
                    float(temp),
                    content,
                    single,
                )
                scores.append(single)

            score = sum(scores) / max(1, len(scores))
            _LOGGER.info("rerank chat score idx=%d avg_score=%.4f (n=%d)", idx, score, len(scores))
        except Exception as exc:
            _LOGGER.warning("rerank 失败 idx=%d: %s", idx, exc)
            score = 0.0
        return RerankResult(index=idx, score=score)

    def rerank(self, query: str, documents: List[Dict[str, object]], top_n: int) -> List[RerankResult]:
        if not documents or not query:
            return []
        truncated_query = query[:2000]
        _LOGGER.info("准备 rerank：文档数=%d，查询=%s", len(documents), truncated_query)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [
                pool.submit(self._score_single, idx, truncated_query, doc)
                for idx, doc in enumerate(documents)
            ]
            results = [future.result() for future in concurrent.futures.as_completed(futures)]
        results.sort(key=lambda item: item.score, reverse=True)
        top = results[: min(top_n, len(results))]
        _LOGGER.info("rerank 结束，结果数=%d，top_n=%d", len(top), int(top_n))
        return top


class LLMCodeBlockAgent:
    _COLLECTED_META_MARKER = "__COLLECTED_CODEBLOCKS_METADATA__"

    def __init__(self, llm: OpenAILLM, *, tree_char_limit: int = 100000, max_rounds: int = 8):
        self.llm = llm
        self.tree_char_limit = tree_char_limit
        self.max_rounds = max_rounds

    def collect(
        self,
        *,
        questions: Sequence[str],
        structures: List[ProjectStructure],
        target_blocks: int,
    ) -> Tuple[List[CodeBlock], str]:
        query_text = "\n".join(str(q).strip() for q in questions if str(q).strip()) or "请结合项目结构回答核心逻辑"
        tree_summary = self._build_tree_prompt(structures)

        _LOGGER.info(
            "LLMCodeBlockAgent.collect 开始：questions=%d projects=%d target_blocks=%d",
            len(questions),
            len(structures),
            int(target_blocks),
        )
        preview_projects = structures[:5]
        for structure in preview_projects:
            text = (structure.tree_text or "").strip()
            truncated = len(text) > self.tree_char_limit
            preview = "\n".join(text.splitlines()[:20])
            _LOGGER.info(
                "LLM tree 提示加入项目 %s：原始长度=%d 截断=%s 预览前20行:\n%s",
                structure.name,
                len(text),
                truncated,
                preview,
            )
        if len(structures) > len(preview_projects):
            _LOGGER.info("项目结构预览已截断：仅展示前 %d/%d 个项目", len(preview_projects), len(structures))

        _ensure_code_search_tools_ready()
        tools = tool_spec(toolset="code_search")
        if not tools:
            raise RuntimeError("toolset='code_search' 未提供任何工具，请确认已注册 fetch_code_blocks")

        system_prompt = (
            """
You are an expert-level code engineer tasked with quickly identifying key files, classes, or functions that may contain answers based on the **user query** and **project structure diagram**. This is a preliminary screening process to retrieve potentially relevant code blocks.
**Workflow**:
1. **Analyze intent**: Identify core functional modules referenced in the user query.
2. **Structure matching**: Screen the project structure for paths with semantically matching names (files/directories).
3. **State reasons**: Provide brief justification for each selected path.
4. **Execute retrieval**: Call `fetch_code_blocks` to obtain code content.

**Critical constraints**:
- Do not interact with the user; directly output function calls.
- Avoid retrieving code blocks already provided by the user.
"""
        ).strip()
        structures_map = {s.name: s for s in structures}
        collected: Dict[Tuple[str, str, str], CodeBlock] = {}

        def parse_tool_blocks(tool_response: str) -> List[CodeBlock]:
            try:
                data = json.loads(tool_response)
            except Exception:
                return []
            if data.get("success") is False:
                return []
            if not data.get("ok"):
                return []
            blocks: List[CodeBlock] = []
            for block in data.get("blocks", []):
                project = str(block.get("project") or "")
                relative_path = str(block.get("relative_path") or "")
                code = str(block.get("code") or "")
                if not project or not relative_path or not code:
                    continue
                structure = structures_map.get(project)
                if not structure:
                    continue
                file_path = structure.path / Path(relative_path)
                symbol_name = str(block.get("symbol_name") or "unknown")
                symbol_type = str(block.get("symbol_type") or "unknown")
                blocks.append(
                    CodeBlock(
                        project=project,
                        file_path=file_path,
                        relative_path=relative_path,
                        symbol_type=symbol_type,
                        symbol_name=symbol_name,
                        code=code,
                        score=1.0,
                        tree_file=structure.tree_file,
                        document=f"[{project}] {relative_path}::{symbol_name}\n{code}",
                    )
                )
            return blocks

        base_tool_handler = make_tool_handler(toolset="code_search")
        seen_tool_calls: Dict[str, bool] = {}

        def tool_handler(name: str, args: Dict[str, object]) -> str:
            call_key = ""
            if name == "fetch_code_blocks":
                call_key = json.dumps({"name": name, "args": args or {}}, ensure_ascii=False, sort_keys=True)
                if seen_tool_calls.get(call_key) is True:
                    _LOGGER.info("fetch_code_blocks 重复调用，已跳过: %s", call_key)
                    return json.dumps(
                        {"ok": True, "blocks": [], "skipped": True, "reason": "duplicate tool call"},
                        ensure_ascii=False,
                    )
                _LOGGER.info("fetch_code_blocks 调用参数: %s", call_key)
                project = str((args or {}).get("project") or "")
                file_path = str((args or {}).get("file_path") or "")
                symbol_names = (args or {}).get("symbol_names") or []
                symbol_types = (args or {}).get("symbol_types") or []
                _LOGGER.info(
                    "执行工具 fetch_code_blocks，项目 %s，路径 %s，symbols=%s，types=%s",
                    project,
                    file_path,
                    symbol_names,
                    symbol_types,
                )

            raw = base_tool_handler(name, args or {})

            if name == "fetch_code_blocks":
                ok_flag = False
                try:
                    data = json.loads(raw)
                    ok_flag = bool(data.get("ok")) and (data.get("success") is not False)
                    if not ok_flag:
                        _LOGGER.warning("fetch_code_blocks 返回错误: %s", json.dumps(data, ensure_ascii=False)[:2000])
                except Exception:
                    _LOGGER.warning("fetch_code_blocks 返回非 JSON: %s", str(raw)[:1000])
                if call_key:
                    seen_tool_calls[call_key] = ok_flag

                parsed = parse_tool_blocks(raw)
                _LOGGER.info("解析工具响应，得到 %d 个代码块", len(parsed))
                new_count = 0
                dup_count = 0
                for block in parsed:
                    key = (block.project, block.relative_path, block.symbol_name)
                    if key in collected:
                        dup_count += 1
                        continue
                    collected[key] = block
                    new_count += 1
                _LOGGER.info(
                    "fetch_code_blocks 解析完成：新增=%d，重复=%d，累计=%d",
                    new_count,
                    dup_count,
                    len(collected),
                )
                _LOGGER.info("LLMCodeBlockAgent 累积代码块 %d", len(collected))

            return raw

        for round_idx in range(self.max_rounds):
            if len(collected) >= target_blocks:
                break
            before = len(collected)
            messages: List[Dict[str, object]] = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Question:\n{query_text}\n\nProject structure:\n{tree_summary}\n\n"
                        + ("Continue: retrieve more code blocks from other related files.\n\n" if round_idx > 0 else "")
                        + "Please call the tool to find relevant code blocks/files."
                    ),
                },
            ]

            self._upsert_collected_metadata_prompt(messages, collected)
            _LOGGER.info(
                "LLMCodeBlockAgent 回合 %d/%d：当前=%d，目标=%d",
                round_idx + 1,
                self.max_rounds,
                before,
                int(target_blocks),
            )
            _ = self.llm.chat_with_tools(
                messages,
                tools=tools,
                tool_handler=tool_handler,
                temperature=0.0,
                max_tool_loops=80,
                need_append_msg=False,
            )
            after = len(collected)
            _LOGGER.info("LLMCodeBlockAgent 回合 %d: collected %d -> %d", round_idx + 1, before, after)
            if after <= before:
                _LOGGER.info("LLMCodeBlockAgent 回合 %d 没有新增结果，提前结束", round_idx + 1)
                break

        _LOGGER.info("LLMCodeBlockAgent 完成，获取代码块: %d", len(collected))
        return list(collected.values())[:target_blocks], query_text

    def _build_tree_prompt(self, structures: List[ProjectStructure]) -> str:
        segments: List[str] = []
        for structure in structures:
            text = (structure.tree_text or "").strip()
            if len(text) > self.tree_char_limit:
                text = text[: self.tree_char_limit] + "\n..."
            segments.append(f"[{structure.name}]\n{text}")
        return "\n\n".join(segments)

    def _render_collected_metadata_prompt(
        self,
        collected: Dict[Tuple[str, str, str], CodeBlock],
        *,
        char_limit: int = 20000,
    ) -> str:
        if not collected:
            return ""
        lines: List[str] = [
            "Metadata of retrieved code blocks (do not re-retrieve the same file/symbol):",
            "",
            f"Retrieved code blocks (total: {len(collected)}):",
        ]
        for block in collected.values():
            lines.append(f"- [{block.project}] {block.relative_path}::{block.symbol_name} ({block.symbol_type})")
        text = "\n".join(lines).strip()
        if len(text) <= char_limit:
            return text
        return (text[:char_limit] + "\n...(truncated)").strip()

    def _upsert_collected_metadata_prompt(
        self,
        messages: List[Dict[str, object]],
        collected: Dict[Tuple[str, str, str], CodeBlock],
    ) -> None:
        prompt = self._render_collected_metadata_prompt(collected)
        if not prompt:
            return
        content = f"{self._COLLECTED_META_MARKER}\n{prompt}"
        for msg in messages:
            if msg.get("role") != "user" or not isinstance(msg.get("content"), str):
                continue
            base = msg["content"]
            if self._COLLECTED_META_MARKER in base:
                base = base.split(self._COLLECTED_META_MARKER, 1)[0].rstrip()
            msg["content"] = f"{base}\n\n{content}".strip()
            return
        messages.append({"role": "user", "content": content})


class CodeSearchAgent:
    def __init__(
        self,
        project_manager: Optional[ProjectTreeManager] = None,
        max_workers: Optional[int] = None,
        llm_client: Optional[OpenAILLM] = None,
        rerank_client: Optional[ChatCompletionRerankClient] = None,
    ):
        self.project_manager = project_manager or ProjectTreeManager()
        self.max_workers = max_workers or _default_max_workers()

        model = os.getenv("CODE_SEARCH_AGENT_LLM_NAME") or os.getenv("code_search_agent_model_name") or "gpt-4.1-mini"
        self.llm_client = llm_client or OpenAILLM(model=model, module_name="memory_module", agent_name="CodeSearchAgent")
        self.rerank_client = rerank_client or ChatCompletionRerankClient(self.llm_client, max_workers=self.max_workers)

    def search(self, questions: Sequence[str], top_n: int = 5, candidate_pool: int = 20) -> List[Dict[str, object]]:
        log_path = _configure_logger("code_search_agent")
        _LOGGER.info("日志文件: %s", log_path)
        _LOGGER.info("CodeSearchAgent.search 原始问题: %s", [str(q) for q in questions])
        if questions:
            _LOGGER.info("CodeSearchAgent.search 启动，问题 %s，候选池上限: %d", str(questions[0]), int(candidate_pool))
        else:
            _LOGGER.info("CodeSearchAgent.search 启动，候选池上限: %d", int(candidate_pool))

        structures = self.project_manager.sync_structures()
        if not structures:
            raise RuntimeError("未找到可用的代码参考项目结构")

        llm_agent = LLMCodeBlockAgent(self.llm_client)
        code_blocks, query_text = llm_agent.collect(questions=questions, structures=structures, target_blocks=candidate_pool)
        _LOGGER.info("候选代码块数量: %d", len(code_blocks))
        if code_blocks:
            _LOGGER.info("候选代码块列表:")
            for block in code_blocks:
                path_display = str(block.relative_path).replace("/", "\\")
                _LOGGER.info("- [%s] %s::%s", block.project, path_display, block.symbol_name)

        documents = [
            {
                "project": block.project,
                "relative_path": block.relative_path,
                "symbol_name": block.symbol_name,
                "symbol_type": block.symbol_type,
                "code": block.code,
            }
            for block in code_blocks
        ]
        rerank_results = self.rerank_client.rerank(query=query_text, documents=documents, top_n=top_n)

        ranked_blocks: List[CodeBlock] = []
        used_indices = set()
        for result in rerank_results:
            if result.index >= len(code_blocks):
                continue
            block = code_blocks[result.index]
            block.rerank_score = result.score
            ranked_blocks.append(block)
            used_indices.add(result.index)

        for idx, block in enumerate(code_blocks):
            if len(ranked_blocks) >= top_n:
                break
            if idx in used_indices:
                continue
            if block.rerank_score is None:
                block.rerank_score = block.score
            ranked_blocks.append(block)

        top = [block.to_dict() for block in ranked_blocks[:top_n]]
        if ranked_blocks:
            _LOGGER.info("Top 结果:")
            for block in ranked_blocks[:top_n]:
                path_display = str(block.relative_path).replace("/", "\\")
                _LOGGER.info(
                    "- [%s] %s::%s (rerank_score=%s)",
                    block.project,
                    path_display,
                    block.symbol_name,
                    str(block.rerank_score),
                )
        _LOGGER.info("CodeSearchAgent.search 完成，返回候选 %d 个，top %d 个", len(code_blocks), len(top))
        return top
