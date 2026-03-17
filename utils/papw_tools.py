"""papw_tools — papw 集成同步工具函数

同步工具，供 tools/*.json 配置引用（type: "tool"）。

工具列表：
    papw_init                     — 预创建 papw 根目录
    papw_build_method_explain     — 从 U2E 精英 patches 构建代码目录结构
    papw_build_writing_requirement — 读取代码和研究信息，构建写作需求文本
    papw_normalize_outline        — 标准化 LLM 大纲并展平为叶子任务列表
    papw_get_loop_task            — 按索引取出当前任务
    papw_update_task_state        — 根据任务执行结果更新循环状态
    papw_check_improve_done       — 检查优化是否已完成
    papw_read_file                — 读取文件内容
    papw_save_file                — 保存文本到文件
"""
from __future__ import annotations

import json
import os
from typing import Any

from utils import ToolResult


class PapwInitResult(ToolResult):
    """papw_init() 的返回值。"""

    papw_root: str   # papw 根目录路径（{output_dir}/papw）


def papw_init(output_dir: str) -> PapwInitResult:
    """预创建 papw 根目录，使文献下载和向量库构建能与代码优化流水线并行启动。

    Args:
        output_dir: 项目输出根目录，papw/ 子目录将在此创建。

    Returns:
        PapwInitResult: 含 papw_root 路径。
    """
    papw_root = os.path.join(output_dir, "papw")
    os.makedirs(papw_root, exist_ok=True)
    return PapwInitResult(papw_root=papw_root)


class PapwMethodExplainResult(ToolResult):
    """papw_build_method_explain() 的返回值。"""

    method_code_path: str      # papw 代码目录路径（含 Ours.py 和 other/baseline_0/）
    research_info_path: str    # research_information.json 路径
    papw_root: str             # papw 根目录路径（{output_dir}/papw）
    main_research: str         # 主要研究描述（= requirement）
    method_name: str           # 方法名称（= metric_name）
    ours_code: str             # U2E 进化后的函数代码（多函数拼合）
    baseline_code: str         # arch 生成的原始函数代码（MCTS 起点，多函数拼合）


