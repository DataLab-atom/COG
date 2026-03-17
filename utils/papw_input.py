"""papw_input — papw 基础设施异步输入函数

异步输入函数，供 input/*.json 配置引用（type: "input", async: true）。

仅保留基础设施阶段（文献下载、数据库构建），写作/实验模块已迁移至 graphs/ 图编排。

函数列表：
    papw_build_references     — 下载参考文献 PDF（paper_crawler）并可选调用 Semantic Scholar 扩充
    papw_build_database       — 将 PDF 解析为 Markdown 并建立 Chroma 向量数据库
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import re as _re

from utils import ToolResult

_executor = ThreadPoolExecutor(max_workers=4)


def _extract_english_query(text: str) -> str:
    """从中英文混合文本中提取英文片段，拼接为 Semantic Scholar 搜索词。

    Semantic Scholar 不支持中文查询（返回 403），因此需提取英文关键词。
    """
    # 提取连续的英文 + 数字 + 常见标点片段（保留括号内容如 "k-nearest neighbor"）
    fragments = _re.findall(r"[A-Za-z][A-Za-z0-9\s\-_/().,']+", text)
    # 去除过短的碎片（如单个字母）
    meaningful = [f.strip() for f in fragments if len(f.strip()) > 2]
    query = " ".join(meaningful)
    # 截断过长的查询（S2 API 有长度限制）
    if len(query) > 300:
        query = query[:300]
    return query


def _makedirs(*paths: str) -> None:
    for p in paths:
        os.makedirs(p, exist_ok=True)


async def _run_blocking(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, partial(fn, *args, **kwargs))


# ── 返回值类型 ─────────────────────────────────────────────────────────────────


class PapwBuildReferencesResult(ToolResult):
    bib_path: str        # 最终合并后的 .bib 文件路径
    paper_pdf_dir: str   # 下载的 PDF 所在目录
    paper_count: int     # 合并后 bib 中的文献条目数


class PapwBuildDatabaseResult(ToolResult):
    experiment_vb_path: str   # 用于 experimenting 的 Chroma DB 路径
    full_text_vb_path: str    # 用于章节写作的 Chroma DB 路径（full text）
    md_dir: str               # 论文 Markdown 文件目录（供 experimenting 使用）


# ── papw_build_references ─────────────────────────────────────────────────────


async def papw_build_references(
    papw_root: str,
    requirement: str,
    s2_query: str = "",
    base_bib_path: str = "",
    expand_references: bool = True,
    s2_api_key: str = "",
    all_paper_num: int = 70,
    unpaywall_email: str = "",
) -> PapwBuildReferencesResult:
    """下载参考文献 PDF，并可选通过 Semantic Scholar 在现有 bib 基础上扩充。

    Args:
        papw_root:          papw 工作根目录（= {output_dir}/papw）。
        requirement:        需求描述（原始文本，可为中文）。
        s2_query:           英文搜索关键词，由 s2_query_gen agent 生成。为空时回退到从 requirement 提取。
        base_bib_path:      用户提供的初始 .bib 文件路径（可为空串）。
        expand_references:  是否调用 Semantic Scholar 在 base_bib 基础上扩充文献。
        s2_api_key:         Semantic Scholar API Key（expand_references=True 时使用）。
        all_paper_num:      目标参考文献总数。
    """


    pdf_dir = os.path.join(papw_root, "database", "pdf")
    bib_out = os.path.join(papw_root, "reference.bib")
    _makedirs(pdf_dir, os.path.dirname(bib_out))

    def _build():
        import bibtexparser
        from bibtexparser.bibdatabase import BibDatabase
        from bibtexparser.bwriter import BibTexWriter

        # 1. 加载 base bib
        db = BibDatabase()
        if base_bib_path and os.path.exists(base_bib_path):
            with open(base_bib_path, encoding="utf-8") as f:
                db = bibtexparser.load(f)
            logging.info(f"已加载基础 bib：{len(db.entries)} 条")

        # 2. 可选：Semantic Scholar 扩充
        if expand_references and s2_api_key:
            try:
                from semanticscholar import SemanticScholar
                sch = SemanticScholar(api_key=s2_api_key)
                # 优先使用 validation_agent 解析出的英文 metric_name，回退到正则提取
                query = s2_query or _extract_english_query(requirement)
                if not query:
                    logging.warning("无可用英文搜索词，跳过 Semantic Scholar 扩充")
                    raise ValueError("无可用英文查询词")
                logging.info(f"Semantic Scholar 搜索词: {query}")
                results = sch.search_paper(query, limit=all_paper_num, fields=["title", "authors", "year", "externalIds"])
                existing_titles = {e.get("title", "").lower() for e in db.entries}
                added = 0
                for paper in results:
                    title = paper.title or ""
                    if title.lower() in existing_titles:
                        continue
                    authors = " and ".join(a.name for a in (paper.authors or []))
                    year = str(paper.year or "")
                    key = f"ss_{added:04d}"
                    db.entries.append({
                        "ENTRYTYPE": "article",
                        "ID": key,
                        "title": title,
                        "author": authors,
                        "year": year,
                    })
                    existing_titles.add(title.lower())
                    added += 1
                logging.info(f"Semantic Scholar 新增 {added} 条文献")
            except Exception as e:
                logging.warning(f"Semantic Scholar 扩充失败（跳过）: {e}")

        # 3. 写出合并 bib
        writer = BibTexWriter()
        with open(bib_out, "w", encoding="utf-8") as f:
            f.write(writer.write(db))
        logging.info(f"合并后 bib 写入 {bib_out}，共 {len(db.entries)} 条")

        # 4. 用 paper_crawler 下载 PDF（启用缓存）
        paper_cache_dir = os.path.join(papw_root, "database", ".paper_cache")
        if db.entries:
            try:
                from utils.papw.paper_crawler import paper_crawler
                paper_crawler(bib_out, pdf_dir, [], [], {}, {}, "file", len(db.entries),
                              unpaywall_email=unpaywall_email, cache_dir=paper_cache_dir)
            except Exception as e:
                logging.warning(f"paper_crawler 下载失败（跳过）: {e}")

        return len(db.entries)

    paper_count = await _run_blocking(_build)
    return PapwBuildReferencesResult(bib_path=bib_out, paper_pdf_dir=pdf_dir, paper_count=paper_count)


# ── papw_build_database ───────────────────────────────────────────────────────


async def papw_build_database(
    papw_root: str,
    bib_path: str,
    embedding_model: str = "BAAI/bge-m3",
    embedding_model_source: str = "api",
    pdf_transform_mode: str = "api",
    api_parsing_method: str = "file",
    database_device: str = "cpu",
    pdf_md_token: str = "",
    s2_api_key: str = "",
    all_paper_num: int = 70,
    unpaywall_email: str = "",
) -> PapwBuildDatabaseResult:
    """解析 PDF 并建立用于实验分析和章节写作的双 Chroma 向量数据库。

    LLM 依赖已移除：图表描述和文献去重改用非 LLM 方案，符合 input 源规范。

    Args:
        papw_root:            papw 工作根目录。
        bib_path:             合并后的 .bib 文件路径（papw_build_references 输出）。
        embedding_model:      嵌入模型名称。
        embedding_model_source: 'local' 或 'api'。
        pdf_transform_mode:   PDF 解析模式 'local' 或 'api'。
        api_parsing_method:   API 解析方式 'file' 或 'url'。
        database_device:      'cpu' 或 'cuda:N'。
        s2_api_key:           Semantic Scholar API Key。
        all_paper_num:        最大论文数。
        unpaywall_email:      Unpaywall API 邮箱。
    """


    pdf_dir = os.path.join(papw_root, "database", "pdf")
    md_dir = os.path.join(papw_root, "database", "md")
    experiment_vb = os.path.join(papw_root, "database", "vector_experiment")
    full_text_vb = os.path.join(papw_root, "database", "vector_full")
    plot_code_template = os.path.join(papw_root, "plot_code_template")
    _makedirs(pdf_dir, md_dir, experiment_vb, full_text_vb,
              os.path.join(papw_root, "log", "database_building"))

    config = {
        "log_path":                         os.path.join(papw_root, "log", "database_building"),
        "bib_path":                         bib_path,
        "save_pdf_path":                    pdf_dir,
        "save_md_path":                     md_dir,
        "vector_database_paper_information": os.path.join(papw_root, "database", "paper_info.json"),
        "embedding_model":                  embedding_model,
        "embedding_model_source":           embedding_model_source,
        "pdf_transform_mode":               pdf_transform_mode,
        "api_parsing_method":               api_parsing_method,
        "plot_code_template":               plot_code_template,
        "experiment_persist_directory_path": experiment_vb,
        "full_text_persist_directory_path":  full_text_vb,
        "PlotCode_vb_path":                 os.path.join(papw_root, "database", "vector_plot"),
        "text_data_processing":             {"min_tokens": 1300, "max_tokens": 2400, "chunk_size": 700},
        "pdf_md_token":                     pdf_md_token,
        "database_device":                  database_device,
        "chart_model_name":                 "",
        "judge_model_name":                 "",
        "temperature":                      1,
        "all_paper_num":                    all_paper_num,
        "S2_API_KEY":                       s2_api_key,
        "unpaywall_email":                  unpaywall_email,
        "paper_cache_dir":                  os.path.join(papw_root, "database", ".paper_cache"),
    }

    def _build():
        from utils.papw.database_building import database_building
        database_building(config)

    await _run_blocking(_build)
    return PapwBuildDatabaseResult(
        experiment_vb_path=experiment_vb,
        full_text_vb_path=full_text_vb,
        md_dir=md_dir,
    )

