"""pdf_extract — PDF 解析与 Markdown 后处理（替代 Eureka database_building.pipeline）"""
from __future__ import annotations

import logging
import os
import time
import zipfile
from pathlib import Path

import requests
from PIL import Image

from .common import encode_image


def _get_settings():
    from config import settings
    return settings


def _resolve_pdf_md_token(pdf_md_token: str | None) -> str:
    """获取 MineRU API Token。"""
    if pdf_md_token:
        return pdf_md_token
    s = _get_settings()
    token = s.get("PDF_MD_TOKEN", "")
    if not token:
        raise ValueError("PDF_MD_TOKEN 未配置")
    return token


# ── 图片压缩与 Markdown 后处理 ─────────────────────────────────────────────


def chart_squeezing(chart_path: str, quality: int = 70) -> None:
    """压缩图片尺寸和质量。"""
    try:
        with Image.open(chart_path) as img:
            original_width, original_height = img.size
            new_width = int(original_width * 0.7)
            new_height = int(original_height * 0.7)
            resized_img = img.resize((new_width, new_height), Image.LANCZOS)
            resized_img.save(chart_path, "JPEG", optimize=True, quality=quality)
    except Exception as exc:
        logging.error("chart_squeezing failed for %s: %s", chart_path, exc)


def data_dealing(save_result_path: str, start_num: int) -> None:
    """重命名图片并更新 Markdown 中的引用路径。"""
    for markdown_folder in sorted(os.listdir(save_result_path))[start_num:]:
        markdown_folder_path = os.path.join(save_result_path, markdown_folder)
        images_folder_path = os.path.join(markdown_folder_path, "images")

        md_file = os.path.join(markdown_folder_path, f"{markdown_folder}.md")
        if not os.path.exists(md_file):
            continue

        with open(md_file, "r", encoding="utf-8") as f:
            old_md_content = f.read()

        if not os.path.exists(images_folder_path):
            continue

        all_image_name = os.listdir(images_folder_path)
        idx = 0
        for image_file in all_image_name:
            chart_squeezing(os.path.join(images_folder_path, image_file))

            if old_md_content.find(f"![](images/{image_file})") != -1:
                new_name = f"image_{idx:03d}.jpg"
                os.rename(
                    os.path.join(images_folder_path, image_file),
                    os.path.join(images_folder_path, new_name),
                )
                new_image_path = os.path.join(images_folder_path, new_name)
                old_md_content = old_md_content.replace(
                    f"![](images/{image_file})", f"![]({new_image_path})"
                )
                idx += 1
            else:
                os.remove(os.path.join(images_folder_path, image_file))

        with open(md_file, "w", encoding="utf-8") as f:
            f.write(old_md_content)


# ── API PDF 解析 ──────────────────────────────────────────────────────────


