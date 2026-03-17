"""database_building — 数据库构建主流程（替代 Eureka database_building.core）"""
from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import bibtexparser
from bibtexparser.bibdatabase import BibDatabase
from bibtexparser.bwriter import BibTexWriter
from semanticscholar import SemanticScholar

from .common import clear_folder
from .paper_crawler import paper_crawler
from .pdf_extract import pdf_extractor
from .scholar import check_reference_repeat
from .vector_db import build_paper_vb, build_plotcode_vb, count_vector_database


def _prepare_directories(config: dict) -> None:
    os.makedirs(config["log_path"], exist_ok=True)
    clear_folder(config["save_pdf_path"])
    clear_folder(config["save_md_path"])
    clear_folder(config["experiment_persist_directory_path"])

    if os.path.exists(config["full_text_persist_directory_path"]):
        shutil.rmtree(config["full_text_persist_directory_path"])
    os.makedirs(config["full_text_persist_directory_path"], exist_ok=True)


def _init_logging(log_path: str) -> str:
    log_file_name = os.path.join(
        log_path, f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    log_file = Path(log_file_name)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.touch(exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(filename)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file_name, mode="a", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return log_file_name


def database_building(config: dict) -> None:
    """数据库构建主入口。"""
    log_file_name = _init_logging(config["log_path"])
    _prepare_directories(config)

    # 构建 plot code 向量数据库
    if os.path.exists(config.get("PlotCode_vb_path", "")):
        logging.info("plot code vector database already exists: %s", config["PlotCode_vb_path"])
    else:
        plot_vdb = build_plotcode_vb(config)
        if plot_vdb:
            logging.info("plot code vdb records: %d", len(plot_vdb.get()["ids"]))

    if "cpu" not in config.get("database_device", "cpu"):
        os.environ["CUDA_VISIBLE_DEVICES"] = config["database_device"].split(":")[-1]
        config["database_device"] = "cuda"

    downloaded_paper_title_list: list = []
    downloaded_paper_id_list: list = []
    downloaded_paper_key_list: list = []
    downloaded_paper_list: list = []
    downloaded_paper_information_bib: dict = {}
    downloaded_paper_information: dict = {}

    # 论文缓存目录（与 pdf/ 同级，不会被 clear_folder 清除）
    cache_dir = config.get("paper_cache_dir", "")

    # 第一轮：下载 bib 中的初始论文
    paper_crawler(
        config["bib_path"],
        config["save_pdf_path"],
        downloaded_paper_title_list,
        downloaded_paper_key_list,
        downloaded_paper_information,
        downloaded_paper_information_bib,
        config["api_parsing_method"],
        config["all_paper_num"],
        unpaywall_email=config.get("unpaywall_email", ""),
        cache_dir=cache_dir,
    )

    logging.info("processing initial papers")
    used_experiment_pdf_path = []
    for root, _, files in os.walk(config["save_pdf_path"]):
        for file in files:
            used_experiment_pdf_path.append(os.path.join(root, file))

    # PDF -> Markdown
    try:
        pdf_extractor(
            used_experiment_pdf_path,
            config["save_md_path"],
            config["pdf_transform_mode"],
            config["api_parsing_method"],
            config.get("pdf_md_token"),
        )
    except Exception as exc:
        logging.error("PDF 解析失败（继续构建已有数据）: %s", exc)

    # 构建实验用向量数据库
    logging.info("building vector database for experiment papers")
    vectordb = build_paper_vb(
        used_experiment_pdf_path,
        downloaded_paper_information_bib,
        config["experiment_persist_directory_path"],
        config,
        log_file_name,
    )
    count_vector_database(vectordb)

    # 复制为全文向量数据库的基础
    if os.path.exists(config["full_text_persist_directory_path"]):
        shutil.rmtree(config["full_text_persist_directory_path"])
    shutil.copytree(
        config["experiment_persist_directory_path"],
        config["full_text_persist_directory_path"],
    )

    # 第二轮：通过 Semantic Scholar 扩展引用/被引
    field_list = [
        "references", "citations", "paperId", "title",
        "citationStyles", "externalIds", "openAccessPdf",
    ]

    for idx, paper_title in enumerate(downloaded_paper_title_list):
        try:
            sch = SemanticScholar(api_key=config.get("S2_API_KEY", ""))
            results = sch.search_paper(paper_title, limit=1, fields=field_list)
            time.sleep(1)

            if not results or len(results) == 0:
                downloaded_paper_title_list[idx] = [paper_title]
                logging.warning("paper not found in SemanticScholar: %s", paper_title)
                continue
            downloaded_paper_title_list[idx] = [results[0]["title"], paper_title]
            downloaded_paper_list.append(results[0])
            downloaded_paper_id_list.append(results[0]["paperId"])
        except Exception as exc:
            downloaded_paper_title_list[idx] = [paper_title]
            logging.warning("SemanticScholar search failed for %s: %s", paper_title, exc)

    tmp_bib_path = os.path.join(
        os.path.dirname(config["vector_database_paper_information"]), "tmp_bib.bib"
    )

    for current_paper in downloaded_paper_list:
        if len(downloaded_paper_title_list) >= config["all_paper_num"]:
            break

        # 处理 references
        _process_related_papers(
            current_paper, "references", field_list, config,
            downloaded_paper_title_list, downloaded_paper_id_list,
            downloaded_paper_key_list, downloaded_paper_list,
            downloaded_paper_information, downloaded_paper_information_bib,
            tmp_bib_path, cache_dir=cache_dir,
        )

        if len(downloaded_paper_title_list) >= config["all_paper_num"]:
            break

        # 处理 citations
        _process_related_papers(
            current_paper, "citations", field_list, config,
            downloaded_paper_title_list, downloaded_paper_id_list,
            downloaded_paper_key_list, downloaded_paper_list,
            downloaded_paper_information, downloaded_paper_information_bib,
            tmp_bib_path, cache_dir=cache_dir,
        )

    if os.path.exists(tmp_bib_path):
        os.remove(tmp_bib_path)

    with open(config["vector_database_paper_information"], "w", encoding="utf-8") as f:
        json.dump(downloaded_paper_information, f, indent=4, ensure_ascii=False)

    # 处理扩展论文
    logging.info("processing full text papers")
    used_full_text_pdf_path = []
    for file in sorted(os.listdir(config["save_pdf_path"]))[len(used_experiment_pdf_path):]:
        used_full_text_pdf_path.append(os.path.join(config["save_pdf_path"], file))

    if used_full_text_pdf_path:
        try:
            pdf_extractor(
                used_full_text_pdf_path,
                config["save_md_path"],
                config["pdf_transform_mode"],
                config["api_parsing_method"],
                config.get("pdf_md_token"),
            )
        except Exception as exc:
            logging.error("扩展论文 PDF 解析失败（继续构建已有数据）: %s", exc)

        vectordb = build_paper_vb(
            used_full_text_pdf_path,
            downloaded_paper_information_bib,
            config["full_text_persist_directory_path"],
            config,
            log_file_name,
        )
        count_vector_database(vectordb)

    logging.info("database_building done")


def _process_related_papers(
    current_paper,
    relation_type: str,
    field_list: list,
    config: dict,
    downloaded_paper_title_list: list,
    downloaded_paper_id_list: list,
    downloaded_paper_key_list: list,
    downloaded_paper_list: list,
    downloaded_paper_information: dict,
    downloaded_paper_information_bib: dict,
    tmp_bib_path: str,
    cache_dir: str = "",
) -> None:
    """处理当前论文的 references 或 citations。"""
    related = getattr(current_paper, relation_type, None)
    if not related:
        return

    tmp_bib = ""
    tmp_paper_id_list = []
    tmp_paper_title_list: list[list] = []
    tmp_paper_key_list = []
    tmp_downloaded_paper_list = []

    logging.info("current paper %s: %s", relation_type, current_paper.title)

    for ref in related[:50]:
        sch = SemanticScholar(api_key=config.get("S2_API_KEY", ""))

        paper = None
        try:
            paper = sch.get_paper(ref["paperId"], fields=field_list)
            time.sleep(1)
        except Exception as exc:
            logging.warning("get_paper failed for %s: %s", ref.get("title", ""), exc)

        if not paper:
            try:
                results = sch.search_paper(ref["title"], limit=1, fields=field_list)
                time.sleep(1)
                if results and len(results) > 0:
                    paper = results[0]
                else:
                    continue
            except Exception:
                continue

        try:
            parser = bibtexparser.bparser.BibTexParser()
            bib_db = bibtexparser.loads(paper.citationStyles["bibtex"], parser=parser)
            entry = bib_db.entries[0]
        except Exception:
            continue

        if hasattr(paper, "externalIds") and paper.get("externalIds"):
            if paper.externalIds.get("DOI"):
                entry["doi"] = paper.externalIds["DOI"]

        if hasattr(paper, "openAccessPdf") and paper.get("openAccessPdf"):
            if paper.openAccessPdf.get("url"):
                entry["url"] = paper.openAccessPdf["url"]

        tmp_db = BibDatabase()
        tmp_db.entries = [entry]
        writer = BibTexWriter()
        bib = writer.write(tmp_db)

        if not check_reference_repeat(
            [paper.title, ref["title"], entry["title"]],
            paper["paperId"],
            entry["ID"],
            bib,
            downloaded_paper_title_list,
            downloaded_paper_id_list,
            downloaded_paper_key_list,
            downloaded_paper_information,
            tmp_paper_title_list,
            tmp_paper_id_list,
            tmp_paper_key_list,
            tmp_bib,
            config,
        ):
            tmp_bib += bib + "\n"
            tmp_paper_id_list.append(paper["paperId"])
            tmp_downloaded_paper_list.append(paper)
            tmp_paper_title_list.append([paper.title, ref["title"], entry["title"]])
            tmp_paper_key_list.append(entry["ID"])

    with open(tmp_bib_path, "w", encoding="utf-8") as f:
        f.write(tmp_bib)

    paper_crawler(
        tmp_bib_path,
        config["save_pdf_path"],
        downloaded_paper_title_list,
        downloaded_paper_key_list,
        downloaded_paper_information,
        downloaded_paper_information_bib,
        config["api_parsing_method"],
        config["all_paper_num"],
        tmp_paper_id_list,
        tmp_downloaded_paper_list,
        tmp_paper_title_list,
        unpaywall_email=config.get("unpaywall_email", ""),
        cache_dir=cache_dir,
    )

    downloaded_paper_id_list.extend(tmp_paper_id_list)
    downloaded_paper_list.extend(tmp_downloaded_paper_list)
