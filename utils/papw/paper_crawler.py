"""paper_crawler — 论文下载（替代 Eureka common.paper_crawler）

整合了 Download, Files, Arxiv, Unpaywall, FilterAgent 子模块。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time

from difflib import SequenceMatcher

import requests


def _get_settings():
    from config import settings
    return settings


# ── 论文缓存 ─────────────────────────────────────────────────────────────────


def _normalize_title(title: str) -> str:
    """标准化论文标题用于缓存键：小写、去除花括号和多余空白。"""
    title = title.replace("{", "").replace("}", "").replace("\n", " ")
    return re.sub(r"\s+", " ", title).strip().lower()


def _cache_key(title: str = "", doi: str = "", arxiv_id: str = "") -> str | None:
    """基于标题/DOI/arXiv ID 生成缓存键（取第一个非空值的 MD5）。"""
    for raw in [doi, arxiv_id, _normalize_title(title)]:
        if raw and raw.strip():
            return hashlib.md5(raw.strip().lower().encode()).hexdigest()
    return None


class PaperCache:
    """基于文件系统的论文 PDF 缓存。

    缓存目录结构：
        cache_dir/
            index.json          ← {cache_key: {"file": "xxx.pdf", "title": ..., "doi": ...}}
            files/              ← 实际 PDF / ZIP 文件
    """

    def __init__(self, cache_dir: str):
        self._dir = cache_dir
        self._files_dir = os.path.join(cache_dir, "files")
        self._index_path = os.path.join(cache_dir, "index.json")
        os.makedirs(self._files_dir, exist_ok=True)
        self._index: dict = self._load_index()

    def _load_index(self) -> dict:
        if os.path.exists(self._index_path):
            try:
                with open(self._index_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logging.warning("论文缓存索引损坏，将重建")
        return {}

    def _save_index(self) -> None:
        with open(self._index_path, "w", encoding="utf-8") as f:
            json.dump(self._index, f, indent=2, ensure_ascii=False)

    def lookup(self, title: str = "", doi: str = "", arxiv_id: str = "") -> str | None:
        """查找缓存，命中则返回缓存文件的绝对路径，否则返回 None。"""
        # 尝试多种 key（DOI、arXiv ID、标题）提高命中率
        for raw in [doi, arxiv_id, _normalize_title(title)]:
            if not raw or not raw.strip():
                continue
            key = hashlib.md5(raw.strip().lower().encode()).hexdigest()
            entry = self._index.get(key)
            if entry:
                cached_path = os.path.join(self._files_dir, entry["file"])
                if os.path.exists(cached_path):
                    logging.info("缓存命中: %s → %s", raw[:60], entry["file"])
                    return cached_path
                else:
                    # 文件丢失，清理所有指向该文件的孤立索引条目
                    missing_file = entry["file"]
                    stale_keys = [k for k, v in self._index.items() if v.get("file") == missing_file]
                    for k in stale_keys:
                        del self._index[k]
                    self._save_index()
        return None

    def store(self, source_path: str, title: str = "", doi: str = "", arxiv_id: str = "") -> None:
        """将已下载文件加入缓存。"""
        key = _cache_key(title, doi, arxiv_id)
        if not key:
            return
        filename = os.path.basename(source_path)
        # 避免文件名冲突，加上 key 前缀
        cache_filename = f"{key}_{filename}"
        cache_path = os.path.join(self._files_dir, cache_filename)
        if not os.path.exists(cache_path):
            shutil.copy2(source_path, cache_path)

        # 为所有可用的标识符都建立索引条目
        meta = {"file": cache_filename, "title": title, "doi": doi, "arxiv_id": arxiv_id}
        changed = False
        for raw in [doi, arxiv_id, _normalize_title(title)]:
            if raw and raw.strip():
                k = hashlib.md5(raw.strip().lower().encode()).hexdigest()
                if self._index.get(k) != meta:
                    self._index[k] = meta
                    changed = True
        if changed:
            self._save_index()


def _user_agent() -> str:
    s = _get_settings()
    return s.get("USER_AGENT", "")


def _unpaywall_email() -> str:
    s = _get_settings()
    return s.get("UNPAYWALL_EMAIL", "")


def _pdf_md_token() -> str:
    s = _get_settings()
    return s.get("PDF_MD_TOKEN", "")


# ── Files ──────────────────────────────────────────────────────────────────


def sanitize_filename(filename: str) -> str:
    filename = filename.replace("{", "").replace("}", "").replace("\n", " ")
    return re.sub(r'[\\/*?:"<>|]', "", filename)


# ── Download ───────────────────────────────────────────────────────────────

DOWNLOAD_TIMEOUT = 30


def download_pdf(url: str, filename: str, folder: str) -> bool:
    if not url:
        return False
    headers = {"User-Agent": _user_agent()}
    try:
        logging.info("downloading from %s", url)
        response = requests.get(url, headers=headers, timeout=DOWNLOAD_TIMEOUT, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "application/pdf" not in content_type:
            logging.warning("not PDF: %s (Content-Type: %s)", url, content_type)
            return False
        filepath = os.path.join(folder, sanitize_filename(f"{filename}.pdf"))
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logging.info("saved: %s", filepath)
        return True
    except Exception as exc:
        logging.warning("download failed %s: %s", url, exc)
    return False


def download_zip(url: str, filename: str, folder: str) -> bool:
    if not url:
        return False
    token = _pdf_md_token()
    base_url = "https://mineru.net/api/v4/extract/task"
    header = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    data = {"url": url, "language": "en"}

    try:
        res = requests.post(base_url, headers=header, json=data)
        if res.status_code != 200:
            logging.warning("zip upload failed: %s", url)
            return False

        task_id = res.json()["data"]["task_id"]
        poll_header = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

        while True:
            res = requests.get(f"https://mineru.net/api/v4/extract/task/{task_id}", headers=poll_header)
            state = res.json()["data"]["state"]
            if state == "done":
                filepath = os.path.join(folder, f"{filename}.zip")
                resp = requests.get(res.json()["data"]["full_zip_url"])
                with open(filepath, "wb") as f:
                    f.write(resp.content)
                logging.info("zip saved: %s", filepath)
                return True
            elif state == "failed":
                logging.warning("zip parsing failed: %s", url)
                return False
            time.sleep(15)
    except Exception as exc:
        logging.warning("download_zip failed %s: %s", url, exc)
    return False


# ── Arxiv ──────────────────────────────────────────────────────────────────


def search_arxiv_and_download(entry: dict) -> str | bool:
    """在 arXiv 上搜索论文，返回 PDF URL 或 False。"""
    try:
        import arxiv as arxiv_lib
    except ImportError:
        logging.warning("arxiv library not installed")
        return False

    arxiv_id = None
    if entry.get("archiveprefix", "").lower() == "arxiv" and entry.get("eprint"):
        arxiv_id = entry["eprint"]
    elif entry.get("eprint") and "arxiv" in entry.get("eprint", "").lower():
        match = re.search(r"arxiv[:\s]*([\d\.]+v?\d*)", entry["eprint"], re.IGNORECASE)
        if match:
            arxiv_id = match.group(1)

    title = entry.get("title", "")

    if arxiv_id:
        try:
            search = arxiv_lib.Search(id_list=[arxiv_id], max_results=1)
            paper = next(search.results(), None)
        except Exception:
            paper = None
    elif title:
        query = f'ti:"{title}"'
        year = entry.get("year", "")
        if year:
            query += f" AND submittedDate:[{year}01010000 TO {year}12312359]"
        try:
            search = arxiv_lib.Search(query=query, max_results=3)
            paper = next(search.results(), None)
            if paper and title.lower() not in paper.title.lower():
                logging.warning("arXiv title mismatch: %s vs %s", paper.title, title)
        except Exception:
            paper = None
    else:
        return False

    if paper and paper.pdf_url:
        return paper.pdf_url
    return False


# ── Unpaywall ──────────────────────────────────────────────────────────────


def get_unpaywall_url_by_doi(doi: str, email: str = "") -> str | None:
    email = email or _unpaywall_email()
    try:
        api_url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("best_oa_location") and data["best_oa_location"].get("url_for_pdf"):
            return data["best_oa_location"]["url_for_pdf"]
    except Exception as exc:
        logging.warning("Unpaywall failed: %s", exc)
    return None


def get_unpaywall_search_result_by_title(query: str, email: str = ""):
    email = email or _unpaywall_email()
    try:
        api_url = f"https://api.unpaywall.org/v2/search?query={query}&email={email}"
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logging.warning("Unpaywall search failed: %s", exc)
    return None


# ── FilterAgent ────────────────────────────────────────────────────────────


def filter_agent_run(bibtex, paper_list) -> dict:
    """从论文列表中筛选匹配 BibTeX 的论文（基于标题模糊匹配，无 LLM 调用）。"""
    # 从 BibTeX entry 中提取标题
    title = ""
    if isinstance(bibtex, dict):
        title = bibtex.get("title", "")
    else:
        m = re.search(r'title\s*=\s*\{(.+?)\}', str(bibtex), re.IGNORECASE)
        if m:
            title = m.group(1).strip()
    if not title:
        return {"status": False, "value": ""}

    title_lower = title.replace("{", "").replace("}", "").lower()

    # 从 Unpaywall 搜索结果中匹配
    results = []
    if isinstance(paper_list, dict):
        results = paper_list.get("results", [])
    elif isinstance(paper_list, list):
        results = paper_list

    best_url = ""
    best_ratio = 0.0
    for item in results:
        try:
            resp = item.get("response", item) if isinstance(item, dict) else {}
            item_title = resp.get("title", "") or ""
            oa = resp.get("best_oa_location") or {}
            pdf_url = oa.get("url_for_pdf", "") or ""
            if not item_title or not pdf_url:
                continue
            ratio = SequenceMatcher(None, title_lower, item_title.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_url = pdf_url
        except Exception:
            continue

    if best_ratio > 0.7 and best_url:
        return {"status": True, "value": best_url}
    return {"status": False, "value": ""}


# ── 主入口 ─────────────────────────────────────────────────────────────────


def paper_crawler(
    bib_file_path: str,
    download_folder: str,
    downloaded_paper_title_list: list,
    downloaded_paper_key_list: list,
    downloaded_paper_information: dict,
    downloaded_paper_information_bib: dict,
    api_parsing_method: str,
    all_paper_num: int,
    batch_paper_id: list | None = None,
    batch_downloaded_paper_list: list | None = None,
    batch_paper_title_list: list | None = None,
    unpaywall_email: str = "",
    cache_dir: str = "",
) -> None:
    """论文下载主函数。

    Args:
        cache_dir: 论文缓存目录路径。传入非空字符串时启用缓存，
                   已缓存的论文将直接从缓存复制而非重新下载。
    """
    import bibtexparser
    from bibtexparser.bibdatabase import BibDatabase
    from bibtexparser.bwriter import BibTexWriter

    if not os.path.exists(download_folder):
        os.makedirs(download_folder)

    cache = PaperCache(cache_dir) if cache_dir else None

    tmp_id = []
    tmp_paper = []

    try:
        with open(bib_file_path, "r", encoding="utf-8") as f:
            bib_database = bibtexparser.load(f)
    except FileNotFoundError:
        logging.error("BibTeX file not found: %s", bib_file_path)
        return
    except Exception as exc:
        logging.error("BibTeX parse failed: %s", exc)
        return

    logging.info("found %d entries in BibTeX", len(bib_database.entries))
    download_count = 0
    cache_hit_count = 0
    start_num = len(downloaded_paper_title_list)

    for i, entry in enumerate(bib_database.entries):
        title = entry.get("title", "")
        doi = entry.get("doi", "")
        url_bib = entry.get("url")
        arxiv_id = entry.get("eprint", "")
        file_key = entry.get("ID", sanitize_filename(title[:50] if title else "untitled"))

        pdf_found = False
        file_label = f"paper_{start_num + download_count:03}"

        # 0. 检查缓存
        if cache:
            cached_path = cache.lookup(title=title, doi=doi, arxiv_id=arxiv_id)
            if cached_path:
                ext = os.path.splitext(cached_path)[1] or ".pdf"
                dest = os.path.join(download_folder, sanitize_filename(f"{file_label}{ext}"))
                shutil.copy2(cached_path, dest)
                pdf_found = True
                cache_hit_count += 1
                logging.info("从缓存恢复: %s → %s", os.path.basename(cached_path), dest)

        # 1. 直接 PDF 链接
        if not pdf_found and url_bib and url_bib.lower().endswith(".pdf"):
            if api_parsing_method == "file" or not api_parsing_method:
                pdf_found = download_pdf(url_bib, file_label, download_folder)
            else:
                pdf_found = download_zip(url_bib, file_label, download_folder)

        # 2. arXiv
        if not pdf_found and (title or entry.get("eprint")):
            arxiv_url = search_arxiv_and_download(entry)
            if arxiv_url:
                if api_parsing_method == "file" or not api_parsing_method:
                    pdf_found = download_pdf(arxiv_url, file_label, download_folder)
                else:
                    pdf_found = download_zip(arxiv_url, file_label, download_folder)

        # 3. Unpaywall by DOI
        if not pdf_found and doi:
            unpaywall_url = get_unpaywall_url_by_doi(doi, email=unpaywall_email)
            if unpaywall_url:
                if api_parsing_method == "file" or not api_parsing_method:
                    pdf_found = download_pdf(unpaywall_url, file_label, download_folder)
                else:
                    pdf_found = download_zip(unpaywall_url, file_label, download_folder)
            time.sleep(1)

        # 4. Unpaywall by title
        if not pdf_found and title and not doi:
            data = get_unpaywall_search_result_by_title(title, email=unpaywall_email)
            if data:
                res = filter_agent_run(entry, data)
                if res.get("status"):
                    if api_parsing_method == "file" or not api_parsing_method:
                        pdf_found = download_pdf(res["value"], file_label, download_folder)
                    else:
                        pdf_found = download_zip(res["value"], file_label, download_folder)
            time.sleep(1)

        # 下载成功后写入缓存
        if pdf_found and cache:
            # 查找刚下载的文件并加入缓存
            for fname in os.listdir(download_folder):
                if fname.startswith(sanitize_filename(file_label)):
                    fpath = os.path.join(download_folder, fname)
                    cache.store(fpath, title=title, doi=doi, arxiv_id=arxiv_id)
                    break

        if pdf_found:
            if batch_paper_title_list:
                downloaded_paper_title_list.append(batch_paper_title_list[i])
            else:
                downloaded_paper_title_list.append(title)

            downloaded_paper_key_list.append(file_key)

            if batch_paper_id:
                tmp_id.append(batch_paper_id[i])
            if batch_downloaded_paper_list:
                tmp_paper.append(batch_downloaded_paper_list[i])

            db = BibDatabase()
            db.entries = [entry]
            writer = BibTexWriter()
            downloaded_paper_information[f"paper_{start_num + download_count:03}.pdf"] = writer.write(db)

            tmp_dict = {}
            for key in entry:
                if key in [
                    "ENTRYTYPE", "ID", "author", "title", "year", "volume",
                    "publisher", "pages", "number", "journal", "booktitle",
                    "organization", "isbn",
                ]:
                    tmp_dict[key] = entry[key]

            db2 = BibDatabase()
            db2.entries = [tmp_dict]
            writer2 = BibTexWriter()
            downloaded_paper_information_bib[f"paper_{start_num + download_count:03}.pdf"] = writer2.write(db2)

            download_count += 1

            if len(downloaded_paper_title_list) >= all_paper_num:
                logging.info("target paper count reached")
                break
        else:
            logging.warning("failed to download: %s (%s)", file_key, title[:50] if title else "")

        time.sleep(1)

    # 清理未下载的 batch 数据
    if batch_paper_id:
        batch_paper_id[:] = [pid for pid in batch_paper_id if pid in tmp_id]
    if batch_downloaded_paper_list:
        batch_downloaded_paper_list[:] = [p for p in batch_downloaded_paper_list if p in tmp_paper]

    if cache:
        logging.info("论文下载完成: %d 篇（缓存命中 %d 篇，新下载 %d 篇）",
                     download_count, cache_hit_count, download_count - cache_hit_count)
    else:
        logging.info("downloaded %d papers", download_count)
