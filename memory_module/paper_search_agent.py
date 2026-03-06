from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from pypdf import PdfReader

from LLM_MODULE.llm import OpenAILLM
from .task_context import reference_paper_root
from .runtime_utils import PAPER_SEARCH_AGENT_LLM

logger = logging.getLogger(__name__)
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _configure_logger(prefix: str) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{prefix}_{timestamp}.log"

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    handler = logging.FileHandler(log_path, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return log_path


def _clean_markdown(text: str) -> str:
    text = re.sub(r"!\\[[^\\]]*\\]\\([^)]*\\)", "", text)
    text = re.sub(r"\\n{3,}", "\\n\\n", text)
    return text.strip()


def _truncate_blocks(blocks: Sequence[str], max_chars: int) -> str:
    result: List[str] = []
    total = 0
    for block in blocks:
        if total >= max_chars:
            break
        remaining = max_chars - total
        if len(block) <= remaining:
            result.append(block)
            total += len(block)
        else:
            result.append(block[:remaining])
            total = max_chars
            break
    return "\n\n".join(result)


def _extract_pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    parts: List[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n\n".join(p for p in parts if p.strip())
    return text.strip()


@dataclass
class PaperRecord:
    pdf_path: Path
    category: str
    md_path: Path

    @property
    def title(self) -> str:
        return self.pdf_path.stem


class PaperSearchAgent:
    """
    注意：
    - 对外接口（__init__ 参数与 answer 方法签名）保持不变：因为它被注册成 tool。
    - RAG 逻辑已按需求移除，只保留“是否触发 RAG”的判断与占位接口。
    """

    def __init__(
        self,
        rag_threshold: int = 8,
        retrieval_top_k: int = 12,
        rerank_top_k: int = 5,
        llm_model: str = PAPER_SEARCH_AGENT_LLM,
        embedding_model_name: str = "BAAI/bge-m3",
    ) -> None:
        self.paper_root = reference_paper_root()
        self.problem_dir = self.paper_root / "problem_area_paper"
        self.reference_dir = self.paper_root / "reference_area_paper"

        self.rag_threshold = int(rag_threshold)
        self.retrieval_top_k = int(retrieval_top_k)
        self.rerank_top_k = int(rerank_top_k)

        self.llm_model = llm_model
        self.embedding_model_name = embedding_model_name

        self.llm = OpenAILLM(model=self.llm_model, module_name="memory_module", agent_name="PaperSearchAgent")

        self.paper_records: List[PaperRecord] = []
        self.sources_used: List[Dict[str, str]] = []
        self._ensure_markdown_ready()

    def _ensure_markdown_ready(self) -> None:
        for category, folder in [("problem_area", self.problem_dir), ("reference_area", self.reference_dir)]:
            if not folder.exists():
                continue
            for pdf_path in sorted(folder.glob("*.pdf")):
                md_dir = folder / pdf_path.stem
                md_file = md_dir / f"{pdf_path.stem}.md"
                if not md_file.exists():
                    md_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        text = _extract_pdf_text(pdf_path)
                        md_file.write_text(text, encoding="utf-8")
                    except Exception as exc:
                        logger.warning("PDF 转文本失败: %s, err=%s", pdf_path, exc)
                if md_file.exists():
                    self.paper_records.append(PaperRecord(pdf_path=pdf_path, category=category, md_path=md_file))

    def _full_context_blocks(self) -> Tuple[List[str], List[Dict[str, str]]]:
        blocks: List[str] = []
        citations: List[Dict[str, str]] = []
        for record in self.paper_records:
            text = _clean_markdown(record.md_path.read_text(encoding="utf-8", errors="ignore"))
            header = f"# {record.title} ({record.category})\n来源: {record.md_path.as_posix()}\n"
            blocks.append(f"{header}\n{text}")
            citations.append({"title": record.title, "category": record.category, "path": record.md_path.as_posix()})
        return blocks, citations

    def _rag_blocks(self, question: str) -> Tuple[List[str], List[Dict[str, str]]]:
        logger.info("RAG 触发(论文数>=阈值)，但 RAG 逻辑已移除：question=%s", question)
        raise NotImplementedError
        #return self._full_context_blocks()

    def _call_llm(self, context: str, question: str, language: str) -> str:
        return self.llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a senior domain expert. Your primary task is to answer the user's question based solely on the provided excerpts (Context). "
                        "Please adhere to the following rules: "
                        "1. **Strictly rely on the context**: Use only the provided information; do not fabricate or incorporate external knowledge. "
                        "2. **Citation standard**: When stating a fact, append a source citation at the end of the sentence (e.g., [Author, Year] or excerpt number). "
                        "3. **Honesty principle**: If the provided context does not contain sufficient information to answer the question, explicitly state: 'The available materials are insufficient to answer this question.' "
                        "4. **Tone and style**: Maintain an objective, neutral, and academic tone."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Extracted paper content:\n{context}\n\n"
                        f"Please answer the following question in {language}:\n{question}\n"
                        "Requirements: 1) Clearly state the conclusion; 2) List the sources of supporting paragraphs."
                    ),
                },
            ],
            temperature=0.2,
        ).strip()

    def answer(self, questions: Sequence[str], language: str = "en") -> Dict[str, List[Dict[str, str]]]:
        log_path = _configure_logger("paper_search_agent")
        logger.info("日志文件: %s", log_path)

        if isinstance(questions, str):
            questions = [questions]

        if not self.paper_records:
            raise RuntimeError(f"未找到可用论文: {self.paper_root}")

        use_rag = len(self.paper_records) >= self.rag_threshold
        logger.info("论文数量=%d, rag_threshold=%d, use_rag=%s", len(self.paper_records), self.rag_threshold, use_rag)

        answers: List[Dict[str, str]] = []
        all_citations: List[Dict[str, str]] = []
        for question in questions:
            if use_rag:
                blocks, citations = self._rag_blocks(str(question))
            else:
                blocks, citations = self._full_context_blocks()
            context = _truncate_blocks(blocks, 100000)
            reply = self._call_llm(context, str(question), language)
            answers.append({"question": str(question), "answer": reply})
            all_citations.extend(citations)

        self.sources_used = all_citations
        return {"answers": answers, "citations": all_citations}