def _mineru_batch_request_with_retry(
    url: str, header: dict, data: dict, max_retries: int = 3
) -> dict:
    """向 MineRU batch API 发送请求，带指数退避重试。

    Args:
        max_retries: 最大重试次数（不含首次请求）。

    Returns:
        API 返回的 JSON data 字段。

    Raises:
        RuntimeError: 所有重试均失败后抛出。
    """
    last_error = None
    for attempt in range(1 + max_retries):
        try:
            response = requests.post(url, headers=header, json=data, timeout=30)
            if response.status_code != 200:
                last_error = (
                    f"MineRU batch API HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
                logging.warning(
                    "MineRU batch API 请求失败 (attempt %d/%d): %s",
                    attempt + 1, 1 + max_retries, last_error,
                )
                if attempt < max_retries:
                    time.sleep(2 ** (attempt + 1))
                continue

            result = response.json()
            logging.info("MineRU batch response: %s", result)

            if result.get("code") == 0:
                return result["data"]

            # API 返回非零 code，记录详情
            last_error = (
                f"MineRU API code={result.get('code')}, "
                f"msg={result.get('msg', result.get('message', ''))}"
            )
            logging.warning(
                "MineRU apply upload url 失败 (attempt %d/%d): %s",
                attempt + 1, 1 + max_retries, last_error,
            )
        except requests.RequestException as exc:
            last_error = f"MineRU batch API 网络异常: {exc}"
            logging.warning(
                "MineRU batch API 网络异常 (attempt %d/%d): %s",
                attempt + 1, 1 + max_retries, exc,
            )

        if attempt < max_retries:
            time.sleep(2 ** (attempt + 1))

    raise RuntimeError(f"MineRU apply upload url failed after {1 + max_retries} attempts: {last_error}")


def load_extractor_pdf(
    pdf_data_path_list: list, save_result_path: str, pdf_md_token: str | None
) -> None:
    """通过 MineRU API 上传 PDF 文件并获取解析结果。"""
    token = _resolve_pdf_md_token(pdf_md_token)
    url = "https://mineru.net/api/v4/file-urls/batch"
    header = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = {
        "language": "en",
        "files": [
            {"name": f"paper_{idx:03}.pdf", "data_id": f"paper_{idx:03}"}
            for idx in range(len(pdf_data_path_list))
        ],
    }

    resp_data = _mineru_batch_request_with_retry(url, header, data)
    batch_id = resp_data["batch_id"]
    urls = resp_data["file_urls"]

    for i, upload_url in enumerate(urls):
        with open(pdf_data_path_list[i], "rb") as f:
            res_upload = requests.put(upload_url, data=f)
            if res_upload.status_code != 200:
                raise RuntimeError(f"upload failed for {pdf_data_path_list[i]}")
            logging.info("upload success: %s", pdf_data_path_list[i])

    # 轮询等待解析完成
    poll_url = f"https://mineru.net/api/v4/extract-results/batch/{batch_id}"
    poll_header = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    while True:
        res = requests.get(poll_url, headers=poll_header)
        extract_results = res.json()["data"]["extract_result"]
        if all(state["state"] == "done" for state in extract_results):
            break
        time.sleep(15)

    for idx in range(len(pdf_data_path_list)):
        folder_name = Path(pdf_data_path_list[idx]).stem
        download_url = extract_results[idx]["full_zip_url"]
        resp = requests.get(download_url, proxies={"http": None, "https": None})
        folder_path = os.path.join(save_result_path, folder_name)
        os.makedirs(folder_path, exist_ok=True)
        zip_path = os.path.join(folder_path, "result.zip")

        with open(zip_path, "wb") as f:
            f.write(resp.content)

        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(folder_path)
        os.remove(zip_path)

        for name in os.listdir(folder_path):
            if name == "images":
                continue
            full = os.path.join(folder_path, name)
            if name.endswith(".md"):
                os.rename(full, os.path.join(folder_path, f"{folder_name}.md"))
            else:
                os.remove(full)


def extractor_zip(pdf_data_path_list: list, save_result_path: str) -> None:
    """解压预解析的 zip 文件。"""
    for idx in range(len(pdf_data_path_list)):
        folder_name = Path(pdf_data_path_list[idx]).stem
        folder_path = os.path.join(save_result_path, folder_name)
        os.makedirs(folder_path, exist_ok=True)

        with zipfile.ZipFile(pdf_data_path_list[idx], "r") as zip_ref:
            zip_ref.extractall(folder_path)
        os.remove(pdf_data_path_list[idx])

        for name in os.listdir(folder_path):
            if name == "images":
                continue
            full = os.path.join(folder_path, name)
            if name.endswith(".pdf"):
                os.rename(full, os.path.join(os.path.dirname(pdf_data_path_list[idx]), f"{folder_name}.pdf"))
            elif name.endswith(".md"):
                os.rename(full, os.path.join(folder_path, f"{folder_name}.md"))
            else:
                os.remove(full)


def pdf_extractor_local(pdf_data_path_list: list, save_result_path: str) -> None:
    """使用本地 MineRU 库解析 PDF（需要安装 mineru）。"""
    try:
        from mineru.backend.pipeline.model_json_to_middle_json import (
            result_to_middle_json as pipeline_result_to_middle_json,
        )
        from mineru.backend.pipeline.pipeline_analyze import doc_analyze as pipeline_doc_analyze
        from mineru.backend.pipeline.pipeline_middle_json_mkcontent import union_make as pipeline_union_make
        from mineru.cli.common import convert_pdf_bytes_to_bytes_by_pypdfium2, read_fn
        from mineru.data.data_reader_writer import FileBasedDataWriter
        from mineru.utils.enum_class import MakeMode
    except ImportError:
        raise ImportError("本地 PDF 解析需要安装 mineru 库: pip install mineru")

    import copy

    file_name_list = []
    pdf_bytes_list = []
    lang_list = []

    for pdf_data_path in pdf_data_path_list:
        file_name_list.append(Path(pdf_data_path).stem)
        pdf_bytes_list.append(read_fn(pdf_data_path))
        lang_list.append("en")

    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_bytes_list[idx] = convert_pdf_bytes_to_bytes_by_pypdfium2(pdf_bytes, 0, None)

    infer_results, all_image_lists, all_pdf_docs, lang_list_out, ocr_enabled_list = pipeline_doc_analyze(
        pdf_bytes_list, lang_list, parse_method="auto", formula_enable=True, table_enable=True,
    )

    for idx, model_list in enumerate(infer_results):
        pdf_file_name = file_name_list[idx]
        local_image_dir = os.path.join(save_result_path, pdf_file_name, "images")
        local_md_dir = os.path.join(save_result_path, pdf_file_name)
        image_writer = FileBasedDataWriter(local_image_dir)
        md_writer = FileBasedDataWriter(local_md_dir)

        images_list = all_image_lists[idx]
        pdf_doc = all_pdf_docs[idx]
        _lang = lang_list_out[idx]
        _ocr_enable = ocr_enabled_list[idx]
        middle_json = pipeline_result_to_middle_json(
            model_list, images_list, pdf_doc, image_writer, _lang, _ocr_enable, True,
        )
        pdf_info = middle_json["pdf_info"]
        image_dir = str(os.path.basename(local_image_dir))
        md_content_str = pipeline_union_make(pdf_info, MakeMode.MM_MD, image_dir)
        md_writer.write_string(f"{pdf_file_name}.md", md_content_str)


def pdf_extractor(
    pdf_data_path_list: list,
    save_result_path: str,
    pdf_transform_mode: str,
    api_parsing_method: str,
    pdf_md_token: str | None = None,
) -> None:
    """PDF 解析入口：根据模式选择 API 或本地解析。"""
    if not pdf_data_path_list:
        logging.info("pdf_extractor: 无 PDF 文件需要解析，跳过")
        return
    if pdf_transform_mode == "api" and api_parsing_method == "file":
        load_extractor_pdf(pdf_data_path_list, save_result_path, pdf_md_token)
    elif pdf_transform_mode == "api" and api_parsing_method == "url":
        extractor_zip(pdf_data_path_list, save_result_path)
    else:
        pdf_extractor_local(pdf_data_path_list, save_result_path)

    for folder in os.listdir(save_result_path):
        images_path = os.path.join(save_result_path, folder, "images")
        if not os.path.exists(images_path):
            os.makedirs(images_path)

    data_dealing(save_result_path, len(os.listdir(save_result_path)) - len(pdf_data_path_list))
