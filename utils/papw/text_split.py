"""text_split — Markdown 文本按标题分块（替代 Eureka database_building.text.split）"""
from __future__ import annotations

import re

import tiktoken
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownTextSplitter


def _count_tokens(text: str, model_name: str) -> int:
    """使用 tiktoken 计算 token 数（替代 Eureka 的 llm.tokens）。"""
    try:
        enc = tiktoken.encoding_for_model(model_name)
    except KeyError:
        enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def split_text_with_headings(
    text: str,
    reference: dict,
    min_tokens: int,
    max_tokens: int,
    chunk_size: int,
    embedding_model_name: str,
) -> list[Document]:
    """将 Markdown 文本按标题分块，控制每块 token 数在 [min_tokens, max_tokens] 之间。"""
    heading_pattern = re.compile(r"(\n#+ .*\n)")
    marked_text = heading_pattern.sub(r"|||\1|||", text)

    segments: list[Document] = []
    current_segment = ""
    temp_parts = marked_text.split("|||")

    markdown_splitter = MarkdownTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=0,
    )

    def _make_doc(content: str) -> Document:
        return Document(
            page_content=content.strip(),
            metadata={"document_content": content.strip(), "citation": reference},
        )

    def _flush_large(segment: str) -> None:
        """将超长段落进一步分块后加入 segments。"""
        split_docs = markdown_splitter.split_text(segment.strip())
        chunks = ""
        for idx, chunk in enumerate(split_docs):
            chunk_tokens = _count_tokens(chunks, embedding_model_name)
            if min_tokens <= chunk_tokens < max_tokens:
                segments.append(_make_doc(chunks))
                chunks = f"{split_docs[idx - 1]}\n{chunk}"
            else:
                chunks += chunk + "\n"
        segments.append(_make_doc(chunks))

    for part in temp_parts:
        if heading_pattern.match(part):
            if _count_tokens(current_segment, embedding_model_name) > max_tokens:
                _flush_large(current_segment)
                current_segment = part
            else:
                current_tokens = _count_tokens(current_segment, embedding_model_name)
                if chunk_size <= current_tokens < max_tokens:
                    segments.append(_make_doc(current_segment))
                    current_segment = part
                else:
                    current_segment += part
        else:
            current_segment += part

    if current_segment:
        if _count_tokens(current_segment, embedding_model_name) > max_tokens:
            _flush_large(current_segment)
        else:
            segments.append(_make_doc(current_segment))

    return segments