def papw_build_method_explain(
    tree: list,
    elitist_patches: list | None,
    requirement: str,
    metric_name: str,
    output_dir: str,
) -> PapwMethodExplainResult:
    """从 U2E 精英 patches 与架构 tree 构建 method_explain，并写入 papw 目录结构。

    old_code（baseline）来自 tree fn 节点的 code 字段，即 arch 生成的原始代码（MCTS 起点）。
    new_code（Ours）来自 u2e_elitist.patches[i].code（U2E 进化后）。

    Args:
        tree:             arch_to_project 输出的 tree（fn 节点含 code 字段）。
        elitist_patches:  U2E 全局精英个体的 patches 列表，每项含 target_name / code。
        requirement:      原始需求描述（作为 main_research）。
        metric_name:      优化指标名称（作为 method_name）。
        output_dir:       项目输出根目录。

    Returns:
        PapwMethodExplainResult: 含 method_code_path / research_info_path 等路径与代码字符串。
    """
    if elitist_patches is None:
        elitist_patches = []

    # 建立 func_name → arch 原始代码的映射（MCTS 起点）
    baseline_map: dict[str, str] = {}
    for node in tree:
        if node.get("kind") in ("function", "type"):
            fn_id = node.get("id", "")
            fname = fn_id.split("::", 1)[-1] if "::" in fn_id else fn_id
            baseline_map[fname] = node.get("code", "")

    # 拼合 Ours（U2E 进化后）与 baseline（arch 原始，MCTS 起点）代码
    ours_parts: list[str] = []
    baseline_parts: list[str] = []
    for patch in elitist_patches:
        fname = patch.get("target_name", "")
        new_code = patch.get("code", "")
        old_code = baseline_map.get(fname, "# (not found in tree)")
        ours_parts.append(f"# Function: {fname}\n{new_code}")
        baseline_parts.append(f"# Function: {fname} (original, MCTS start point)\n{old_code}")

    ours_code = "\n\n".join(ours_parts)
    baseline_code = "\n\n".join(baseline_parts)

    # 写入 papw 目录结构
    papw_root = os.path.join(output_dir, "papw")
    code_dir = os.path.join(papw_root, "code")
    baseline_dir = os.path.join(code_dir, "other", "baseline_0")
    os.makedirs(code_dir, exist_ok=True)
    os.makedirs(baseline_dir, exist_ok=True)

    with open(os.path.join(code_dir, "Ours.py"), "w", encoding="utf-8") as f:
        f.write(ours_code)

    with open(os.path.join(baseline_dir, "baseline.py"), "w", encoding="utf-8") as f:
        f.write(baseline_code)

    # 写入 research_information.json（papw 各章节写作阶段共用）
    info_path = os.path.join(papw_root, "research_information.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump({"method_name": metric_name, "main_research": requirement}, f, ensure_ascii=False, indent=4)

    return PapwMethodExplainResult(
        method_code_path=code_dir,
        research_info_path=info_path,
        papw_root=papw_root,
        main_research=requirement,
        method_name=metric_name,
        ours_code=ours_code,
        baseline_code=baseline_code,
    )


# ── 方法论写作工具 ───────────────────────────────────────────────────────────


class PapwMethodPathsResult(ToolResult):
    our_method_path: str
    research_information_path: str
    result_write_path: str
    chapters_dir: str


def papw_method_writing_paths(papw_root: str) -> PapwMethodPathsResult:
    """从 papw_root 派生方法论写作所需的所有路径，并创建必要的目录。"""
    chapters_dir = os.path.join(papw_root, "chapters", "method_writing")
    os.makedirs(chapters_dir, exist_ok=True)
    return PapwMethodPathsResult(
        our_method_path=os.path.join(papw_root, "code", "Ours.py"),
        research_information_path=os.path.join(papw_root, "research_information.json"),
        result_write_path=os.path.join(chapters_dir, "method.tex"),
        chapters_dir=chapters_dir,
    )


class PapwWritingRequirementResult(ToolResult):
    writing_requirement: str


def papw_build_writing_requirement(
    our_method_path: str,
    research_information_path: str,
) -> PapwWritingRequirementResult:
    """读取代码文件和研究信息 JSON，拼合为写作需求文本。"""
    parts: list[str] = []
    if os.path.exists(our_method_path):
        with open(our_method_path, encoding="utf-8") as f:
            parts.append(f"Method Code:\n{f.read()}")
    if os.path.exists(research_information_path):
        with open(research_information_path, encoding="utf-8") as f:
            info = json.load(f)
            parts.append(f"Research Information:\n{json.dumps(info, ensure_ascii=False, indent=2)}")
    return PapwWritingRequirementResult(writing_requirement="\n\n".join(parts))


class PapwNormalizeOutlineResult(ToolResult):
    leaf_tasks: list       # 展平后的叶子任务列表 [{goal, task_type, length?}, ...]
    task_plan_text: str    # 完整大纲文本（用于注入 agent 提示词）
    task_count: int        # 叶子任务总数


def papw_normalize_outline(outline: Any) -> PapwNormalizeOutlineResult:
    """标准化 LLM 返回的大纲，并展平为有序的叶子任务列表。"""

    def _normalize(raw: Any) -> dict:
        if isinstance(raw, dict) and "id" in raw:
            return raw
        if isinstance(raw, dict) and "outline" in raw:
            return _normalize(raw["outline"])
        if isinstance(raw, list):
            return {
                "id": "root", "task_type": "write",
                "goal": "Write the Methodology section based on the provided requirements.",
                "length": "800 words", "sub_tasks": raw,
            }
        return {
            "id": "root", "task_type": "write",
            "goal": "Write the Methodology section based on the provided requirements.",
            "length": "800 words", "sub_tasks": [],
        }

    task_root = _normalize(outline)

    # 展平为叶子任务列表（深度优先，只保留没有 sub_tasks 的叶子节点）
    leaf_tasks: list[dict] = []
    plan_lines: list[str] = []

    def _walk(task: dict, depth: int = 0) -> None:
        indent = "  " * depth
        length_part = f", length: {task.get('length')}" if task.get("length") else ""
        plan_lines.append(
            f"{indent}**{task.get('id', '?')}**: {task.get('goal', '')} "
            f"(task_type: {task.get('task_type', 'write')}{length_part})"
        )
        subs = task.get("sub_tasks") or []
        if subs:
            for sub in subs:
                _walk(sub, depth + 1)
        else:
            leaf_tasks.append({
                "goal": task.get("goal", ""),
                "task_type": task.get("task_type", "write"),
                "length": task.get("length", ""),
            })

    _walk(task_root)

    return PapwNormalizeOutlineResult(
        leaf_tasks=leaf_tasks,
        task_plan_text="\n".join(plan_lines),
        task_count=len(leaf_tasks),
    )


class PapwGetLoopTaskResult(ToolResult):
    task_goal: str
    task_type: str
    need_cite: bool
    total_tasks: int
    has_more_tasks: bool


def papw_get_loop_task(
    tasks: list,
    index: int,
) -> PapwGetLoopTaskResult:
    """从任务列表中按索引取出当前任务。"""
    total = len(tasks)
    if index >= total:
        return PapwGetLoopTaskResult(
            task_goal="", task_type="write", need_cite=False,
            total_tasks=total, has_more_tasks=False,
        )
    task = tasks[index]
    return PapwGetLoopTaskResult(
        task_goal=task.get("goal", ""),
        task_type=task.get("task_type", "write"),
        need_cite=task.get("need_cite", False),
        total_tasks=total,
        has_more_tasks=(index + 1 < total),
    )


class PapwUpdateTaskStateResult(ToolResult):
    done_write: str
    related_think: str
    next_index: int
    has_more_tasks: bool


def papw_update_task_state(
    task_type: str,
    dispatch_result: dict,
    current_done_write: str,
    current_related_think: str,
    task_index: int,
    total_tasks: int,
) -> PapwUpdateTaskStateResult:
    """根据任务执行结果更新循环状态。

    think 任务的结果追加到 related_think；
    write 任务的结果追加到 done_write。
    """
    done_write = current_done_write
    related_think = current_related_think

    if task_type == "think":
        content = dispatch_result.get("think", "")
        related_think = related_think + content + "\n\n\n"
    elif task_type == "write":
        content = dispatch_result.get("write", "")
        done_write = done_write + content + "\n\n\n"

    next_idx = task_index + 1
    return PapwUpdateTaskStateResult(
        done_write=done_write,
        related_think=related_think,
        next_index=next_idx,
        has_more_tasks=(next_idx < total_tasks),
    )


class PapwCheckImproveDoneResult(ToolResult):
    content: str
    should_continue: bool


def papw_check_improve_done(
    improve_result: str,
    previous_content: str,
) -> PapwCheckImproveDoneResult:
    """检查优化结果：若包含 'already completed' 则停止，否则继续。"""
    if "already completed" in improve_result:
        return PapwCheckImproveDoneResult(
            content=previous_content,
            should_continue=False,
        )
    return PapwCheckImproveDoneResult(
        content=improve_result,
        should_continue=True,
    )


class PapwReadFileResult(ToolResult):
    content: str


def papw_read_file(file_path: str) -> PapwReadFileResult:
    """读取文件内容，文件不存在则返回空字符串。"""
    if os.path.exists(file_path):
        with open(file_path, encoding="utf-8") as f:
            return PapwReadFileResult(content=f.read())
    return PapwReadFileResult(content="")


class PapwSaveFileResult(ToolResult):
    file_path: str
    success: bool


def papw_save_file(file_path: str, content: str) -> PapwSaveFileResult:
    """保存文本到文件。"""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    return PapwSaveFileResult(file_path=file_path, success=True)


# ── 实验章节写作工具 ───────────────────────────────────────────────────────────


class PapwExperimentPathsResult(ToolResult):
    our_method_path: str
    research_information_path: str
    result_write_path: str
    chapters_dir: str


def papw_experiment_writing_paths(papw_root: str) -> PapwExperimentPathsResult:
    """从 papw_root 派生实验章节写作所需的所有路径，并创建必要的目录。"""
    chapters_dir = os.path.join(papw_root, "chapters", "experiment_writing")
    os.makedirs(chapters_dir, exist_ok=True)
    return PapwExperimentPathsResult(
        our_method_path=os.path.join(papw_root, "code", "Ours.py"),
        research_information_path=os.path.join(papw_root, "research_information.json"),
        result_write_path=os.path.join(chapters_dir, "experiment.tex"),
        chapters_dir=chapters_dir,
    )


class PapwExperimentBuildReqResult(ToolResult):
    writing_requirement: str


def papw_experiment_build_req(
    our_method_path: str,
    research_information_path: str,
    result_perspective_path: str,
) -> PapwExperimentBuildReqResult:
    """读取代码、研究信息和实验结果，构建实验章节写作需求文本。

    收集 result_perspective_path 下各透视维度的实验代码、绘图代码和图表路径，
    拼合为完整的写作需求文本。
    """
    parts: list[str] = []

    # 方法代码
    if os.path.exists(our_method_path):
        with open(our_method_path, encoding="utf-8") as f:
            parts.append(f"Method Code:\n{f.read()}")

    # 研究信息
    if os.path.exists(research_information_path):
        with open(research_information_path, encoding="utf-8") as f:
            info = json.load(f)
            parts.append(f"Research Information:\n{json.dumps(info, ensure_ascii=False, indent=2)}")

    # 实验结果信息
    experiment_info = ""
    if result_perspective_path and os.path.exists(result_perspective_path):
        for perspective_name in sorted(os.listdir(result_perspective_path)):
            perspective_dir = os.path.join(result_perspective_path, perspective_name)
            if not os.path.isdir(perspective_dir):
                continue

            experiment_info += f"\n## Perspective: {perspective_name}\n"

            # 实验代码
            code_path = os.path.join(perspective_dir, "experimental_code.py")
            if os.path.exists(code_path):
                with open(code_path, encoding="utf-8") as f:
                    experiment_info += f"### Code:\n```python\n{f.read()}\n```\n"

            # 绘图代码
            draw_code_path = os.path.join(perspective_dir, "draw_code.py")
            if os.path.exists(draw_code_path):
                with open(draw_code_path, encoding="utf-8") as f:
                    draw_code = f.read()
                    experiment_info += f"### Draw Code:\n```python\n{draw_code}\n```\n"

            # 图表路径
            charts_dir = os.path.join(perspective_dir, "charts")
            if os.path.exists(charts_dir):
                for chart_name in sorted(os.listdir(charts_dir)):
                    chart_path = os.path.join(charts_dir, chart_name)
                    if chart_path.lower().endswith((".png", ".jpg", ".jpeg")):
                        experiment_info += f"### Chart: {chart_path}\n"

    if experiment_info:
        parts.append(f"Experimental Results:\n{experiment_info}")

    return PapwExperimentBuildReqResult(writing_requirement="\n\n".join(parts))


# ── 合并写作工具 ────────────────────────────────────────────────────────────


class PapwMergePathsResult(ToolResult):
    result_write_path: str
    chapters_dir: str


def papw_merge_writing_paths(papw_root: str) -> PapwMergePathsResult:
    """从 papw_root 派生合并写作所需的路径。"""
    chapters_dir = os.path.join(papw_root, "chapters", "merge_writing")
    os.makedirs(chapters_dir, exist_ok=True)
    return PapwMergePathsResult(
        result_write_path=os.path.join(chapters_dir, "paper.tex"),
        chapters_dir=chapters_dir,
    )


class PapwCollectChaptersResult(ToolResult):
    introduction: str
    related_work: str
    methodology: str
    experiments: str
    bibliography: str


def papw_collect_chapters(
    method_tex_path: str,
    related_work_tex_path: str,
    experiment_tex_path: str,
    introduction_tex_path: str,
    related_work_bib_path: str,
    introduction_bib_path: str,
) -> PapwCollectChaptersResult:
    """读取各章节 LaTeX 和 bib 文件内容。"""
    def _read(path: str) -> str:
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
        return ""

    # 合并 bib
    bib_parts: list[str] = []
    for bib_path in [related_work_bib_path, introduction_bib_path]:
        content = _read(bib_path)
        if content:
            bib_parts.append(content)

    return PapwCollectChaptersResult(
        introduction=_read(introduction_tex_path),
        related_work=_read(related_work_tex_path),
        methodology=_read(method_tex_path),
        experiments=_read(experiment_tex_path),
        bibliography="\n\n".join(bib_parts),
    )


class PapwMergeCheckResult(ToolResult):
    content: str
    should_continue: bool


def papw_merge_check_improve(
    improve_result: str,
    previous_content: str,
) -> PapwMergeCheckResult:
    """检查合并改进是否应继续：若内容无变化或为空则停止。"""
    if not improve_result or improve_result == previous_content:
        return PapwMergeCheckResult(content=previous_content, should_continue=False)
    return PapwMergeCheckResult(content=improve_result, should_continue=True)


class PapwFormatChaptersResult(ToolResult):
    chapters_text: str


def papw_format_chapters(
    introduction: str,
    related_work: str,
    methodology: str,
    experiments: str,
) -> PapwFormatChaptersResult:
    """将各章节内容格式化为单一文本，供结论/摘要/标题 agent 使用。"""
    parts: list[str] = []
    for name, content in [
        ("Introduction", introduction),
        ("Related Work", related_work),
        ("Methodology", methodology),
        ("Experiments", experiments),
    ]:
        if content:
            parts.append(f"## {name}\n{content}")
    return PapwFormatChaptersResult(chapters_text="\n\n".join(parts))


class PapwBuildChaptersTextResult(ToolResult):
    chapters_content: str
    chapters_and_conclusion: str
    abstract_and_chapters: str
    merge_content: str


def papw_build_merge_texts(
    introduction: str,
    related_work: str,
    methodology: str,
    experiments: str,
    bibliography: str,
    conclusion: str,
    abstract: str,
    title: str,
    tex_template: str,
) -> PapwBuildChaptersTextResult:
    """构建合并写作各阶段所需的文本拼接。"""
    chapters_content = ""
    for name, content in [
        ("Introduction", introduction),
        ("Related Work", related_work),
        ("Methodology", methodology),
        ("Experiments", experiments),
    ]:
        if content:
            chapters_content += f"## {name}\n{content}\n\n"

    chapters_and_conclusion = chapters_content
    if conclusion:
        chapters_and_conclusion += f"## Conclusion\n{conclusion}\n\n"

    abstract_and_chapters = f"## Abstract\n{abstract}\n\n"
    for name, content in [("Introduction", introduction), ("Methodology", methodology)]:
        if content:
            abstract_and_chapters += f"## {name}\n{content}\n\n"

    merge_content = (
        f"# LaTeX Template:\n```latex\n{tex_template}\n```\n\n"
        f"# Title:\n{title}\n\n"
        f"# Abstract:\n{abstract}\n\n"
        f"# Introduction:\n{introduction}\n\n"
        f"# Related Work:\n{related_work}\n\n"
        f"# Methodology:\n{methodology}\n\n"
        f"# Experiments:\n{experiments}\n\n"
        f"# Conclusion:\n{conclusion}\n\n"
        f"# Bibliography entries:\n{bibliography}"
    )

    return PapwBuildChaptersTextResult(
        chapters_content=chapters_content,
        chapters_and_conclusion=chapters_and_conclusion,
        abstract_and_chapters=abstract_and_chapters,
        merge_content=merge_content,
    )


class PapwMergeCiteCheckResult(ToolResult):
    content: str
    should_continue: bool


def papw_merge_check_cite_done(
    verified: bool,
    corrected_tex: str,
    previous_content: str,
) -> PapwMergeCiteCheckResult:
    """检查引用检查是否通过：若 verified=true 则停止。"""
    content = corrected_tex if corrected_tex else previous_content
    return PapwMergeCiteCheckResult(
        content=content,
        should_continue=not verified,
    )


# ── 相关工作 / 引言共用的 RAG 写作工具 ────────────────────────────────────────


class PapwRwPathsResult(ToolResult):
    our_method_path: str
    research_information_path: str
    result_write_path: str
    bib_result_path: str
    bib_result_list_path: str
    chapters_dir: str


def papw_rw_writing_paths(papw_root: str) -> PapwRwPathsResult:
    """从 papw_root 派生相关工作写作所需路径。"""
    chapters_dir = os.path.join(papw_root, "chapters", "related_work_writing")
    os.makedirs(chapters_dir, exist_ok=True)
    return PapwRwPathsResult(
        our_method_path=os.path.join(papw_root, "code", "Ours.py"),
        research_information_path=os.path.join(papw_root, "research_information.json"),
        result_write_path=os.path.join(chapters_dir, "related_work.tex"),
        bib_result_path=os.path.join(chapters_dir, "reference_bib.bib"),
        bib_result_list_path=os.path.join(chapters_dir, "related_work_reference_bib.json"),
        chapters_dir=chapters_dir,
    )


class PapwSelectCiteResult(ToolResult):
    formatted_text_refs: list       # 文本引用列表（dict 格式）
    formatted_image_refs: list      # 图片引用列表（dict 格式）
    selected_bib_entries: list      # 选中的 bib 字符串列表
    bib_content: str                # 拼接后的 bib 内容


def papw_select_citations(
    retrieval_results: list,
    used_bib_set: list,
    max_cite_num: int = 3,
    max_data_per_cite: int = 3,
) -> PapwSelectCiteResult:
    """从检索结果中选择未使用的引用并格式化。

    Args:
        retrieval_results: VDB 检索 + 重排的结果列表（来自 papw_vdb_search_rerank）。
        used_bib_set: 已使用的 bib 条目列表（用于去重）。
        max_cite_num: 每句最多引用数。
        max_data_per_cite: 每个引用最多数据条数。
    """
    used_set = set(used_bib_set)
    current_refs: dict[str, list] = {}

    for item in retrieval_results:
        if len(current_refs) >= max_cite_num:
            break

        bib_tex = item.get("citation", "")
        if not bib_tex or bib_tex in used_set:
            continue

        if bib_tex not in current_refs:
            current_refs[bib_tex] = [item]
        elif len(current_refs[bib_tex]) < max_data_per_cite:
            current_refs[bib_tex].append(item)

    # 格式化引用
    text_refs: list[dict] = []
    image_refs: list[dict] = []
    for bib, items in current_refs.items():
        for item in items:
            meta = item.get("metadata", {})
            if meta.get("image_base64"):
                image_refs.append({
                    "Bib citation": bib,
                    "reference content": {
                        "image_base64": meta["image_base64"],
                        "image_path": meta.get("image_path", ""),
                    },
                })
            else:
                text_refs.append({
                    "Bib citation": bib,
                    "reference content": item.get("content", ""),
                })

    selected_bibs = list(current_refs.keys())
    bib_content = "\n\n".join(selected_bibs)

    return PapwSelectCiteResult(
        formatted_text_refs=text_refs,
        formatted_image_refs=image_refs,
        selected_bib_entries=selected_bibs,
        bib_content=bib_content,
    )


class PapwRwUpdateStateResult(ToolResult):
    done_write: str
    all_bib_content: str
    used_bib_set: list
    field_index: int
    sentence_index: int
    has_more_tasks: bool


def papw_rw_update_write_state(
    tex_code: str,
    selected_bib_entries: list,
    current_done_write: str,
    current_all_bib: str,
    current_used_bib_set: list,
    field_index: int,
    sentence_index: int,
    sentence_num: int,
    total_fields: int,
) -> PapwRwUpdateStateResult:
    """更新相关工作写作循环状态。"""
    done_write = current_done_write + tex_code + "\n"
    all_bib = current_all_bib + "\n" + "\n\n".join(selected_bib_entries)
    used_bib = list(set(current_used_bib_set + selected_bib_entries))

    next_sentence = sentence_index + 1
    next_field = field_index
    if next_sentence >= sentence_num:
        next_sentence = 0
        next_field = field_index + 1

    has_more = next_field < total_fields

    return PapwRwUpdateStateResult(
        done_write=done_write,
        all_bib_content=all_bib,
        used_bib_set=used_bib,
        field_index=next_field,
        sentence_index=next_sentence,
        has_more_tasks=has_more,
    )


class PapwRwBuildTaskResult(ToolResult):
    current_field: str
    current_sentence_index: int
    total_fields: int
    has_more_tasks: bool


def papw_rw_get_current_task(
    fields: list,
    field_index: int,
    sentence_index: int,
    sentence_num: int,
) -> PapwRwBuildTaskResult:
    """获取当前要处理的领域和句子索引。"""
    total = len(fields)
    if field_index >= total:
        return PapwRwBuildTaskResult(
            current_field="",
            current_sentence_index=0,
            total_fields=total,
            has_more_tasks=False,
        )
    return PapwRwBuildTaskResult(
        current_field=fields[field_index],
        current_sentence_index=sentence_index,
        total_fields=total,
        has_more_tasks=True,
    )


class PapwRwCleanBibResult(ToolResult):
    cleaned_bib: str
    bib_list: list


def papw_rw_clean_bib_by_content(
    all_bib_content: str,
    tex_content: str,
) -> PapwRwCleanBibResult:
    """根据 tex 内容清理未引用的 bib 条目。"""
    try:
        import bibtexparser
        parser = bibtexparser.bparser.BibTexParser()
        bib_db = bibtexparser.loads(all_bib_content, parser=parser)

        used_entries = []
        for entry in bib_db.entries:
            bib_id = entry.get("ID", "")
            if bib_id and bib_id in tex_content:
                used_entries.append(entry)

        # 重建 bib 字符串
        clean_db = bibtexparser.bibdatabase.BibDatabase()
        clean_db.entries = used_entries
        writer = bibtexparser.bwriter.BibTexWriter()
        cleaned = writer.write(clean_db)

        bib_list = [e.get("ID", "") for e in used_entries]
        return PapwRwCleanBibResult(cleaned_bib=cleaned, bib_list=bib_list)
    except Exception:
        return PapwRwCleanBibResult(cleaned_bib=all_bib_content, bib_list=[])


# ── 引言章节写作工具 ───────────────────────────────────────────────────────────


class PapwIntroPathsResult(ToolResult):
    our_method_path: str
    research_information_path: str
    result_write_path: str
    bib_result_path: str
    bib_result_list_path: str
    chapters_dir: str


def papw_intro_writing_paths(papw_root: str) -> PapwIntroPathsResult:
    """从 papw_root 派生引言写作所需路径。"""
    chapters_dir = os.path.join(papw_root, "chapters", "introduction_writing")
    os.makedirs(chapters_dir, exist_ok=True)
    return PapwIntroPathsResult(
        our_method_path=os.path.join(papw_root, "code", "Ours.py"),
        research_information_path=os.path.join(papw_root, "research_information.json"),
        result_write_path=os.path.join(chapters_dir, "introduction.tex"),
        bib_result_path=os.path.join(chapters_dir, "reference_bib.bib"),
        bib_result_list_path=os.path.join(chapters_dir, "reference_bib_list.json"),
        chapters_dir=chapters_dir,
    )


class PapwIntroBuildReqResult(ToolResult):
    writing_requirement: str


def papw_intro_build_req(
    our_method_path: str,
    research_information_path: str,
    papw_root: str,
) -> PapwIntroBuildReqResult:
    """构建引言写作需求：包含方法代码、研究信息和其他已完成章节。"""
    parts: list[str] = []

    if os.path.exists(our_method_path):
        with open(our_method_path, encoding="utf-8") as f:
            parts.append(f"Method Code:\n{f.read()}")

    if os.path.exists(research_information_path):
        with open(research_information_path, encoding="utf-8") as f:
            info = json.load(f)
            parts.append(f"Research Information:\n{json.dumps(info, ensure_ascii=False, indent=2)}")

    # 读取其他已完成章节内容
    done_content = ""
    chapters_base = os.path.join(papw_root, "chapters")
    for chapter_subpath in [
        "related_work_writing/related_work.tex",
        "method_writing/method.tex",
        "experiment_writing/experiment.tex",
    ]:
        chapter_path = os.path.join(chapters_base, chapter_subpath)
        if os.path.exists(chapter_path):
            with open(chapter_path, encoding="utf-8") as f:
                done_content += f.read() + "\n\n"

    if done_content:
        parts.append(f"The content of other chapters in the paper:\n{done_content}")

    return PapwIntroBuildReqResult(writing_requirement="\n\n".join(parts))


class PapwIntroNormalizeResult(ToolResult):
    leaf_tasks: list
    task_plan_text: str
    task_count: int


def papw_intro_normalize_outline(outline: Any) -> PapwIntroNormalizeResult:
    """标准化引言大纲并展平为叶子任务。保留 need_cite 属性。"""

    def _normalize(raw: Any) -> dict:
        if isinstance(raw, dict) and "id" in raw:
            return raw
        if isinstance(raw, dict) and "outline" in raw:
            return _normalize(raw["outline"])
        if isinstance(raw, list):
            return {
                "id": "root", "task_type": "write",
                "goal": "Write the Introduction section.",
                "length": "450 words", "sub_tasks": raw,
            }
        return {
            "id": "root", "task_type": "write",
            "goal": "Write the Introduction section.",
            "length": "450 words", "sub_tasks": [],
        }

    task_root = _normalize(outline)

    leaf_tasks: list[dict] = []
    plan_lines: list[str] = []

    def _walk(task: dict, depth: int = 0) -> None:
        indent = "  " * depth
        length_part = f", length: {task.get('length')}" if task.get("length") else ""
        cite_part = f", need_cite: {task.get('need_cite', False)}"
        plan_lines.append(
            f"{indent}**{task.get('id', '?')}**: {task.get('goal', '')} "
            f"(task_type: {task.get('task_type', 'write')}{length_part}{cite_part})"
        )
        subs = task.get("sub_tasks") or []
        if subs:
            for sub in subs:
                _walk(sub, depth + 1)
        else:
            leaf_tasks.append({
                "goal": task.get("goal", ""),
                "task_type": task.get("task_type", "write"),
                "length": task.get("length", ""),
                "need_cite": task.get("need_cite", False),
            })

    _walk(task_root)

    return PapwIntroNormalizeResult(
        leaf_tasks=leaf_tasks,
        task_plan_text="\n".join(plan_lines),
        task_count=len(leaf_tasks),
    )


# ────────────── 引言写作状态更新 ──────────────


class PapwIntroUpdateStateResult(ToolResult):
    done_write: str
    all_bib_content: str
    used_bib_set: list
    next_index: int
    has_more_tasks: bool


def papw_intro_update_state(
    dispatch_result: dict,
    current_done_write: str,
    current_all_bib: str,
    current_used_bib_set: list,
    task_index: int,
    total_tasks: int,
) -> PapwIntroUpdateStateResult:
    """根据引言子任务（cite/no-cite 分支）执行结果更新循环状态。"""
    tex_code = ""
    all_bib = current_all_bib
    used_bib_set = list(current_used_bib_set)

    if isinstance(dispatch_result, dict):
        # 两种分支均输出 tex_code
        tex_code = dispatch_result.get("tex_code", "")
        # cite 分支额外输出 selected_bib_entries / has_bib
        bib_entries = dispatch_result.get("selected_bib_entries", [])
        if bib_entries:
            for entry in bib_entries:
                bib_text = entry if isinstance(entry, str) else entry.get("bib_entry", "")
                cite_key = entry if isinstance(entry, str) else entry.get("cite_key", "")
                if bib_text and cite_key and cite_key not in used_bib_set:
                    all_bib = all_bib + bib_text + "\n\n"
                    used_bib_set.append(cite_key)

    done_write = current_done_write + tex_code + "\n\n\n" if tex_code else current_done_write
    next_idx = task_index + 1

    return PapwIntroUpdateStateResult(
        done_write=done_write,
        all_bib_content=all_bib,
        used_bib_set=used_bib_set,
        next_index=next_idx,
        has_more_tasks=(next_idx < total_tasks),
    )


# ────────────── experimenting 工具函数 ──────────────


class PapwExpPathsResult(ToolResult):
    method_code_path: str
    md_dir: str
    research_info_path: str
    perspective_base: str
    env_dir: str
    exp_get_dir: str


def papw_exp_paths(papw_root: str) -> PapwExpPathsResult:
    """从 papw_root 派生 experimenting 所需路径并创建目录。"""
    exp_get_dir = os.path.join(papw_root, "experimenting", "get")
    perspective_base = os.path.join(papw_root, "experimenting", "doing", "perspective")
    env_dir = os.path.join(papw_root, "experimental_environment")
    for d in [exp_get_dir, perspective_base, env_dir,
              os.path.join(papw_root, "log", "experimenting")]:
        os.makedirs(d, exist_ok=True)
    return PapwExpPathsResult(
        method_code_path=os.path.join(papw_root, "code", "Ours.py"),
        md_dir=os.path.join(papw_root, "database", "md"),
        research_info_path=os.path.join(papw_root, "research_information.json"),
        perspective_base=perspective_base,
        env_dir=env_dir,
        exp_get_dir=exp_get_dir,
    )


class PapwExpListPapersResult(ToolResult):
    papers: list
    paper_count: int


def papw_exp_list_papers(md_dir: str) -> PapwExpListPapersResult:
    """列出 md 目录中所有论文文件夹，返回 [{name, md_path}] 列表。"""
    papers = []
    if os.path.exists(md_dir):
        for folder_name in sorted(os.listdir(md_dir)):
            folder_path = os.path.join(md_dir, folder_name)
            if not os.path.isdir(folder_path):
                continue
            md_path = os.path.join(folder_path, f"{folder_name}.md")
            if os.path.exists(md_path):
                # 截断内容避免超出上下文
                with open(md_path, "r", encoding="utf-8") as f:
                    content = f.read()
                papers.append({
                    "name": folder_name,
                    "content": content[:8000] if len(content) > 8000 else content,
                })
    return PapwExpListPapersResult(papers=papers, paper_count=len(papers))


class PapwExpCollectAbstractsResult(ToolResult):
    abstracts_text: str
    abstracts_count: int


def papw_exp_collect_abstracts(
    abstract_results: list,
    paper_names: list = None,
) -> PapwExpCollectAbstractsResult:
    """将 map 步骤输出的摘要列表合并为文本。

    Args:
        abstract_results: 摘要字符串列表（map collect="abstract" 的输出）。
        paper_names: 论文信息列表（含 name 字段），用于关联摘要与论文名。
    """
    parts = []
    names = paper_names or []
    for i, abstract in enumerate(abstract_results):
        name = names[i].get("name", f"paper_{i}") if i < len(names) and isinstance(names[i], dict) else f"paper_{i}"
        text = abstract if isinstance(abstract, str) else str(abstract)
        parts.append(f"Paper: {name}\nAbstract: {text}")
    return PapwExpCollectAbstractsResult(
        abstracts_text="\n\n".join(parts),
        abstracts_count=len(parts),
    )


class PapwExpSaveResearchInfoResult(ToolResult):
    research_info_path: str


def papw_exp_save_research_info(
    research_info_path: str,
    method_name: str,
    main_research: str,
    method_statement: str,
    abstracts_count: int,
) -> PapwExpSaveResearchInfoResult:
    """保存研究信息到 JSON 文件。"""
    info = {
        "method_name": method_name,
        "main_research": main_research,
        "method_statement": method_statement,
        "abstracts_count": abstracts_count,
    }
    os.makedirs(os.path.dirname(research_info_path), exist_ok=True)
    with open(research_info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    return PapwExpSaveResearchInfoResult(research_info_path=research_info_path)


class PapwExpSavePerspectivesResult(ToolResult):
    perspective_names: list
    perspective_count: int


def papw_exp_save_perspectives(
    perspective_base: str,
    perspectives: list,
) -> PapwExpSavePerspectivesResult:
    """为每个分析角度创建目录并保存信息。"""
    import re
    names = []
    for i, ap in enumerate(perspectives):
        p_name = ap.get("analysis_perspective", f"perspective_{i+1}")
        # 清理目录名
        sanitized = re.sub(r'[^\w\-.]', '_', p_name.strip())
        sanitized = re.sub(r'_+', '_', sanitized).strip('_')
        p_name = sanitized[:100] if sanitized else "perspective"
        p_dir = os.path.join(perspective_base, p_name)
        for sub in ["", "code", "results", "charts"]:
            os.makedirs(os.path.join(p_dir, sub) if sub else p_dir, exist_ok=True)
        with open(os.path.join(p_dir, "perspective_info.json"), "w", encoding="utf-8") as f:
            json.dump(ap, f, ensure_ascii=False, indent=2)
        names.append(p_name)
    return PapwExpSavePerspectivesResult(
        perspective_names=names,
        perspective_count=len(names),
    )


class PapwExpReadEnvInfoResult(ToolResult):
    env_info: str


def papw_exp_read_env_info(env_dir: str) -> PapwExpReadEnvInfoResult:
    """读取实验环境信息。"""
    parts = []
    if os.path.exists(env_dir):
        for fname in sorted(os.listdir(env_dir)):
            fpath = os.path.join(env_dir, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        parts.append(f"--- {fname} ---\n{f.read()}")
                except Exception:
                    pass
    return PapwExpReadEnvInfoResult(env_info="\n\n".join(parts))


class PapwExpPrepPerspectiveResult(ToolResult):
    perspective_info: str
    code_dir: str
    results_dir: str
    charts_dir: str
    chart_save_path: str
    p_name: str


def papw_exp_prep_perspective(
    perspective_base: str,
    p_name: str,
) -> PapwExpPrepPerspectiveResult:
    """准备单个角度的目录和信息。"""
    p_dir = os.path.join(perspective_base, p_name)
    info_path = os.path.join(p_dir, "perspective_info.json")
    info_text = ""
    if os.path.exists(info_path):
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        info_text = (
            f"Analysis perspective: {info.get('analysis_perspective', '')}\n"
            f"Core focus: {info.get('core_focus', '')}\n"
            f"Differentiated feature: {info.get('differentiated_feature', '')}\n"
            f"Expected insight: {info.get('expected_insight', '')}\n"
            f"Experimental metric: {info.get('experimental_metric', '')}"
        )
    code_dir = os.path.join(p_dir, "code")
    results_dir = os.path.join(p_dir, "results")
    charts_dir = os.path.join(p_dir, "charts")
    for d in [code_dir, results_dir, charts_dir]:
        os.makedirs(d, exist_ok=True)
    return PapwExpPrepPerspectiveResult(
        perspective_info=info_text,
        code_dir=code_dir,
        results_dir=results_dir,
        charts_dir=charts_dir,
        chart_save_path=os.path.join(charts_dir, "chart.png"),
        p_name=p_name,
    )


class PapwExpSaveAnalysisResult(ToolResult):
    analysis_path: str


def papw_exp_save_analysis(
    results_dir: str,
    experiment_analysis: str,
) -> PapwExpSaveAnalysisResult:
    """保存实验分析到文件。"""
    path = os.path.join(results_dir, "experiment_analysis.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(experiment_analysis)
    return PapwExpSaveAnalysisResult(analysis_path=path)


class PapwExpWriteDataExplainResult(ToolResult):
    explain_path: str


def papw_exp_write_data_explain(
    papw_root: str,
    perspective_base: str,
    perspective_names: list,
) -> PapwExpWriteDataExplainResult:
    """汇总所有角度的实验结果，写入实验数据说明文件。"""
    explain_path = os.path.join(papw_root, "experimental_data_explain.md")
    sections = []
    for p_name in perspective_names:
        p_dir = os.path.join(perspective_base, p_name)
        info = {}
        info_path = os.path.join(p_dir, "perspective_info.json")
        if os.path.exists(info_path):
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
        analysis = ""
        analysis_path = os.path.join(p_dir, "results", "experiment_analysis.md")
        if os.path.exists(analysis_path):
            with open(analysis_path, "r", encoding="utf-8") as f:
                analysis = f.read()
        exec_result = {}
        exec_path = os.path.join(p_dir, "results", "execution_result.json")
        if os.path.exists(exec_path):
            with open(exec_path, "r", encoding="utf-8") as f:
                exec_result = json.load(f)
        section = f"## {info.get('analysis_perspective', p_name)}\n\n"
        section += f"**Core Focus**: {info.get('core_focus', 'N/A')}\n\n"
        section += f"**Metric**: {info.get('experimental_metric', 'N/A')}\n\n"
        section += f"**Status**: {'Success' if exec_result.get('success') else 'Failed'}\n\n"
        if analysis:
            section += f"### Analysis\n\n{analysis}\n\n"
        chart_path = os.path.join(p_dir, "charts", "chart.png")
        if os.path.exists(chart_path):
            section += f"![Chart]({chart_path})\n\n"
        sections.append(section)
    full_text = "# Experimental Data Explanation\n\n" + "\n---\n\n".join(sections)
    with open(explain_path, "w", encoding="utf-8") as f:
        f.write(full_text)
    return PapwExpWriteDataExplainResult(explain_path=explain_path)
