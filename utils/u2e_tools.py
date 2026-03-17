"""u2e_tools — U2E 进化算法工具函数

同步工具，供 tools/*.json 配置引用（type: "tool"）。

工具列表：
    u2e_random_select              — 随机选择 2×pop_size 个亲本对
    u2e_diversity_select           — 多样性感知亲本选择（叠加精英档案多样性奖励）
    u2e_build_crossover_pairs      — 构建 2D 跨函数交叉任务列表（含协同增益预算分配）
    u2e_build_eval_tasks           — 将 individual 列表转换为评估 POST 任务列表
    u2e_aggregate_eval_results     — 将 HTTP 评估响应聚合回 individual 列表
    u2e_parse_responses            — 将 LLM 原始响应列表解析为 individual dict 列表
    u2e_parse_crossover_pair_tasks — 将 2D 交叉任务（无 LLM）直接解析为 individual 列表
    u2e_update_iter                — 更新精英档案、最优目标值、func_individual 追踪状态
    u2e_update_elite_archive       — 维护 top-K 多样性精英档案
    u2e_record_synergy_gains       — 记录 2D 交叉评估后的协同增益
    u2e_retrieve_relevant_memory   — 从结构化记忆中检索相关条目为 prompt 字符串
    u2e_build_reflect_tasks        — 为每对亲本构建短期反思提示数据
    u2e_build_crossover_tasks      — 将反思结果与亲本代码组合为交叉生成提示
    u2e_build_mutate_tasks         — 基于精英个体构建变异提示任务列表
"""
from __future__ import annotations

import copy
import json
import random
import re
from itertools import combinations
from typing import Any

import numpy as np

from utils import ToolResult
from utils.text_utils import _strip_fences


# ── 返回值类型 ─────────────────────────────────────────────────────────────────


class U2eSelectResult(ToolResult):
    """u2e_random_select() 的返回值。"""
    selected: list        # 选出的 2×pop_size 个亲本（成对排列）
    parent_funcs: list    # selected[0]["funcs"]，供后续 parse_responses 使用
    parent_patches: list  # selected[0].get("patches", [])，patch 模式下使用
    success: bool         # 是否选出了足够亲本（False 时 selected/parent_funcs 为空）


class U2eEliteArchiveResult(ToolResult):
    """u2e_update_elite_archive() 的返回值。"""
    elite_archive: list  # top-K 多样性精英个体列表
    elitist: Any         # archive[0]，向后兼容
    best_obj_overall: Any  # 全局最优目标值


class U2eSynergyResult(ToolResult):
    """u2e_record_synergy_gains() 的返回值。"""
    synergy_history: dict  # {"func_a|||func_b": {"total_gain": float, "count": int}}


class U2eRetrieveMemoryResult(ToolResult):
    """u2e_retrieve_relevant_memory() 的返回值。"""
    text: str  # 格式化后的结构化记忆字符串，供 Agent prompt 使用


class U2eCrossPairsResult(ToolResult):
    """u2e_build_crossover_pairs() 的返回值。"""
    tasks: list               # 2D 交叉任务列表，每项: {func_code, parent_funcs, func_index}
    synergy_history_update: dict  # {task_id_str: pair_key}，供 record_synergy_gains 使用


class U2eEvalTasksResult(ToolResult):
    """u2e_build_eval_tasks() 的返回值。"""
    tasks: list            # 仅含有效任务：[{individual_id, url, body, func_index, timeout}, ...]
    valid_population: list  # 与 tasks 等长，有效个体列表（供 aggregate 对齐）
    invalid_population: list  # 已预标 exec_success=False 的无效个体列表


class U2eJoinTextsResult(ToolResult):
    """u2e_join_texts() 的返回值。"""
    text: str  # 拼合后的字符串


class U2eAggregateEvalResult(ToolResult):
    """u2e_aggregate_eval_results() 的返回值。"""
    population: list      # 已打分的 individual 列表
    function_evals: int   # 更新后的累计评估次数


class U2eParseResponsesResult(ToolResult):
    """u2e_parse_responses() / u2e_parse_crossover_pair_tasks() 的返回值。"""
    population: list   # individual dict 列表


class U2eUpdateIterResult(ToolResult):
    """u2e_update_iter() 的返回值。"""
    func_individual: dict          # {func_name: [individual, ...]}
    elitist: Any                   # 全局精英个体（dict 或 None）
    best_obj_overall: Any          # 全局最优目标值（float 或 None）
    best_code_overall: Any         # 最优代码字符串（str 或 None）
    best_code_func_index_overall: Any  # 最优代码对应 func_index（int 或 None）
    func_best_obj_overall: dict    # {func_name: float}
    func_elitist: dict             # {func_name: individual_dict}
    iteration: int                 # 迭代号 +1
    function_evals: int            # 累计函数评估次数（透传，供前端展示）
    should_continue: bool          # function_evals < max_fe
    elite_archive: list            # top-K 多样性精英档案


class U2eReflectTasksResult(ToolResult):
    """u2e_build_reflect_tasks() 的返回值。"""
    tasks: list  # [{worse_code, better_code, func_name, func_desc, problem_desc}, ...]


class U2eCrossoverTasksResult(ToolResult):
    """u2e_build_crossover_tasks() 的返回值。"""
    tasks: list  # [{reflection, worse_code, better_code, user_generator,
    #               func_signature, func_name, long_term_reflection}, ...]


class U2eMutateTasksResult(ToolResult):
    """u2e_build_mutate_tasks() 的返回值。"""
    tasks: list  # n_mutants 个相同任务 [{elitist_code, user_generator,
    #              func_signature, reflection, func_name}, ...]


# ── 内部辅助 ───────────────────────────────────────────────────────────────────


def _extract_code_from_generator(response: str) -> str:
    """从 LLM 响应中提取 Python 代码块（markdown 围栏内的内容）。"""
    return _strip_fences(response)


def _extract_code_from_generators(funcs: list[str]) -> str:
    """将多个函数体拼合为完整 Python 源码文件。"""
    return "\n\n".join(f.strip() for f in funcs)


def _filter_code(code: str) -> str:
    """去除代码首尾空白，便于嵌入 prompt。"""
    return code.strip()


def _build_individual(
    func_code: str,
    parent_funcs: list,
    func_index: int,
    iteration: int,
    response_id: int,
    suffix: str = "",
) -> dict:
    """构建单个 individual dict（parse_responses 和 parse_crossover_pair_tasks 的公共逻辑）。

    Args:
        func_code:    已提取/清理的函数源码（不含 markdown 围栏）。
        parent_funcs: 亲本函数源码列表，会深拷贝后在 func_index 处替换。
        func_index:   本次优化的函数下标。
        iteration:    当前迭代编号（用于生成文件路径）。
        response_id:  本个体的唯一 ID（含 id_offset）。
        suffix:       文件名中间缀，普通 crossover 为 ""，2D 交叉为 "2d_"。
    """
    temp_funcs = copy.deepcopy(parent_funcs)
    temp_funcs[func_index] = func_code.replace("_v2", "").replace("_v1", "")
    code = _extract_code_from_generators(temp_funcs)
    return {
        "stdout_filepath": f"problem_iter{iteration}_{suffix}stdout{response_id}.txt",
        "code_path":       f"problem_iter{iteration}_funcIndex{func_index}_{suffix}code{response_id}.txt",
        "code":            code,
        "funcs":           temp_funcs,
        "func_index":      func_index,
        "response_id":     response_id,
    }


