"""embeddings — 嵌入模型适配（替代 Eureka llm.embeddings）"""
from __future__ import annotations

import time
from typing import List

import requests
from langchain.embeddings.base import Embeddings
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_openai import OpenAIEmbeddings


def _get_settings():
    from config import settings
    return settings


def _retrieve_url() -> str:
    s = _get_settings()
    return s.get("RETRIEVE_URL", "") or s.get("EMBEDDING_API_URL", "") or "https://api.siliconflow.cn/v1/embeddings"


def _rerank_retrieve_key() -> str:
    s = _get_settings()
    return s.get("RERANK_RETRIEVE_KEY", "") or s.get("EMBEDDING_API_KEY", "") or ""


class SiliconFlowEmbeddings(Embeddings):
    """SiliconFlow 嵌入 API 适配。"""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.api_token = _rerank_retrieve_key()
        self.url = _retrieve_url()

    def _get_embeddings(self, texts: List[str]) -> List[List[float]]:
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model_name, "input": texts}
        response = requests.post(self.url, json=payload, headers=headers, timeout=60)
        time.sleep(0.5)
        if response.status_code == 200:
            result = response.json()
            embeddings = [item["embedding"] for item in sorted(result["data"], key=lambda x: x["index"])]
            return embeddings
        raise RuntimeError(f"embedding API failed: {response.status_code} {response.text[:200]}")

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        batch_size = 10
        all_embeddings: List[List[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            embeddings = self._get_embeddings(batch)
            all_embeddings.extend(embeddings)
        return all_embeddings

    def embed_query(self, text: str) -> List[float]:
        return self._get_embeddings([text])[0]


def get_Embedding_model(model_name: str, database_device: str) -> HuggingFaceEmbeddings:
    """本地 HuggingFace 嵌入模型。"""
    return HuggingFaceEmbeddings(
        model_name=model_name,
        model_kwargs={"device": database_device},
        encode_kwargs={"normalize_embeddings": True, "batch_size": 2},
    )


def call_Embedding_model(model_name: str):
    """根据模型名称选择嵌入实现。"""
    if model_name in ["BAAI/bge-m3", "BAAI/bge-large-en-v1.5"]:
        return SiliconFlowEmbeddings(model_name)
    return OpenAIEmbeddings(
        model=model_name,
        api_key=_rerank_retrieve_key(),
        base_url=_retrieve_url(),
    )
