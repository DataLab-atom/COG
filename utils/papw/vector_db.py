"""vector_db — 向量数据库构建（替代 Eureka database_building.vector_db）"""
from __future__ import annotations

import json
import logging
import os
from functools import partial
from multiprocessing import Pool
from pathlib import Path

from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document

from .common import encode_image, setup_logging
from .embeddings import call_Embedding_model, get_Embedding_model
from .text_split import split_text_with_headings


def get_image_description(
    image_path: str,
    model_name: str = "",
    image_references: str | None = None,
) -> tuple:
    """为图片生成描述。若提供 model_name 则调用 LLM，否则使用文件名作为占位描述。"""
    image_base = encode_image(image_path)

    if model_name:
        from .llm_adapter import LLMClient, system, image_part, user_content
        from .prompts import CHART_DESCRIPTION_SYSTEM, ChartDescription
        client = LLMClient()
        messages = [
            system(CHART_DESCRIPTION_SYSTEM),
            user_content([image_part(image_base, "image/jpeg")]),
        ]
        response = client.call(
            messages,
            model=model_name,
            response_schema=ChartDescription,
        )
        image_description = response["chart_description"]
    else:
        image_description = f"Figure from paper: {os.path.basename(image_path)}"

    return (image_base, image_description, image_path, image_references)


def data_init_processing(
    image_folder_path_list: list[str],
    paper_cite_information: dict,
    config: dict,
    log_file_name: str | None = None,
) -> list[Document]:
    """处理论文 Markdown 和图片，生成向量数据库数据列表。"""
    data_list: list[Document] = []
    for image_folder_path in image_folder_path_list:
        folder_name = os.path.basename(os.path.dirname(image_folder_path))
        dir_path = os.path.dirname(image_folder_path)
        cite = paper_cite_information.get(f"{folder_name}.pdf", "")

        md_path = os.path.join(dir_path, f"{folder_name}.md")
        if os.path.exists(md_path):
            with open(md_path, "r", encoding="utf-8") as f:
                md_content = f.read()
                data_list.extend(
                    split_text_with_headings(
                        md_content,
                        cite,
                        config["text_data_processing"]["min_tokens"],
                        config["text_data_processing"]["max_tokens"],
                        config["text_data_processing"]["chunk_size"],
                        config["embedding_model"],
                    )
                )

        chart_model_name = config.get("chart_model_name", "")
        need_image_description = []
        if os.path.exists(image_folder_path):
            for image_file in os.listdir(image_folder_path):
                need_image_description.append(os.path.join(image_folder_path, image_file))

        if need_image_description and chart_model_name:
            partial_get = partial(
                get_image_description,
                model_name=config["chart_model_name"],
                image_references=cite,
            )

            pool_kwargs = {}
            if log_file_name:
                pool_kwargs = {"initializer": setup_logging, "initargs": (log_file_name,)}

            with Pool(processes=min(5, len(need_image_description)), **pool_kwargs) as pool:
                image_results = pool.map(partial_get, need_image_description)

            for result in image_results:
                data_list.append(
                    Document(
                        page_content=result[1],
                        metadata={
                            "image_base64": result[0],
                            "image_path": result[2],
                            "image_references": result[3],
                        },
                    )
                )

    return data_list


def build_paper_vb(
    pdf_path_list: list[str],
    paper_cite_information: dict,
    persist_directory_path: str,
    config: dict,
    log_file_name: str | None = None,
):
    """构建论文向量数据库。"""
    folder_path_list = []
    for pdf_path in pdf_path_list:
        folder_path_list.append(
            os.path.join(config["save_md_path"], Path(pdf_path).stem, "images")
        )

    vectordb_data_list = data_init_processing(
        folder_path_list, paper_cite_information, config, log_file_name
    )

    if config["embedding_model_source"] == "local":
        embedding_model = get_Embedding_model(config["embedding_model"], config["database_device"])
    else:
        embedding_model = call_Embedding_model(config["embedding_model"])

    vectordb = Chroma.from_documents(
        documents=vectordb_data_list,
        embedding=embedding_model,
        persist_directory=persist_directory_path,
    )

    return vectordb


def build_plotcode_vb(config: dict):
    """构建 plot code 向量数据库。"""
    vectordb_data_list = []

    if not os.path.exists(config["plot_code_template"]):
        logging.warning("plot_code_template not found: %s", config["plot_code_template"])
        return None

    for template in os.listdir(config["plot_code_template"]):
        template_dir = os.path.join(config["plot_code_template"], template)
        plot_code_path = os.path.join(template_dir, "plot_code.py")
        caption_path = os.path.join(template_dir, "caption_explain.md")

        if not os.path.exists(plot_code_path) or not os.path.exists(caption_path):
            continue

        with open(plot_code_path, "r", encoding="utf-8") as f:
            plot_code = f.read()
        with open(caption_path, "r", encoding="utf-8") as f:
            caption_explain = f.read()

        charts = []
        charts_dir = os.path.join(template_dir, "charts")
        if os.path.exists(charts_dir):
            for chart in os.listdir(charts_dir):
                charts.append(os.path.join(charts_dir, chart))

        vectordb_data_list.append(
            Document(
                page_content=caption_explain,
                metadata={
                    "plot_code": plot_code,
                    "charts": json.dumps(charts),
                },
            )
        )

    if not vectordb_data_list:
        logging.warning("no plot code templates found")
        return None

    if config["embedding_model_source"] == "local":
        embedding_model = get_Embedding_model(config["embedding_model"], config["database_device"])
    else:
        embedding_model = call_Embedding_model(config["embedding_model"])

    vectordb = Chroma.from_documents(
        documents=vectordb_data_list,
        embedding=embedding_model,
        persist_directory=config["PlotCode_vb_path"],
    )

    logging.info("plot code vector database created at %s", config["PlotCode_vb_path"])
    return vectordb


def count_vector_database(vectordb) -> None:
    """统计向量数据库中的记录数和论文数。"""
    all_data = vectordb.get()
    data_count = len(all_data["ids"])
    logging.info("vector database contains %s records", data_count)

    paper_bib = set()
    for metadata in all_data["metadatas"]:
        if "image_references" in metadata:
            paper_bib.add(str(metadata["image_references"]))
        else:
            paper_bib.add(str(metadata.get("citation", "")))

    logging.info("vector database contains %s papers", len(paper_bib))