def _aggregate_scores(scores: list[dict]) -> float:
    """将多指标评估结果聚合为单一 obj 值（统一最小化）。

    direction="max" 的指标取负值，使所有指标统一为最小化问题。
    """
    total = 0.0
    for s in scores:
        value = float(s["value"])
        direction = s.get("direction", "min")
        weight = float(s.get("weight", 1.0))
        total += weight * value if direction == "min" else -weight * value
    return total


# ── 工具函数 ──────────────────────────────────────────────────────────────────


def u2e_random_select(
    population: list,
    pop_size: int,
    problem_type: str = "white_box",
    seed_obj: float | None = None,
) -> U2eSelectResult:
    """随机选择亲本对，供后续交叉使用。

    过滤掉无效个体（exec_success=False），black_box 模式下还过滤不优于 seed 的个体。
    选出 2×pop_size 个体，两两成对，保证每对目标值不相同。

    Args:
        population:   当前函数的 individual 列表（含 exec_success / obj 字段）
        pop_size:     期望选出的 pair 数量（输出 2×pop_size 个体）
        problem_type: "black_box" | 其他（默认 white_box）
        seed_obj:     seed 个体目标值，black_box 模式下用于过滤

    Returns:
        U2eSelectResult:
            .selected      选出的 2×pop_size 个亲本（[p1, p2, p1, p2, ...]）
            .parent_funcs  selected[0]["funcs"]，供 parse_responses 使用
            .success       是否选出了足够亲本（False 时两字段均为空列表）
    """
    if not population:
        return U2eSelectResult(selected=[], parent_funcs=[], parent_patches=[], success=False)

    if problem_type == "black_box" and seed_obj is not None:
        valid = [
            ind for ind in population
            if ind.get("exec_success") and ind.get("obj", float("inf")) < seed_obj
        ]
    else:
        valid = [ind for ind in population if ind.get("exec_success")]

    if len(valid) < 2:
        return U2eSelectResult(selected=[], parent_funcs=[], parent_patches=[], success=False)

    selected: list = []
    trial = 0
    while len(selected) < 2 * pop_size:
        trial += 1
        parents = random.sample(valid, 2)
        obj0 = parents[0].get("obj", float("inf"))
        obj1 = parents[1].get("obj", float("inf"))
        if obj0 != obj1:
            selected.extend(parents)
        elif random.random() < 0.2 and parents[0].get("code") != parents[1].get("code"):
            selected.extend(parents)
        if trial > 1000:
            return U2eSelectResult(selected=[], parent_funcs=[], parent_patches=[], success=False)

    parent_funcs = list(selected[0].get("funcs", []))
    parent_patches = list(selected[0].get("patches", []))
    return U2eSelectResult(selected=selected, parent_funcs=parent_funcs, parent_patches=parent_patches, success=True)


def u2e_build_crossover_pairs(
    func_individual: dict,
    func_names: list,
    synergy_history: dict | None = None,
    min_individuals: int = 5,
    max_per_pair: int = 20,
    total_budget: int | None = None,
) -> U2eCrossPairsResult:
    """构建 2D 跨函数交叉任务列表。

    遍历所有满足条件的函数对组合，从各自 individual 池中随机配对并交叉拼合，
    产出无需 LLM 的纯遗传交叉任务。当传入 synergy_history 时，根据历史协同增益
    按比例分配各函数对的任务预算（高增益对获得更多任务）。

    Args:
        func_individual:  {func_name: [individual, ...]} 各函数当前种群
        func_names:       全量函数名列表（决定 func_index 偏移）
        synergy_history:  {"func_a|||func_b": {"total_gain": float, "count": int}}
                          历史协同增益记录，初始为 {}
        min_individuals:  函数须有至少此数量有效个体才参与交叉（默认 5）
        max_per_pair:     每对函数最多产生的交叉任务数（默认 20）
        total_budget:     所有对的总任务数上限；None 时每对独立按 max_per_pair 计

    Returns:
        U2eCrossPairsResult:
            .tasks                每项 {func_code, parent_funcs, func_index, task_id}
            .synergy_history_update  {task_id_str: pair_key}，供后续 record_synergy_gains 使用
    """
    if synergy_history is None:
        synergy_history = {}

    # 筛选有足够有效个体的函数
    ind_map: dict[str, list] = {}
    for k in func_names:
        pool = [
            ind for ind in func_individual.get(k, [])
            if ind and ind.get("obj", float("inf")) < 1e23 and ind.get("exec_success")
        ]
        if len(pool) >= min_individuals:
            ind_map[k] = pool

    ind_keys = list(ind_map.keys())
    if len(ind_keys) < 2:
        return U2eCrossPairsResult(tasks=[], synergy_history_update={})

    # 计算每个函数对的协同得分和可用任务数上限
    valid_pairs: list[tuple[str, str]] = list(combinations(ind_keys, 2))
    pair_scores: list[float] = []
    pair_max_n: list[int] = []
    for key_1, key_2 in valid_pairs:
        pair_key = "|||".join(sorted([key_1, key_2]))
        hist = synergy_history.get(pair_key, {})
        if hist.get("count", 0) > 0:
            avg_gain = hist["total_gain"] / hist["count"]
            score = max(0.0, avg_gain) + 1.0
        else:
            score = 1.0  # 未探索对给中性分
        pair_scores.append(score)
        pool_n = min(len(ind_map[key_1]), len(ind_map[key_2]))
        pair_max_n.append(min(pool_n, max_per_pair))

    # 按协同得分比例分配预算
    if total_budget is not None and total_budget > 0:
        total_score = sum(pair_scores)
        budgets = [
            max(1, min(mx, round(total_budget * s / total_score)))
            for s, mx in zip(pair_scores, pair_max_n)
        ]
    else:
        budgets = pair_max_n

    tasks: list = []
    synergy_history_update: dict = {}
    task_counter = 0

    for (key_1, key_2), n in zip(valid_pairs, budgets):
        pair_key = "|||".join(sorted([key_1, key_2]))
        pool_1 = list(ind_map[key_1])
        pool_2 = list(ind_map[key_2])
        random.shuffle(pool_1)
        random.shuffle(pool_2)
        ki_1 = func_names.index(key_1)
        ki_2 = func_names.index(key_2)
        for i in range(n):
            tid = str(task_counter)
            tasks.append({
                "func_code":    pool_1[i]["funcs"][ki_1],
                "parent_funcs": pool_2[i]["funcs"],
                "func_index":   ki_1,
                "task_id":      task_counter,
            })
            synergy_history_update[tid] = pair_key
            task_counter += 1

            tid = str(task_counter)
            tasks.append({
                "func_code":    pool_2[i]["funcs"][ki_2],
                "parent_funcs": pool_1[i]["funcs"],
                "func_index":   ki_2,
                "task_id":      task_counter,
            })
            synergy_history_update[tid] = pair_key
            task_counter += 1

    return U2eCrossPairsResult(tasks=tasks, synergy_history_update=synergy_history_update)


