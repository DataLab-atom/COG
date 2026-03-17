"""scholar — 文献去重检查（替代 Eureka database_building.scholar.semantic）

使用标题模糊匹配替代 LLM 判重，符合 input 源禁止 LLM 调用的规范。
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher


def _extract_titles_from_bib(bib_text: str) -> list[str]:
    """从 BibTeX 文本中提取所有 title 字段值。"""
    titles = re.findall(r'title\s*=\s*\{(.+?)\}', bib_text, re.IGNORECASE)
    return [t.replace("{", "").replace("}", "").strip().lower() for t in titles]


def _judge_duplicate(bib: str, all_bib: str, model_name: str = "") -> bool:
    """判断候选文献是否与已有文献重复（基于标题模糊匹配，无 LLM 调用）。"""
    candidate_titles = _extract_titles_from_bib(bib)
    existing_titles = _extract_titles_from_bib(all_bib)

    for ct in candidate_titles:
        if not ct:
            continue
        for et in existing_titles:
            if not et:
                continue
            if SequenceMatcher(None, ct, et).ratio() > 0.85:
                logging.info("fuzzy title match: '%s' ~ '%s'", ct[:60], et[:60])
                return True
    return False


def check_reference_repeat(
    titles: list,
    paper_id: str,
    key_id: str,
    bib: str,
    downloaded_paper_title_list: list,
    downloaded_paper_id_list: list,
    downloaded_paper_key_list: list,
    downloaded_paper_information: dict,
    batch_paper_title_list: list[list],
    batch_paper_paper_id_list: list,
    batch_paper_key_list: list,
    batch_bib: str,
    config: dict,
) -> bool:
    """检查候选文献是否与已下载文献重复。"""
    for downloaded_paper_title, batch_paper_title in zip(
        downloaded_paper_title_list, batch_paper_title_list
    ):
        all_paper_title = downloaded_paper_title + batch_paper_title
        if any(t in all_paper_title for t in titles[:3]):
            logging.info("duplicate paper detected: %s", titles)
            return True

    all_paper_id_list = downloaded_paper_id_list + batch_paper_paper_id_list
    all_paper_key_list = downloaded_paper_key_list + batch_paper_key_list
    if paper_id in all_paper_id_list or key_id in all_paper_key_list:
        logging.info("duplicate paper detected: %s", titles)
        return True

    all_bib = batch_bib
    for key in downloaded_paper_information:
        all_bib += f"\n\n{downloaded_paper_information[key]}"

    if _judge_duplicate(bib, all_bib):
        logging.info("duplicate paper detected: %s", titles)
        return True

    return False
