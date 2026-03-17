"""rerank — 重排模型适配（替代 Eureka llm.rerank）"""
from __future__ import annotations

import logging
from typing import List

import requests
from langchain_core.documents import Document

logger = logging.getLogger(__name__)


def _get_settings():
    from config import settings
    return settings


def _rerank_url() -> str:
    s = _get_settings()
    return s.get("RERANK_URL", "") or "https://api.siliconflow.cn/v1/rerank"


def _rerank_key() -> str:
    s = _get_settings()
    return s.get("RERANK_RETRIEVE_KEY", "") or ""


def get_reranker_result(model_name: str, query: str, documents: List[str], n: int):
    payload = {
        "model": model_name,
        "query": query,
        "documents": documents,
        "top_n": n,
    }
    headers = {
        "Authorization": f"Bearer {_rerank_key()}",
        "Content-Type": "application/json",
    }
    response = requests.post(_rerank_url(), json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()["results"]


def reranking_intercept(
    query: str,
    data: List[Document],
    k: int,
    reranker_path: str,
    device: str,
    reranker_model_source: str,
) -> List[Document]:
    if not data or k <= 0:
        return []
    if k > len(data):
        k = len(data)

    if reranker_model_source == "local":
        from FlagEmbedding import FlagReranker
        reranking_model = FlagReranker(reranker_path, devices=device)
        scores = reranking_model.compute_score([(query, doc.page_content) for doc in data])
        reranked_data = [doc for _, doc in sorted(zip(scores, data), reverse=True)]
        return reranked_data[:k]

    text_content = [doc.page_content for doc in data]
    try:
        scores = get_reranker_result(reranker_path, query, text_content, k)
        return [data[score["index"]] for score in scores]
    except Exception as exc:
        logger.warning("reranker API failed, fallback to original order: %s", exc)
        return data[:k]