def u2e_build_eval_tasks(
    population: list,
    init_generated_funcs: list,
    evaluate_funcs_url: str,
    timeout: float = 60.0,
) -> U2eEvalTasksResult:
    """将 individual 列表转换为 HTTP 评估任务列表。

    每个有效 individual 产出一个 {url, body, ...} 任务供下游 map+http_post 并发执行；
    无效个体（code 为 None）预标 exec_success=False，不进入评估队列。

    Args:
        population:           待评估的 individual 列表
        init_generated_funcs: 原始函数元信息列表（来自 get_funcs API）
        evaluate_funcs_url:   评估服务 URL（POST 目标）
        timeout:              单次 HTTP 请求超时秒数（默认 60）

    Returns:
        U2eEvalTasksResult:
            .tasks             仅含有效任务（无 skip 占位），与 valid_population 等长
            .valid_population  对应有效任务的 individual 列表（供 aggregate 对齐）
            .invalid_population 已预标 exec_success=False 的无效 individual 列表
    """
    tasks: list = []
    valid_population: list = []
    invalid_population: list = []

    for idx, individual in enumerate(population):
        fi = individual.get("func_index", -1)
        if individual.get("code") is None:
            # 预标记为无效，不进入 map
            invalid_ind = copy.deepcopy(individual)
            invalid_ind["exec_success"] = False
            invalid_ind["obj"] = float("inf")
            invalid_ind["traceback_msg"] = "Invalid response (code is None)"
            invalid_population.append(invalid_ind)
            continue

        # 构建 func_list（对应 _build_func_list_for_eval）
        func_list: list = []
        for i, func_dict in enumerate(init_generated_funcs):
            entry = dict(func_dict)
            if fi != -1 and i == fi:
                entry["func_source"] = individual["funcs"][i]
                entry["is_modified"] = True
            else:
                entry["is_modified"] = False
            func_list.append(entry)

        tasks.append({
            "individual_id": idx,
            "url":           evaluate_funcs_url,
            "body":          func_list,
            "func_index":    fi,
            "timeout":       timeout,
        })
        valid_population.append(individual)

    return U2eEvalTasksResult(
        tasks=tasks,
        valid_population=valid_population,
        invalid_population=invalid_population,
    )


def u2e_aggregate_eval_results(
    eval_results: list,
    valid_population: list,
    invalid_population: list,
    function_evals: int = 0,
) -> U2eAggregateEvalResult:
    """将 HTTP 评估响应列表聚合回 individual，写入 obj / exec_success / scores。

    eval_results 与 valid_population 一一对应（均不含无效项）；
    invalid_population 是已预标记 exec_success=False 的个体，直接合并进输出。

    Args:
        eval_results:       与 valid_population 等长的 http_post 结果列表
                            每项为 HttpPostResult（含 status_code / body）
        valid_population:   有效个体列表（来自 u2e_build_eval_tasks.valid_population）
        invalid_population: 已预标无效个体列表（来自 u2e_build_eval_tasks.invalid_population）
        function_evals:     当前累计评估次数

    Returns:
        U2eAggregateEvalResult:
            .population      所有已打分个体（valid 已更新 obj + invalid 预标）
            .function_evals  更新后的累计评估次数
    """
    scored: list = []
    evals_added = 0

    for individual, result in zip(valid_population, eval_results):
        ind = copy.deepcopy(individual)
        if result is None:
            ind["exec_success"] = False
            ind["obj"] = float("inf")
            ind["traceback_msg"] = "No result (http_post failed)"
            scored.append(ind)
            continue

        evals_added += 1
        try:
            _r = result if isinstance(result, dict) else result.model_dump()
            body_str = _r.get("body", "")
            status = _r.get("status_code", 0)
            if status != 200 or not body_str:
                ind["exec_success"] = False
                ind["obj"] = float("inf")
                ind["traceback_msg"] = f"HTTP {status}: {str(body_str)[:200]}"
            else:
                scores = json.loads(body_str)
                obj = _aggregate_scores(scores)
                ind["obj"] = obj
                ind["exec_success"] = True
                ind["scores"] = scores
        except Exception as e:
            ind["exec_success"] = False
            ind["obj"] = float("inf")
            ind["traceback_msg"] = str(e)
        scored.append(ind)

    # 合并：先有效（已评估），再无效（预标记）
    population = scored + list(invalid_population)
    return U2eAggregateEvalResult(population=population, function_evals=function_evals + evals_added)


def u2e_parse_responses(
    responses: list,
    parent_funcs: list,
    func_index: int,
    iteration: int,
    id_offset: int = 0,
) -> U2eParseResponsesResult:
    """将 LLM 原始响应列表解析为 individual dict 列表。

    从每条 LLM 响应中提取 Python 代码，替换 parent_funcs[func_index]，
    组合为完整 individual dict，code 字段为全部函数拼合后的源码。

    Args:
        responses:    LLM 文本响应列表（每项为一条原始输出字符串）
        parent_funcs: 亲本函数源码列表（会深拷贝后替换对应项）
        func_index:   本次优化的函数下标
        iteration:    当前迭代编号（用于生成 code_path / stdout_filepath）
        id_offset:    response_id 的偏移量（防止同一迭代内 ID 冲突）

    Returns:
        U2eParseResponsesResult:
            .population  individual dict 列表，各项含
                         code / funcs / func_index / response_id /
                         code_path / stdout_filepath
    """
    population = [
        _build_individual(
            func_code=_extract_code_from_generator(response),
            parent_funcs=parent_funcs,
            func_index=func_index,
            iteration=iteration,
            response_id=response_id + id_offset,
        )
        for response_id, response in enumerate(responses)
    ]
    return U2eParseResponsesResult(population=population)


def u2e_parse_crossover_pair_tasks(
    tasks: list,
    iteration: int,
    id_offset: int = 100,
) -> U2eParseResponsesResult:
    """将 2D 交叉任务（无 LLM）直接解析为 individual 列表。

    对应 evolve() 中 2D 交叉段的 response_to_individual_2d(response=func_code, ...) 调用。
    func_code 是已有函数源码（非 LLM 响应），直接替换到 parent_funcs 对应位置。

    Args:
        tasks:      u2e_build_crossover_pairs 输出的 tasks 列表
                    每项含 {func_code, parent_funcs, func_index}
        iteration:  当前迭代编号
        id_offset:  response_id 起始偏移（默认 100，与普通 crossover 区分）

    Returns:
        U2eParseResponsesResult: .population
    """
    population = [
        _build_individual(
            func_code=task["func_code"],
            parent_funcs=task["parent_funcs"],
            func_index=task["func_index"],
            iteration=iteration,
            response_id=task_id + id_offset,
            suffix="2d_",
        )
        for task_id, task in enumerate(tasks)
    ]
    return U2eParseResponsesResult(population=population)


