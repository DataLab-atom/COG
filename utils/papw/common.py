"""common — 公共工具函数（替代 Eureka common/ 子模块）"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import bibtexparser


# ── 文件系统 ─────────────────────────────────────────────────────────────────

def clear_folder(folder_path: str) -> None:
    if os.path.exists(folder_path):
        shutil.rmtree(folder_path)
    os.makedirs(folder_path, exist_ok=True)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# ── 图片 ─────────────────────────────────────────────────────────────────────

def encode_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# ── 日志 ─────────────────────────────────────────────────────────────────────

def setup_logging(log_file_name: str) -> None:
    log_file = Path(log_file_name)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.touch(exist_ok=True)

    try:
        from concurrent_log_handler import ConcurrentRotatingFileHandler
        file_handler = ConcurrentRotatingFileHandler(
            str(log_file), maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
    except ImportError:
        file_handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(filename)s - %(levelname)s - %(message)s",
        handlers=[file_handler, logging.StreamHandler(sys.stdout)],
        force=True,
    )


# ── 写作工具 ─────────────────────────────────────────────────────────────────

def build_writing_requirement(our_method_path: str, research_information_path: str) -> str:
    """构建写作需求描述（方法代码 + 研究信息）。"""
    parts = []
    if os.path.exists(our_method_path):
        with open(our_method_path, encoding="utf-8") as f:
            parts.append(f"Method Code:\n{f.read()}")
    if os.path.exists(research_information_path):
        with open(research_information_path, encoding="utf-8") as f:
            info = json.load(f)
            parts.append(f"Research Information:\n{json.dumps(info, ensure_ascii=False, indent=2)}")
    return "\n\n".join(parts)


def text_image_separate(refer_data) -> Tuple[List, List]:
    """将检索结果按是否含图片分为文本列表和图片列表。"""
    text_list = []
    image_list = []
    for doc in refer_data:
        if hasattr(doc, "metadata") and doc.metadata.get("image_base64"):
            image_list.append(doc)
        else:
            text_list.append(doc)
    return text_list, image_list


def format_reference(refer_data) -> Tuple[List[dict], List[dict], str]:
    """将检索到的文献数据格式化为结构化引用列表。"""
    text_refs = []
    image_refs = []
    cite_text = ""
    for i, doc in enumerate(refer_data):
        ref = {
            "index": i,
            "content": doc.page_content if hasattr(doc, "page_content") else str(doc),
            "metadata": doc.metadata if hasattr(doc, "metadata") else {},
        }
        if ref["metadata"].get("image_base64"):
            image_refs.append(ref)
        else:
            text_refs.append(ref)
        cite_info = ref["metadata"].get("reference", "")
        if cite_info:
            cite_text += f"[{i}] {cite_info}\n"
    return text_refs, image_refs, cite_text


def _extract_bib_title(bib_content: str) -> str | None:
    """从 bib entry 中提取 title。"""
    try:
        db = bibtexparser.loads(bib_content)
        if db.entries:
            return db.entries[0].get("title")
    except Exception:
        pass
    return None


def check_repeated_cite(
    refer_data,
    all_paper_title_list: Sequence,
    current_used_paper_title: Sequence,
) -> List:
    """过滤掉已经引用过的文献。"""
    filtered = []
    for doc in refer_data:
        title = ""
        if hasattr(doc, "metadata"):
            title = doc.metadata.get("title", "")
        if title and title.lower() in {t.lower() for t in current_used_paper_title}:
            continue
        filtered.append(doc)
    return filtered
