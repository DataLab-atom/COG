"""papw_vdb_input — papw 向量数据库异步输入源

异步输入源，供 input/*.json 配置引用（type: "input"）。
封装 Chroma VDB 检索 + 重排，不含 LLM 调用。

工具列表：
    papw_vdb_search_rerank — 加载 VDB 并执行检索 + 重排
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from utils import ToolResult

logger = logging.getLogger(__name__)


class PapwVdbSearchResult(ToolResult):
    """papw_vdb_search_rerank() 的返回值。"""

    results: list          # 检索结果列表 [{content, metadata, citation, ...}, ...]
    count: int             # 结果数量
    formatted_text: str    # 格式化的文本摘要（供 agent prompt 使用）
    formatted_text_refs: str  # 格式化的引用文本（含 bib 信息）


async def papw_vdb_search_rerank(
    persist_directory_path: str,
    query: str,
    retrieval_num: int = 10,
    embedding_model: str = "BAAI/bge-m3",
    embedding_model_source: str = "api",
    reranker_path: str = "BAAI/bge-reranker-v2-m3",
    reranker_model_source: str = "api",
    database_device: str = "cpu",
) -> PapwVdbSearchResult:
    """加载 Chroma VDB 并执行检索 + 重排。

    Args:
        persist_directory_path: Chroma 数据库持久化路径。
        query: 检索查询文本。
        retrieval_num: 检索数量。
        embedding_model: 嵌入模型名称。
        embedding_model_source: 'local' 或 'api'。
        reranker_path: 重排模型名称/路径。
        reranker_model_source: 'local' 或 'api'。
        database_device: 设备（如 'cpu'、'cuda:0'）。

    Returns:
        PapwVdbSearchResult: 检索 + 重排后的结果列表。
    """

    def _run() -> list:
        from langchain_community.vectorstores import Chroma
        from utils.papw.embeddings import call_Embedding_model, get_Embedding_model
        from utils.papw.rerank import reranking_intercept

        # 加载嵌入模型
        if embedding_model_source == "local":
            emb = get_Embedding_model(embedding_model, database_device)
        else:
            emb = call_Embedding_model(embedding_model)

        # 加载 VDB
        vectordb = Chroma(persist_directory=persist_directory_path, embedding_function=emb)

        # 检索
        docs = vectordb.similarity_search(query, retrieval_num)

        # 重排
        docs = reranking_intercept(
            query, docs, retrieval_num,
            reranker_path, database_device, reranker_model_source,
        )

        # 转为可序列化的 dict 列表
        results = []
        for doc in docs:
            entry: dict[str, Any] = {
                "content": doc.page_content if hasattr(doc, "page_content") else str(doc),
                "metadata": dict(doc.metadata) if hasattr(doc, "metadata") else {},
            }
            # 提取 citation
            meta = entry["metadata"]
            if "image_references" in meta:
                entry["citation"] = meta["image_references"]
            else:
                entry["citation"] = meta.get("citation", "")
            results.append(entry)

        return results

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, _run)

    # 生成格式化文本
    formatted_parts = []
    formatted_ref_parts = []
    for i, r in enumerate(results):
        content = r.get("content", "")[:1000]
        citation = r.get("citation", "")
        formatted_parts.append(f"Reference {i+1}:\n{content}")
        ref_text = f"Reference {i+1}:\n{content}"
        if citation:
            ref_text += f"\n\nBibTeX:\n{citation}"
        formatted_ref_parts.append(ref_text)

    return PapwVdbSearchResult(
        results=results,
        count=len(results),
        formatted_text="\n\n".join(formatted_parts),
        formatted_text_refs="\n\n---\n\n".join(formatted_ref_parts),
    )