def u2e_update_iter(
    population: list,
    func_names: list,
    func_individual: dict,
    func_best_obj_overall: dict | None,
    func_elitist: dict | None,
    elitist: Any,
    best_obj_overall: Any,
    best_code_overall: Any,
    best_code_func_index_overall: Any,
    iteration: int,
    function_evals: int,
    max_fe: int,
    elite_archive: list | None = None,
    archive_size: int = 10,
    min_diversity: float = 0.1,
) -> U2eUpdateIterResult:
    """更新精英档案、最优目标值以及 func_individual 分类存储。

    将本轮新产出的 population 合并进 func_individual 历史，
    更新全局与各函数的精英个体，维护多样性精英档案，并判断是否继续进化。

    Args:
        population:                   本轮新产出的已评估 individual 列表
        func_names:                   全量函数名列表
        func_individual:              {func_name: [individual, ...]} 历史汇总
        func_best_obj_overall:        {func_name: best_obj} 各函数最优目标值历史
        func_elitist:                 {func_name: elitist_individual} 各函数精英
        elitist:                      当前全局精英个体（dict 或 None）
        best_obj_overall:             当前全局最优目标值（float 或 None）
        best_code_overall:            当前最优代码字符串（str 或 None）
        best_code_func_index_overall: 当前最优代码对应 func_index（int 或 None）
        iteration:                    当前迭代号（返回时 +1）
        function_evals:               当前累计函数评估次数
        max_fe:                       最大评估次数上限

    Returns:
        U2eUpdateIterResult 含所有更新后的状态字段以及 should_continue bool。
    """
    # 图初始状态可能传入 None，规范化为空 dict
    if func_best_obj_overall is None:
        func_best_obj_overall = {}
    if func_elitist is None:
        func_elitist = {}

    valid_pop = [ind for ind in population if ind.get("exec_success")]

    if elite_archive is None:
        elite_archive = []

    # 无有效个体时，仅推进迭代号
    # 若 func_names 为空（无任何待进化函数），直接终止，避免空转
    if not valid_pop:
        continue_flag = (function_evals < max_fe) if func_names else False
        return U2eUpdateIterResult(
            func_individual=func_individual,
            elitist=elitist,
            best_obj_overall=best_obj_overall,
            best_code_overall=best_code_overall,
            best_code_func_index_overall=best_code_func_index_overall,
            func_best_obj_overall=func_best_obj_overall,
            func_elitist=func_elitist,
            iteration=iteration + 1,
            function_evals=function_evals,
            should_continue=continue_flag,
            elite_archive=elite_archive,
        )

    objs = [float(ind["obj"]) for ind in valid_pop]
    best_idx = int(np.argmin(np.array(objs)))
    best_obj = objs[best_idx]

    # 更新全局最优
    new_best_obj  = best_obj_overall
    new_best_code = best_code_overall
    new_best_fi   = best_code_func_index_overall
    if best_obj_overall is None or best_obj <= float(best_obj_overall):
        new_best_obj  = best_obj
        new_best_code = valid_pop[best_idx]["code"]
        new_best_fi   = valid_pop[best_idx].get("func_index")

    # 更新精英档案（多样性约束）
    archive_result = u2e_update_elite_archive(
        population=valid_pop,
        elite_archive=elite_archive,
        archive_size=archive_size,
        min_diversity=min_diversity,
        best_obj_overall=new_best_obj,
    )
    new_elite_archive = archive_result.elite_archive
    new_elitist = archive_result.elitist

    # 按 func_index 分桶，合并进 func_individual，并更新各函数精英
    new_func_individual = copy.deepcopy(func_individual)
    new_func_best_obj   = dict(func_best_obj_overall)
    new_func_elitist    = dict(func_elitist)

    buckets: dict[int, list] = {}
    for ind in valid_pop:
        fi = ind.get("func_index", -1)
        if 0 <= fi < len(func_names):
            buckets.setdefault(fi, []).append(ind)

    for fi, bucket in buckets.items():
        fn = func_names[fi]
        new_func_individual.setdefault(fn, [])
        new_func_individual[fn].extend(bucket)

        b_objs   = [float(ind["obj"]) for ind in bucket]
        b_best_idx = int(np.argmin(np.array(b_objs)))
        b_best_obj = b_objs[b_best_idx]

        if b_best_obj <= float(new_func_best_obj.get(fn, 1e10)):
            new_func_best_obj[fn]  = b_best_obj
            new_func_elitist[fn]   = bucket[b_best_idx]

    return U2eUpdateIterResult(
        func_individual=new_func_individual,
        elitist=new_elitist,
        best_obj_overall=new_best_obj,
        best_code_overall=new_best_code,
        best_code_func_index_overall=new_best_fi,
        func_best_obj_overall=new_func_best_obj,
        func_elitist=new_func_elitist,
        iteration=iteration + 1,
        function_evals=function_evals,
        should_continue=function_evals < max_fe,
        elite_archive=new_elite_archive,
    )


def u2e_build_reflect_tasks(
    selected_population: list,
    func_name: str,
    func_desc: str,
    problem_desc: str,
) -> U2eReflectTasksResult:
    """为每对选中的亲本构建短期反思提示数据。

    selected_population 已是 2×pop_size 排列，两两成对，
    每对中 obj 较小的为 better，较大的为 worse。

    Args:
        selected_population: 已选中的亲本列表（偶数长度，成对排列）
        func_name:           本次优化的函数名
        func_desc:           本次优化的函数描述
        problem_desc:        问题描述字符串

    Returns:
        U2eReflectTasksResult:
            .tasks  每项 {worse_code, better_code, func_name, func_desc, problem_desc}
    """
    tasks: list = []
    for i in range(0, len(selected_population) - 1, 2):
        p1 = selected_population[i]
        p2 = selected_population[i + 1]
        fi = p1.get("func_index", 0)

        if p1.get("obj", float("inf")) <= p2.get("obj", float("inf")):
            better, worse = p1, p2
        else:
            better, worse = p2, p1

        tasks.append({
            "worse_code":   _filter_code(worse.get("funcs", [""])[fi]),
            "better_code":  _filter_code(better.get("funcs", [""])[fi]),
            "func_name":    func_name,
            "func_desc":    func_desc,
            "problem_desc": problem_desc,
        })

    return U2eReflectTasksResult(tasks=tasks)


def u2e_build_crossover_tasks(
    reflections: list,
    reflect_tasks: list,
    user_generator_prompt: str,
    func_signature: str,
    long_term_reflection: str,
    func_name: str,
) -> U2eCrossoverTasksResult:
    """将短期反思结果与亲本代码组合为交叉生成任务。

    每条 reflection 对应一个交叉任务，传给下游 map+agent 并发生成新代码。

    Args:
        reflections:           短期反思 LLM 输出列表（与 reflect_tasks 等长）
        reflect_tasks:         u2e_build_reflect_tasks 产出的 tasks
                               （含 worse_code / better_code）
        user_generator_prompt: 当前函数的 user_generator 提示词
        func_signature:        当前函数签名（crossover prompt 中 worse/better 共用）
        long_term_reflection:  当前长期反思字符串（仅供参考，不直接嵌入 crossover prompt）
        func_name:             当前函数名

    Returns:
        U2eCrossoverTasksResult:
            .tasks  每项含 crossover agent 所需的全部 prompt 变量
    """
    tasks: list = []
    for reflection, rt in zip(reflections, reflect_tasks):
        tasks.append({
            "reflection":          reflection,
            "worse_code":          rt["worse_code"],
            "better_code":         rt["better_code"],
            "user_generator":      user_generator_prompt,
            "func_signature":      func_signature,
            "long_term_reflection": long_term_reflection,
            "func_name":           func_name,
        })
    return U2eCrossoverTasksResult(tasks=tasks)


def u2e_build_mutate_tasks(
    elitist: dict,
    user_generator_prompt: str,
    func_signature: str,
    long_term_reflection: str,
    external_knowledge: str,
    func_name: str,
    pop_size: int,
    mutation_rate: float,
) -> U2eMutateTasksResult:
    """基于全局精英个体和结构化记忆构建变异提示任务列表。

    产出 int(pop_size × mutation_rate) 个内容相同的任务，
    由下游 map+agent 并发生成，LLM temperature 保证输出多样性。

    Args:
        elitist:               当前精英个体（funcs / code 字段必须存在）
        user_generator_prompt: 当前函数的 user_generator 提示词
        func_signature:        当前函数签名
        long_term_reflection:  当前长期反思字符串
        external_knowledge:    外部知识字符串
        func_name:             当前函数名
        pop_size:              种群大小（用于计算变异数量）
        mutation_rate:         变异比例（用于计算变异数量）

    Returns:
        U2eMutateTasksResult:
            .tasks  int(pop_size × mutation_rate) 个变异任务列表
                    每项 {elitist_code, user_generator, func_signature, reflection, func_name}
    """
    # elitist 为 None 时（初始迭代尚未产生精英个体），跳过变异
    if not elitist:
        return U2eMutateTasksResult(tasks=[])

    n_mutants = max(1, int(pop_size * mutation_rate))
    # reflection = long_term_reflection + external_knowledge（对应原始代码拼接逻辑）
    reflection = (long_term_reflection + external_knowledge).strip()
    elitist_code = _filter_code(elitist.get("code", ""))

    task = {
        "elitist_code":   elitist_code,
        "user_generator": user_generator_prompt,
        "func_signature": func_signature,
        "reflection":     reflection,
        "func_name":      func_name,
    }
    return U2eMutateTasksResult(tasks=[copy.copy(task) for _ in range(n_mutants)])


def u2e_join_texts(
    texts: list,
    separator: str = "\n",
) -> U2eJoinTextsResult:
    """将字符串列表拼合为单一字符串，供 Agent prompt 模板使用。

    用于将 map 步骤产出的 list[str] 转换为 Agent user_variables 能接受的 str。

    Args:
        texts:     字符串列表（map 步骤的输出）
        separator: 分隔符（默认换行）

    Returns:
        U2eJoinTextsResult:
            .text  拼合后的字符串
    """
    joined = separator.join(str(t) for t in (texts or []))
    return U2eJoinTextsResult(text=joined)


# ── 改进一：精英档案 + 多样性感知选择 ──────────────────────────────────────────


def _code_diversity(code_a: str, code_b: str) -> float:
    """计算两段代码的多样性距离（1 - 行集合 Jaccard 相似度）。

    使用代码行集合的 Jaccard 距离，O(N)，足够用于多样性过滤。
    """
    lines_a = set(code_a.splitlines())
    lines_b = set(code_b.splitlines())
    if not lines_a and not lines_b:
        return 0.0
    intersection = len(lines_a & lines_b)
    union = len(lines_a | lines_b)
    return 1.0 - intersection / union if union > 0 else 0.0


def u2e_update_elite_archive(
    population: list,
    elite_archive: list,
    archive_size: int = 10,
    min_diversity: float = 0.1,
    best_obj_overall: Any = None,
) -> U2eEliteArchiveResult:
    """维护 top-K 多样性精英档案。

    将本轮有效个体与已有档案合并，按目标值排序后贪心构建新档案：
    逐一尝试加入候选，要求与已接受的所有个体代码多样性距离 >= min_diversity。

    Args:
        population:       本轮有效 individual 列表（已过滤 exec_success=True）
        elite_archive:    当前档案（上一轮输出），初始为 []
        archive_size:     档案最大容量（默认 10）
        min_diversity:    候选与已有档案成员间的最小代码距离阈值（默认 0.1）
        best_obj_overall: 当前全局最优值（用于决定 elitist）

    Returns:
        U2eEliteArchiveResult:
            .elite_archive  更新后的档案列表（按 obj 升序）
            .elitist        archive[0]（全局最优），向后兼容
            .best_obj_overall 全局最优目标值
    """
    candidates = [ind for ind in (elite_archive + population) if ind.get("exec_success")]
    if not candidates:
        return U2eEliteArchiveResult(
            elite_archive=list(elite_archive),
            elitist=elite_archive[0] if elite_archive else None,
            best_obj_overall=best_obj_overall,
        )

    # 按 obj 升序排序（越小越好）
    candidates.sort(key=lambda x: float(x.get("obj", float("inf"))))

    new_archive: list = []
    for cand in candidates:
        if len(new_archive) >= archive_size:
            break
        cand_code = cand.get("code", "")
        if all(_code_diversity(cand_code, a.get("code", "")) >= min_diversity
               for a in new_archive):
            new_archive.append(cand)

    # 若贪心结果为空（所有候选彼此相似），直接取最优
    if not new_archive:
        new_archive = [candidates[0]]

    best = new_archive[0]
    new_best_obj = float(best.get("obj", float("inf")))
    if best_obj_overall is not None:
        new_best_obj = min(new_best_obj, float(best_obj_overall))

    return U2eEliteArchiveResult(
        elite_archive=new_archive,
        elitist=best,
        best_obj_overall=new_best_obj,
    )


def u2e_diversity_select(
    population: list,
    elite_archive: list,
    pop_size: int,
    problem_type: str = "white_box",
    seed_obj: float | None = None,
    diversity_weight: float = 0.3,
) -> U2eSelectResult:
    """多样性感知亲本选择，替代 u2e_random_select。

    在纯随机选择的基础上，引入多样性奖励：相对于精英档案更"独特"的个体
    获得更高的被选概率，从而避免种群向单一方向收敛。

    Args:
        population:       当前函数的 individual 列表
        elite_archive:    当前精英档案（u2e_update_elite_archive 产出）
        pop_size:         期望选出的 pair 数量（输出 2×pop_size 个体）
        problem_type:     "black_box" | 其他（默认 white_box）
        seed_obj:         seed 个体目标值，black_box 模式下用于过滤
        diversity_weight: 多样性奖励权重（0~1，默认 0.3）

    Returns:
        U2eSelectResult（与 u2e_random_select 相同结构）
    """
    if not population:
        return U2eSelectResult(selected=[], parent_funcs=[], parent_patches=[], success=False)

    if problem_type == "black_box" and seed_obj is not None:
        valid = [
            ind for ind in population
            if ind.get("exec_success") and ind.get("obj", float("inf")) < seed_obj
        ]
    else:
        valid = [ind for ind in population if ind.get("exec_success")]

    if len(valid) < 2:
        return U2eSelectResult(selected=[], parent_funcs=[], parent_patches=[], success=False)

    # 计算各候选的多样性奖励：与档案的平均距离（无档案时退化为随机）
    if elite_archive:
        diversity_scores = []
        for ind in valid:
            code = ind.get("code", "")
            avg_dist = sum(_code_diversity(code, a.get("code", "")) for a in elite_archive) / len(elite_archive)
            diversity_scores.append(avg_dist)
    else:
        diversity_scores = [1.0] * len(valid)

    # 计算目标值排名得分（rank 越小=越优，得分越高）
    objs = [float(ind.get("obj", float("inf"))) for ind in valid]
    sorted_indices = sorted(range(len(objs)), key=lambda i: objs[i])
    rank_scores = [0.0] * len(valid)
    for rank, idx in enumerate(sorted_indices):
        rank_scores[idx] = 1.0 / (rank + 1)

    # 归一化后混合
    max_rank = max(rank_scores) or 1.0
    max_div  = max(diversity_scores) or 1.0
    weights = [
        (1 - diversity_weight) * (r / max_rank) + diversity_weight * (d / max_div)
        for r, d in zip(rank_scores, diversity_scores)
    ]

    selected: list = []
    trial = 0
    while len(selected) < 2 * pop_size:
        trial += 1
        parents = random.choices(valid, weights=weights, k=2)
        obj0 = parents[0].get("obj", float("inf"))
        obj1 = parents[1].get("obj", float("inf"))
        if obj0 != obj1:
            selected.extend(parents)
        elif random.random() < 0.2 and parents[0].get("code") != parents[1].get("code"):
            selected.extend(parents)
        if trial > 1000:
            return U2eSelectResult(selected=[], parent_funcs=[], parent_patches=[], success=False)

    parent_funcs = list(selected[0].get("funcs", []))
    parent_patches = list(selected[0].get("patches", []))
    return U2eSelectResult(selected=selected, parent_funcs=parent_funcs, parent_patches=parent_patches, success=True)


# ── 改进二：协同增益追踪 ────────────────────────────────────────────────────────


def u2e_record_synergy_gains(
    crossed_population: list,
    synergy_history_update: dict,
    synergy_history: dict,
    func_best_obj_overall: dict,
    func_names: list,
) -> U2eSynergyResult:
    """记录 2D 交叉实际产生的协同增益，更新 synergy_history。

    评估完成后调用，对每个成功执行的 2D 交叉子代，计算其相对于所在函数历史
    最优目标值的改善量，累积到对应函数对的协同增益记录中。

    Args:
        crossed_population:      2D 交叉后已评估的 individual 列表
        synergy_history_update:  {task_id_str: pair_key}，来自 u2e_build_crossover_pairs
        synergy_history:         当前协同增益历史（{"func_a|||func_b": {"total_gain", "count"}}）
        func_best_obj_overall:   {func_name: best_obj} 各函数当前历史最优
        func_names:              全量函数名列表（用于 func_index → func_name 映射）

    Returns:
        U2eSynergyResult:
            .synergy_history  更新后的协同增益字典
    """
    new_history = copy.deepcopy(synergy_history)

    for ind in crossed_population:
        if not ind.get("exec_success"):
            continue
        task_id = str(ind.get("task_id", ind.get("response_id", "")))
        pair_key = synergy_history_update.get(task_id)
        if not pair_key:
            continue

        fi = ind.get("func_index", -1)
        if not (0 <= fi < len(func_names)):
            continue
        fn = func_names[fi]
        hist_best = func_best_obj_overall.get(fn, float("inf"))
        gain = float(hist_best) - float(ind.get("obj", float("inf")))

        entry = new_history.setdefault(pair_key, {"total_gain": 0.0, "count": 0})
        entry["total_gain"] += gain
        entry["count"] += 1

    return U2eSynergyResult(synergy_history=new_history)


# ── 改进三：结构化记忆检索 ──────────────────────────────────────────────────────


def u2e_retrieve_relevant_memory(
    structured_memory: dict,
    func_name: str,
    context: str = "mutate",
    max_entries_per_category: int = 3,
) -> U2eRetrieveMemoryResult:
    """从结构化长期记忆中检索与当前任务最相关的条目，格式化为提示词字符串。

    根据 context（mutate 或 crossover）调整各分类的展示优先级：
    - mutate：强调 principles 和 best_tricks（直接指导生成）
    - crossover：强调 best_tricks 和 principles（理解改善方向）
    两种场景均展示 failed_patterns（避免已知死路）。

    Args:
        structured_memory:       结构化记忆字典，含 principles / failed_patterns / best_tricks 三个列表
        func_name:               当前优化函数名（保留接口，未来可按函数过滤）
        context:                 "mutate" | "crossover"，决定展示侧重
        max_entries_per_category: 每分类最多展示条数（默认 3）

    Returns:
        U2eRetrieveMemoryResult:
            .text  格式化的记忆字符串，直接拼入 Agent prompt
    """
    if not structured_memory:
        return U2eRetrieveMemoryResult(text="")

    principles      = list(structured_memory.get("principles",      []))[:max_entries_per_category]
    failed_patterns = list(structured_memory.get("failed_patterns", []))[:max_entries_per_category]
    best_tricks     = list(structured_memory.get("best_tricks",     []))[:max_entries_per_category]

    if context == "crossover":
        # 交叉场景：先展示 best_tricks 帮助理解改善方向
        sections = [
            ("Best tricks from past improvements", best_tricks),
            ("General principles",                 principles),
            ("Patterns to avoid",                  failed_patterns),
        ]
    else:
        # 变异场景：先展示 principles 指导全局方向
        sections = [
            ("General principles",                 principles),
            ("Best tricks from past improvements", best_tricks),
            ("Patterns to avoid",                  failed_patterns),
        ]

    parts: list[str] = []
    for title, entries in sections:
        if entries:
            bullet = "\n".join(f"- {e}" for e in entries)
            parts.append(f"[{title}]\n{bullet}")

    text = "\n\n".join(parts) if parts else ""
    return U2eRetrieveMemoryResult(text=text)


# ── patch 模式：返回值类型 ─────────────────────────────────────────────────────


class U2eTreeToFuncTasksResult(ToolResult):
    """u2e_tree_to_func_tasks() 的返回值。"""
    func_tasks: list          # [{func_index, func_name, func_desc, func_signature, target_file, target_type, initial_funcs}]
    func_names: list          # 所有函数名有序列表
    initial_func_individual: dict  # {func_name: [initial_individual]}（种群初始化用）


class U2eSandboxEvalTasksResult(ToolResult):
    """u2e_build_sandbox_eval_tasks() 的返回值。"""
    tasks: list               # [{individual_id, sandbox_in_ch, patches}, ...]
    valid_population: list    # 有效个体列表（与 tasks 对齐）
    invalid_population: list  # 无效个体列表（已预标 exec_success=False）


class U2eAggregateSandboxResult(ToolResult):
    """u2e_aggregate_sandbox_results() 的返回值。"""
    population: list      # 所有已打分个体（exec_success/obj/score 已写入）
    function_evals: int   # 更新后的累计评估次数


class U2eBuildFuncIterTasksResult(ToolResult):
    """u2e_build_func_iter_tasks() 的返回值。"""
    func_iter_tasks: list  # 每函数的完整 iter 输入 dict 列表


class U2eCollectFuncResultsResult(ToolResult):
    """u2e_collect_func_results() 的返回值。"""
    population: list              # 所有函数本轮 individual flat 列表
    func_structured_memory: dict  # {func_name: structured_memory}
    function_evals: int           # 更新后的累计评估次数


# ── patch 模式：工具函数 ────────────────────────────────────────────────────────


def u2e_tree_to_func_tasks(
    tree: list,
    problem_desc: str,
    base_patches: list | None = None,
) -> U2eTreeToFuncTasksResult:
    """从 arch_to_project 输出的 tree 中提取所有函数节点，构建 U2E 初始化任务列表。

    遍历 file 节点建立 fn_id → target_file 反向映射；
    遍历 fn 节点提取 func_name / desc / signature / code；
    base_patches（如 MCTS 最优节点 patches）应用到初始代码上。

    Args:
        tree:         arch_to_project 输出的 tree（fn 节点含 code 字段）。
        problem_desc: 问题描述，传递给 U2E 各函数进化子图。
        base_patches: 可选 patch 列表，MCTS 最优 patches，用于计算初始代码。

    Returns:
        U2eTreeToFuncTasksResult:
            .func_tasks               每函数元数据 + initial_funcs 列表
            .func_names               函数名有序列表
            .initial_func_individual  {func_name: [initial_individual]}
    """
    from utils.mcts.tools import _lookup_code_from_tree

    # 建立 fn_id → target_file 反向映射
    fn_to_file: dict = {}
    for node in tree:
        if node.get("kind") == "file":
            target_file = node.get("path", "")
            for fn_id in node.get("functions", []):
                fn_to_file[fn_id] = target_file

    # 收集 fn/type 节点（按出现顺序）
    fn_nodes = [n for n in tree if n.get("kind") in ("function", "type")]

    # 计算所有函数应用 base_patches 后的初始代码
    initial_funcs: list[str] = []
    for fn_node in fn_nodes:
        fn_id = fn_node.get("id", "")
        target_name = fn_id.split("::", 1)[-1] if "::" in fn_id else fn_id
        code = _lookup_code_from_tree(tree, base_patches or [], target_name)
        if not code:
            code = fn_node.get("code", "")
        initial_funcs.append(code)

    # 构建 func_tasks 列表
    func_tasks: list = []
    func_names: list = []
    for fi, fn_node in enumerate(fn_nodes):
        fn_id = fn_node.get("id", "")
        target_name = fn_id.split("::", 1)[-1] if "::" in fn_id else fn_id
        target_file = fn_to_file.get(fn_id, "")

        # 构建函数签名字符串
        params = fn_node.get("params", [])
        returns = fn_node.get("returns", "")
        if params:
            param_str = ", ".join(
                f"{p.get('name', '')}: {p.get('type', 'Any')}" if p.get("type")
                else p.get("name", "")
                for p in params
            )
        else:
            param_str = ""
        func_signature = f"def {target_name}({param_str})"
        if returns:
            func_signature += f" -> {returns}"

        func_tasks.append({
            "func_index":     fi,
            "func_name":      target_name,
            "func_desc":      fn_node.get("description", ""),
            "func_signature": func_signature,
            "target_file":    target_file,
            "target_type":    "function",
            "initial_funcs":  list(initial_funcs),
        })
        func_names.append(target_name)

    # 构建每函数初始 patches（覆盖所有函数，全量快照）
    initial_patches: list = []
    for fi, fn_node in enumerate(fn_nodes):
        fn_id = fn_node.get("id", "")
        target_name = fn_id.split("::", 1)[-1] if "::" in fn_id else fn_id
        task = func_tasks[fi]
        initial_patches.append({
            "action":      "rewrite",
            "target_file": task["target_file"],
            "target_type": task["target_type"],
            "target_name": target_name,
            "code":        initial_funcs[fi],
        })

    # 构建初始 func_individual（每函数一个初始 individual）
    initial_func_individual: dict = {}
    full_code = _extract_code_from_generators(initial_funcs)
    for fi, task in enumerate(func_tasks):
        fn_name = task["func_name"]
        ind = {
            "stdout_filepath": f"problem_iter0_stdout_init_{fi}.txt",
            "code_path":       f"problem_iter0_funcIndex{fi}_code_init.txt",
            "code":            full_code,
            "funcs":           list(initial_funcs),
            "func_index":      fi,
            "response_id":     -1,
            "patches":         list(initial_patches),
            "exec_success":    False,
            "obj":             float("inf"),
        }
        initial_func_individual[fn_name] = [ind]  # list 形式供 u2e_build_func_iter_tasks 使用

    return U2eTreeToFuncTasksResult(
        func_tasks=func_tasks,
        func_names=func_names,
        initial_func_individual=initial_func_individual,
    )


def u2e_parse_responses_patch(
    responses: list,
    parent_funcs: list,
    parent_patches: list,
    func_index: int,
    iteration: int,
    target_file: str,
    func_name: str,
    target_type: str = "function",
    id_offset: int = 0,
) -> U2eParseResponsesResult:
    """patch 模式：将 LLM 响应解析为 individual dict 列表。

    每个 individual 继承 parent_patches（全量函数快照），仅替换目标函数 patch。
    这保留了跨函数协同效应（2D 结构）。

    Args:
        responses:      LLM 文本响应列表
        parent_funcs:   亲本函数源码列表（供 code 字段拼合）
        parent_patches: 亲本 patches 列表（继承非目标函数的 patch）
        func_index:     本次优化的函数下标
        iteration:      当前迭代编号（用于生成文件路径）
        target_file:    目标函数所在文件相对路径
        func_name:      目标函数名
        target_type:    "function" 或 "class"，默认 "function"
        id_offset:      response_id 偏移量（防止同一迭代内 ID 冲突）

    Returns:
        U2eParseResponsesResult: .population
    """
    population = []
    for response_id, response in enumerate(responses):
        func_code = _extract_code_from_generator(response).replace("_v2", "").replace("_v1", "")

        # 更新 funcs 列表（目标函数位置替换为新代码）
        temp_funcs = copy.deepcopy(parent_funcs)
        temp_funcs[func_index] = func_code
        code = _extract_code_from_generators(temp_funcs)

        # 继承 parent_patches，替换目标函数对应的 patch
        new_patches = copy.deepcopy(parent_patches)
        replaced = False
        for i, p in enumerate(new_patches):
            if p.get("target_name") == func_name:
                new_patches[i] = {
                    "action":      "rewrite",
                    "target_file": target_file,
                    "target_type": target_type,
                    "target_name": func_name,
                    "code":        func_code,
                }
                replaced = True
                break
        if not replaced:
            new_patches.append({
                "action":      "rewrite",
                "target_file": target_file,
                "target_type": target_type,
                "target_name": func_name,
                "code":        func_code,
            })

        population.append({
            "stdout_filepath": f"problem_iter{iteration}_stdout{response_id + id_offset}.txt",
            "code_path":       f"problem_iter{iteration}_funcIndex{func_index}_code{response_id + id_offset}.txt",
            "code":            code,
            "funcs":           temp_funcs,
            "func_index":      func_index,
            "response_id":     response_id + id_offset,
            "patches":         new_patches,
        })

    return U2eParseResponsesResult(population=population)


def u2e_build_sandbox_eval_tasks(
    population: list,
    sandbox_in_ch: str,
) -> U2eSandboxEvalTasksResult:
    """构建沙盒评估任务列表（patch 模式）。

    过滤不含 patches 的无效个体；有效个体打包为 {sandbox_in_ch, patches} 任务。

    Args:
        population:    待评估的 individual 列表（每项应含 patches 字段）
        sandbox_in_ch: 共享沙箱会话输入通道 ID

    Returns:
        U2eSandboxEvalTasksResult
    """
    tasks: list = []
    valid_population: list = []
    invalid_population: list = []

    for idx, individual in enumerate(population):
        patches = individual.get("patches")
        code = individual.get("code")

        if not patches or code is None:
            invalid_ind = copy.deepcopy(individual)
            invalid_ind["exec_success"] = False
            invalid_ind["obj"] = float("inf")
            invalid_ind["traceback_msg"] = "Invalid individual: missing patches or code"
            invalid_population.append(invalid_ind)
            continue

        tasks.append({
            "individual_id": idx,
            "sandbox_in_ch": sandbox_in_ch,
            "patches":       patches,
        })
        valid_population.append(individual)

    return U2eSandboxEvalTasksResult(
        tasks=tasks,
        valid_population=valid_population,
        invalid_population=invalid_population,
    )


def u2e_aggregate_sandbox_results(
    eval_results: list,
    valid_population: list,
    invalid_population: list,
    function_evals: int = 0,
) -> U2eAggregateSandboxResult:
    """将沙盒评估结果聚合回 individual，写入 exec_success / obj / score。

    eval_results 为 map(u2e_eval_via_mcts_sandbox) 的结果列表，与 valid_population 一一对应。
    方向转换：MCTS score（越高越好）→ U2E obj = -score（越低越好）。

    Args:
        eval_results:       沙盒评估结果列表（每项含 ok / score / error 等字段）
        valid_population:   有效个体列表（来自 u2e_build_sandbox_eval_tasks.valid_population）
        invalid_population: 已预标无效个体列表（直接合并进输出）
        function_evals:     当前累计评估次数

    Returns:
        U2eAggregateSandboxResult
    """
    scored: list = []
    evals_added = 0

    for individual, result in zip(valid_population, eval_results):
        ind = copy.deepcopy(individual)
        if result is None:
            ind["exec_success"] = False
            ind["obj"] = float("inf")
            ind["traceback_msg"] = "No result from sandbox"
            scored.append(ind)
            continue

        evals_added += 1
        r = result if isinstance(result, dict) else result.model_dump()
        ok = r.get("ok", False)
        score = float(r.get("score", -999.0))

        if ok:
            ind["exec_success"] = True
            ind["obj"] = -score  # U2E 最小化，MCTS score 越高越好 → obj 越低越好
            ind["score"] = score
            ind["output_log"] = r.get("output_log", "")
        else:
            ind["exec_success"] = False
            ind["obj"] = float("inf")
            ind["score"] = score
            ind["traceback_msg"] = r.get("error", "sandbox failed")
            ind["output_log"] = r.get("output_log", "")
        scored.append(ind)

    population = scored + list(invalid_population)
    return U2eAggregateSandboxResult(
        population=population,
        function_evals=function_evals + evals_added,
    )


def u2e_build_func_iter_tasks(
    func_tasks: list,
    func_individual: dict,
    func_elitist: dict,
    func_structured_memory: dict,
    elitist: Any,
    elite_archive: list,
    iteration: int,
    function_evals: int,
    sandbox_in_ch: str,
    pop_size: int,
    mutation_rate: float,
    user_generator_prompt: str = "",
    external_knowledge: str = "",
    problem_desc: str = "",
) -> U2eBuildFuncIterTasksResult:
    """合并 loop 状态与 func_tasks 元数据，构建每函数的完整 iter 输入。

    解决 wire 语法无法动态取 func_individual[func_name] 的问题：
    在 Python 层完成 fanout，每个函数 task dict 携带自己的 func_population 等状态。

    Args:
        func_tasks:             u2e_tree_to_func_tasks 产出的函数元数据列表
        func_individual:        {func_name: [individual, ...]} 历史累积
        func_elitist:           {func_name: individual | None} 各函数精英
        func_structured_memory: {func_name: memory_dict} 各函数结构化记忆
        elitist:                全局精英个体（dict 或 None）
        elite_archive:          当前精英档案列表
        iteration:              当前迭代号
        function_evals:         当前累计评估次数（各函数独立从 0 起，汇总后累加）
        sandbox_in_ch:          共享沙箱会话输入通道 ID
        pop_size:               种群大小
        mutation_rate:          变异比例
        user_generator_prompt:  user_generator 提示词
        external_knowledge:     外部知识字符串
        problem_desc:           问题描述

    Returns:
        U2eBuildFuncIterTasksResult: .func_iter_tasks
    """
    func_iter_tasks: list = []
    for task in func_tasks:
        fn_name = task["func_name"]
        func_population = func_individual.get(fn_name, [])
        func_elitist_for_func = func_elitist.get(fn_name) if isinstance(func_elitist, dict) else None
        structured_memory = (
            func_structured_memory.get(fn_name, {})
            if isinstance(func_structured_memory, dict) else {}
        ) or {"principles": [], "failed_patterns": [], "best_tricks": []}

        func_iter_tasks.append({
            **task,
            "func_population":       func_population,
            "func_elitist_for_func": func_elitist_for_func,
            "structured_memory":     structured_memory,
            "elitist":               elitist,
            "elite_archive":         elite_archive,
            "iteration":             iteration,
            "function_evals":        0,  # 每函数独立计数，由 collect_results 汇总
            "sandbox_in_ch":         sandbox_in_ch,
            "pop_size":              pop_size,
            "mutation_rate":         mutation_rate,
            "user_generator_prompt": user_generator_prompt,
            "external_knowledge":    external_knowledge,
            "problem_desc":          problem_desc,
        })

    return U2eBuildFuncIterTasksResult(func_iter_tasks=func_iter_tasks)


def u2e_collect_func_results(
    keyed_results: dict,
    func_tasks: list,
    function_evals: int = 0,
) -> U2eCollectFuncResultsResult:
    """从 keyed map 输出中提取 flat population 和 per-function 状态。

    keyed_results 来自 map(..., collect="keyed") 的输出，格式为
    {func_name: {population, new_structured_memory, function_evals, ...}}

    Args:
        keyed_results:  map(u2e_func_iter_patch) 的 keyed 输出
        func_tasks:     函数元数据列表（用于确保顺序一致）
        function_evals: 当前 loop 累计评估次数（基准值）

    Returns:
        U2eCollectFuncResultsResult:
            .population              所有函数本轮 individual flat 列表
            .func_structured_memory  {func_name: structured_memory}
            .function_evals          更新后的累计评估次数（基准 + 本轮各函数之和）
    """
    population: list = []
    func_structured_memory: dict = {}
    total_evals_delta = 0

    for task in func_tasks:
        fn_name = task["func_name"]
        result = keyed_results.get(fn_name, {})
        if not isinstance(result, dict):
            continue

        # 收集本函数本轮 individual
        func_pop = result.get("population", [])
        if isinstance(func_pop, list):
            population.extend(func_pop)

        # 结构化记忆（来自长期反思 agent 输出）
        mem = result.get("new_structured_memory")
        if not isinstance(mem, dict):
            mem = {"principles": [], "failed_patterns": [], "best_tricks": []}
        func_structured_memory[fn_name] = mem

        # 各函数独立从 0 计数的 function_evals，直接累加为总量
        func_evals = result.get("function_evals") or 0
        total_evals_delta += func_evals

    return U2eCollectFuncResultsResult(
        population=population,
        func_structured_memory=func_structured_memory,
        function_evals=function_evals + total_evals_delta,
    )
