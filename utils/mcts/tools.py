"""
mcts_tools — MCTS 搜索工具函数

同步工具，供 tools/*.json 配置引用（type: "tool"）。

工具列表：
    mcts_ast_check          — 对 LLM 代码输出执行 AST 语法检查，返回 MctAstCheckResult（含 action 字段）
    mcts_parse_atomic_ops   — 将 Critic 提案拆解为原子 Engineer 任务列表（rewrite/create）
    mcts_collect_patches    — 从 map(mcts_engineer_task) 结果中收集有效 patch
    mcts_create_root_node   — 创建搜索树根节点，返回初始 frontier / all_nodes
    mcts_build_tree_text    — 将搜索树状态序列化为 LLM 可读文本
    mcts_build_fanout       — 为每个 frontier 节点生成待评估任务列表（含 sandbox_in_ch）
    mcts_get_code           — 从 tree.code + ancestor_patches 计算当前目标函数代码（纯内存）
    mcts_build_children     — 将 map 结果转换为子节点并注册到 all_nodes
    mcts_update_state       — 根据 Human Gate 决策更新全部 MCTS 状态变量
    mcts_apply_best         — 将最优节点的 patch 链写回项目（保留 .bak 备份）
    close_sandbox_session   — 关闭共享沙箱会话输入通道，触发会话清理退出
"""
from __future__ import annotations

import ast
import os
import shutil
import uuid as _uuid
from typing import Any

from utils import ToolResult
from utils.text_utils import _strip_fences, _replace_code_block, _append_code_block


# ── 内部辅助 ─────────────────────────────────────────────────────────────────

def _lookup_code_from_tree(tree: list, ancestor_patches: list, target_name: str) -> str:
    """从 arch_tree fn 节点的 code 字段 + ancestor_patches 计算目标函数当前代码。

    先从 tree 中找 fn::<target_name> 节点取 code 作为基线，
    再按顺序应用 ancestor_patches 中匹配 target_name 的 patch（后者覆盖前者）。
    """
    code = ""
    for node in tree:
        if node.get("kind") in ("function", "type"):
            if node.get("id", "").split("::", 1)[-1] == target_name:
                code = node.get("code", "")
                break
    for patch in ancestor_patches:
        if patch.get("target_name") == target_name:
            code = patch["code"]
    return code


class MctAstCheckResult(ToolResult):
    ok: bool
    patch: dict | None
    error: str


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def mcts_ast_check(
    code: str,
    target_file: str,
    target_type: str,
    target_name: str,
    action: str = "rewrite",
) -> MctAstCheckResult:
    """
    对 LLM 生成的代码字符串执行 AST 语法检查，并构建完整 patch 对象。

    先去除 markdown 围栏，再用 ast.parse 验证语法。
    静态失败时返回 ok=False, patch=None（引擎直接丢弃，不计 consecutive_bad）。

    Args:
        code:        LLM 原始输出，可含 markdown 围栏。
        target_file: 相对路径，例如 src/model.py。
        target_type: "function" 或 "class"。
        target_name: 要替换的函数名或类名。
        action:      "rewrite"（替换已有节点）或 "create"（追加新节点），默认 "rewrite"。

    Returns:
        {
            "ok":    bool,          # True = 语法合法
            "patch": dict | None,   # ok=True 时为完整 patch 对象，否则 None
            "error": str,           # 语法错误信息（ok=False 时有效）
        }
    """
    clean = _strip_fences(code)
    try:
        ast.parse(clean)
        patch = {
            "action":      action,
            "target_file": target_file,
            "target_type": target_type,
            "target_name": target_name,
            "code":        clean,
        }
        return MctAstCheckResult(ok=True, patch=patch, error="")
    except SyntaxError as e:
        return MctAstCheckResult(ok=False, patch=None, error=str(e))


# ── 搜索空间常量 ──────────────────────────────────────────────────────────────

# 二元算子：作用于一条边（两个直接相连的节点）
# 多元算子：作用于子图（单入口 → 多中间节点 → 单出口）
_OPERATORS = [
    # 二元
    "insert",      # 在 A→B 之间插入新节点 C，变为 A→C→B
    "merge",       # 合并 A→B 为单节点 AB，消除边界
    "decouple",    # 断开 A→B 的直接依赖，引入抽象层或移除耦合
    "split",       # 将节点 B 拆分为 B1、B2，A 分别连接
    "extract",     # 从 A→B 提取公共组件 C，变为 A→C←B
    # 多元
    "parallelize", # 将单入单出子图的中间串行节点改为并发执行
    "pipeline",    # 将链路改为流式处理（前一步产出即送入下一步）
    "stratify",    # 重整混乱的跨层依赖为单向有序层级结构
]
_ALL_OPS: list[str] = list(_OPERATORS)

BEAM_WIDTH            = 3
CONSECUTIVE_BAD_LIMIT = 3


# ── mcts_create_root_node ─────────────────────────────────────────────────────

class MctsRootNodeResult(ToolResult):
    root_node: dict
    frontier:  list
    all_nodes: dict


def mcts_create_root_node(
    baseline_score: float,
    baseline_log: str,
) -> MctsRootNodeResult:
    """创建 MCTS 搜索树根节点，返回初始 frontier 和 all_nodes 注册表。"""
    root: dict = {
        "node_id":      "root",
        "generation":   0,
        "parent_id":    None,
        "op":           None,
        "patches":      [],
        "score":        baseline_score,
        "metrics":      {},
        "output_log":   baseline_log,
        "tried_combos": [],
    }
    return MctsRootNodeResult(
        root_node=root,
        frontier=[root],
        all_nodes={"root": root},
    )


# ── mcts_build_tree_text ──────────────────────────────────────────────────────

class MctsBuildTreeTextResult(ToolResult):
    tree_text: str


def mcts_build_tree_text(
    all_nodes: dict,
    frontier:  list,
    best_node: dict,
) -> MctsBuildTreeTextResult:
    """将搜索树当前状态序列化为适合 LLM Critic 阅读的文本。"""
    frontier_ids: set[str] = {
        n["node_id"] for n in frontier if isinstance(n, dict)
    }
    best_id: str | None = best_node.get("node_id") if best_node else None

    lines = ["搜索树状态:"]
    for node_id, node in all_nodes.items():
        if not isinstance(node, dict):
            continue
        op_str = node.get("op") or "root"
        score = node.get("score", float("-inf"))
        score_str = (
            f"{score:.4f}"
            if isinstance(score, (int, float)) and score != float("-inf")
            else "N/A"
        )
        tags: list[str] = []
        if node_id in frontier_ids:
            tags.append("frontier")
        if node_id == best_id:
            tags.append("best")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(
            f"  [{node_id}] gen={node.get('generation', 0)} "
            f"op={op_str} score={score_str}{tag_str}"
        )
    return MctsBuildTreeTextResult(tree_text="\n".join(lines))


# ── mcts_build_fanout ─────────────────────────────────────────────────────────

class MctsFanoutResult(ToolResult):
    tasks: list


def _build_node_summary(tree: list) -> str:
    """将 arch tree 的 function/class 节点提炼为 Critic 可读的摘要。

    只输出 function / type 节点的名称、文件和单行描述，
    方便 Critic 从真实存在的节点中选取 A→B 对，避免幻觉。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for node in tree:
        kind = node.get("kind")
        if kind not in ("function", "type"):
            continue
        raw_id: str = node.get("id", "")
        name = raw_id.split("::", 1)[-1] if "::" in raw_id else raw_id
        if not name or name in seen:
            continue
        seen.add(name)
        file_path = node.get("file", "")
        desc = node.get("description", "")
        label = "class" if kind == "type" else "function"
        line = f"  - {name} [{label}] {file_path}"
        if desc:
            line += f" — {desc[:80]}"
        lines.append(line)
    if not lines:
        return "(no code nodes found)"
    return "Available nodes (name [type] file):\n" + "\n".join(lines)


def mcts_build_fanout(
    frontier:        list,
    global_tried:    list,
    beam_width:      int,
    demand:          str,
    tree_text:       str,
    history_prompt:  str,
    baseline_log:    str,
    project_root:    str,
    sandbox_timeout: float,
    tree:            list,
    sandbox_in_ch:   str = "",
) -> MctsFanoutResult:
    """
    为每个 frontier 节点生成待评估任务列表。

    每个任务 = 一次 mcts_single_combo 调用所需的全部参数，
    包括 op 算子和父节点的累积 patches。
    每个节点最多展开 beam_width 个未试过的算子。

    Args:
        sandbox_in_ch: 共享沙箱会话输入通道 ID，透传给每个 mcts_single_combo 任务；
                       为空字符串时表示使用独立 sandbox_eval_once 模式（兼容旧接口）。
    """
    global_tried_set: set[str] = set(global_tried)
    node_summary = _build_node_summary(tree)
    tasks: list[dict] = []

    for node in frontier:
        if not isinstance(node, dict):
            continue
        node_tried: set[str] = set(node.get("tried_combos", []))
        all_tried = global_tried_set | node_tried
        remaining = [op for op in _ALL_OPS if op not in all_tried]

        for op in remaining[:beam_width]:
            tasks.append({
                "op":               op,
                "demand":           demand,
                "tree_text":        tree_text,
                "history_prompt":   history_prompt,
                "baseline_log":     baseline_log,
                "node_summary":     node_summary,
                "project_root":     project_root,
                "ancestor_patches": node.get("patches", []),
                "sandbox_timeout":  sandbox_timeout,
                "_parent_node_id":  node["node_id"],
                "_parent_score":    node.get("score", float("-inf")),
                "tree":             tree,
                "sandbox_in_ch":    sandbox_in_ch,
            })

    return MctsFanoutResult(tasks=tasks)


# ── mcts_build_children ───────────────────────────────────────────────────────

class MctsBuildChildrenResult(ToolResult):
    new_nodes: list
    all_nodes: dict
    best_node: dict
    improved:  bool


def mcts_build_children(
    combo_results: list,
    all_nodes:     dict,
    generation:    int,
    best_node:     dict,
) -> MctsBuildChildrenResult:
    """
    将 map 步骤返回的 mcts_single_combo 结果列表转换为子节点 dict，
    注册到 all_nodes，并跟踪当前最优节点。

    只处理沙盒成功（result.ok=True）的结果，忽略静态/运行时失败项。
    """
    new_nodes: list[dict] = []
    best: dict | None     = dict(best_node) if best_node else None
    all_nodes             = dict(all_nodes)   # shallow copy

    for result in combo_results:
        if not isinstance(result, dict):
            continue
        sandbox_result = result.get("result", {})
        if not isinstance(sandbox_result, dict) or not sandbox_result.get("ok", False):
            continue

        op             = result.get("op", "")
        score          = sandbox_result.get("score", -999.0)
        all_patches    = sandbox_result.get("all_patches", [])
        parent_node_id = result.get("_parent_node_id", "root")
        parent_node    = all_nodes.get(parent_node_id) or all_nodes.get("root") or {}
        parent_tried   = list(parent_node.get("tried_combos", []))

        node_id = str(_uuid.uuid4())[:8]
        child: dict = {
            "node_id":      node_id,
            "generation":   generation,
            "parent_id":    parent_node_id,
            "op":           op,
            "patches":      all_patches,
            "score":        score,
            "metrics":      sandbox_result.get("metrics", {}),
            "output_log":   sandbox_result.get("output_log", ""),
            "tried_combos": parent_tried + [op],
        }
        all_nodes[node_id] = child
        new_nodes.append(child)

        if best is None or score > best.get("score", float("-inf")):
            best = child

    baseline = best_node.get("score", float("-inf")) if best_node else float("-inf")
    improved  = any(n["score"] > baseline for n in new_nodes)

    return MctsBuildChildrenResult(
        new_nodes=new_nodes,
        all_nodes=all_nodes,
        best_node=best if best is not None else (best_node or {}),
        improved=improved,
    )


# ── mcts_update_state ─────────────────────────────────────────────────────────

class MctsUpdateStateResult(ToolResult):
    frontier:         list
    all_nodes:        dict
    best_node:        dict
    consecutive_bad:  int
    generation:       int
    history:          list
    global_tried:     list
    should_continue:  bool
    history_prompt:   str
    baseline_log:     str


def mcts_update_state(
    action:                str,
    next_mode:             str,
    selected_ids:          list,
    new_nodes:             list,
    all_nodes:             dict,
    frontier:              list,
    best_node:             dict,
    consecutive_bad:       int,
    generation:            int,
    history:               list,
    global_tried:          list,
    baseline_log:          str,
    beam_width:            int,
    consecutive_bad_limit: int,
) -> MctsUpdateStateResult:
    """
    根据 Human Gate 决策更新全部 MCTS 状态变量。

    支持的 action：
      stop / architecture → should_continue=False，终止循环
      rollback            → 回退到父节点层作为新 frontier
      continue / select   → 从改进节点中选 top-K 作为新 frontier
    """
    history      = list(history)
    global_tried = list(global_tried)

    # 记录本代历史
    if new_nodes:
        best_new = max(new_nodes, key=lambda n: n.get("score", float("-inf")))
        history.append({
            "generation": generation,
            "score":      best_new.get("score", -999),
            "op":         best_new.get("op"),
            "status":     action,
        })

    # ── stop / architecture ────────────────────────────────────────────
    if action in ("stop", "architecture"):
        return MctsUpdateStateResult(
            frontier=frontier,
            all_nodes=all_nodes,
            best_node=best_node,
            consecutive_bad=consecutive_bad,
            generation=generation + 1,
            history=history,
            global_tried=global_tried,
            should_continue=False,
            history_prompt=_build_history_prompt(history),
            baseline_log=baseline_log,
        )

    # ── rollback ───────────────────────────────────────────────────────
    if action == "rollback":
        new_frontier = _do_rollback(frontier, all_nodes)
        if not new_frontier:
            return MctsUpdateStateResult(
                frontier=frontier,
                all_nodes=all_nodes,
                best_node=best_node,
                consecutive_bad=0,
                generation=generation + 1,
                history=history,
                global_tried=global_tried,
                should_continue=False,
                history_prompt=_build_history_prompt(history),
                baseline_log=baseline_log,
            )
        return MctsUpdateStateResult(
            frontier=new_frontier,
            all_nodes=all_nodes,
            best_node=best_node,
            consecutive_bad=0,
            generation=generation + 1,
            history=history,
            global_tried=global_tried,
            should_continue=True,
            history_prompt=_build_history_prompt(history),
            baseline_log=baseline_log,
        )

    # ── continue / select ──────────────────────────────────────────────
    baseline = best_node.get("score", float("-inf"))
    improved  = [n for n in new_nodes if n.get("score", -999) > baseline]

    if improved:
        sel_pool = (
            [n for n in improved if n["node_id"] in selected_ids] or improved
            if selected_ids else improved
        )
        new_frontier    = sorted(sel_pool, key=lambda n: -n.get("score", -999))[:beam_width]
        new_consecutive = 0

        for n in new_frontier:
            op = n.get("op")
            if op and op not in global_tried:
                global_tried.append(op)

        top = new_frontier[0]
        if top.get("score", -999) > best_node.get("score", float("-inf")):
            best_node    = top
            baseline_log = top.get("output_log", baseline_log)
    else:
        new_frontier    = frontier
        new_consecutive = consecutive_bad + 1

        if new_consecutive >= consecutive_bad_limit:
            rollback_frontier = _do_rollback(frontier, all_nodes)
            new_consecutive   = 0
            if not rollback_frontier:
                return MctsUpdateStateResult(
                    frontier=frontier,
                    all_nodes=all_nodes,
                    best_node=best_node,
                    consecutive_bad=new_consecutive,
                    generation=generation + 1,
                    history=history,
                    global_tried=global_tried,
                    should_continue=False,
                    history_prompt=_build_history_prompt(history),
                    baseline_log=baseline_log,
                )
            new_frontier = rollback_frontier

    return MctsUpdateStateResult(
        frontier=new_frontier,
        all_nodes=all_nodes,
        best_node=best_node,
        consecutive_bad=new_consecutive,
        generation=generation + 1,
        history=history,
        global_tried=global_tried,
        should_continue=True,
        history_prompt=_build_history_prompt(history),
        baseline_log=baseline_log,
    )


def _do_rollback(frontier: list, all_nodes: dict) -> list:
    """将 frontier 回滚到父节点层（去重）。"""
    parents: list[dict] = []
    seen: set[str] = set()
    for node in frontier:
        parent_id = node.get("parent_id")
        if parent_id and parent_id not in seen:
            parent = all_nodes.get(parent_id)
            if parent:
                parents.append(parent)
                seen.add(parent_id)
    return parents


def _build_history_prompt(history: list) -> str:
    """将最近 10 代历史记录格式化为 Critic 提示词片段。"""
    if not history:
        return ""
    lines = ["历史搜索记录（最近各代）:"]
    for rec in history[-10:]:
        lines.append(
            f"  Gen {rec.get('generation')} | "
            f"op={rec.get('op')} "
            f"score={rec.get('score', 'N/A')} status={rec.get('status')}"
        )
    return "\n".join(lines)


# ── mcts_apply_best ───────────────────────────────────────────────────────────

class MctsGetCodeResult(ToolResult):
    original_code: str


def mcts_get_code(
    tree:             list,
    ancestor_patches: list,
    target_name:      str,
) -> MctsGetCodeResult:
    """从 arch_tree fn 节点的 code 字段 + ancestor_patches 计算目标函数当前代码。

    纯内存操作，无磁盘读取。

    Args:
        tree:             架构树（含 code 字段的 fn:: 节点列表）。
        ancestor_patches: 父节点累积 patch 链，每项 {target_name, code, ...}。
        target_name:      目标函数名或类名。
    """
    return MctsGetCodeResult(
        original_code=_lookup_code_from_tree(tree, ancestor_patches, target_name)
    )


class MctsApplyBestResult(ToolResult):
    ok:              bool
    error:           str
    applied_patches: int


def mcts_apply_best(
    best_node:    dict | None,
    project_root: str,
) -> MctsApplyBestResult:
    """
    将最优节点的累积 patch 链按顺序写回到项目文件。

    写入前保留 .bak 备份；任一 patch 注入失败则整体返回 ok=False。
    action="create" 的 patch 使用 _append_code_block 追加；
    action="rewrite"（默认）使用 _replace_code_block 替换。
    """
    if best_node is None:
        return MctsApplyBestResult(ok=True, error="", applied_patches=0)
    patches = best_node.get("patches", [])
    if not patches:
        return MctsApplyBestResult(ok=True, error="", applied_patches=0)

    for patch in patches:
        rel    = patch["target_file"].lstrip("/").replace("\\", "/")
        path   = os.path.join(project_root, rel)
        action = patch.get("action", "rewrite")
        if os.path.exists(path):
            shutil.copy(path, path + ".bak")
        if action == "create":
            ok, msg = _append_code_block(path, patch["code"])
        else:
            ok, msg = _replace_code_block(
                path,
                patch["target_type"],
                patch["target_name"],
                patch["code"],
            )
        if not ok:
            return MctsApplyBestResult(
                ok=False,
                error=f"inject failed on {rel}: {msg}",
                applied_patches=0,
            )

    return MctsApplyBestResult(ok=True, error="", applied_patches=len(patches))


# ── mcts_parse_atomic_ops ─────────────────────────────────────────────────────

class MctsParseAtomicOpsResult(ToolResult):
    engineer_tasks: list   # list[dict] 每项为一个 engineer 任务


def mcts_parse_atomic_ops(
    op:               str,
    node_a_file:      str,
    node_a_type:      str,
    node_a_name:      str,
    node_b_file:      str,
    node_b_type:      str,
    node_b_name:      str,
    direction:        str,
    reasoning:        str,
    new_nodes:        list,
    tree:             list,
    ancestor_patches: list,
) -> MctsParseAtomicOpsResult:
    """
    将 Critic 的结构优化提案拆解为原子级 Engineer 任务列表。

    每个任务对应一次 Engineer 调用，包含：
      action       — "rewrite" 或 "create"
      target_file  — 目标文件相对路径
      target_type  — "function" 或 "class"
      target_name  — 目标节点名
      original_code — 当前代码（来自 tree + ancestor_patches）
      direction    — 改造方向
      reasoning    — 改造理由

    逻辑：
    - node_a 始终生成一个 "rewrite" 任务（主改造目标）
    - insert / split / extract 算子的 new_nodes 生成 "create" 任务（在 node_b_file 中追加）
    - merge / decouple / parallelize / pipeline / stratify 仅改写 node_a
    """
    # 构建 name → 真实文件路径映射，同时校验 node_a 是否在 tree 中真实存在
    # LLM 经常幻构 node_a_file 路径（如 "path/to/harmony.py"），
    # 此处从 tree 查找 fn 节点的实际 file 节点路径，强制使用真实路径
    existing_names: set[str] = set()
    name_to_real_file: dict[str, str] = {}  # fn_name → real relative path
    # 先建 file_id → path 映射
    file_id_to_path: dict[str, str] = {}
    for node in tree:
        if node.get("kind") == "file":
            file_id_to_path[node.get("id", "")] = node.get("path", "")
    for node in tree:
        if node.get("kind") in ("function", "type"):
            raw_id = node.get("id", "")
            name = raw_id.split("::", 1)[-1] if "::" in raw_id else raw_id
            if name:
                existing_names.add(name)
                # 从 fn 节点的 file 字段查找真实路径
                fn_file_id = node.get("file", "")
                real_path = file_id_to_path.get(fn_file_id, "")
                if real_path:
                    name_to_real_file[name] = real_path

    if node_a_name not in existing_names:
        # 节点不存在，返回空任务列表以跳过本次 combo
        return MctsParseAtomicOpsResult(engineer_tasks=[])

    # 优先使用 tree 中查到的真实路径，否则退回到 LLM 提供的路径
    real_node_a_file = name_to_real_file.get(node_a_name) or node_a_file
    real_node_b_file = name_to_real_file.get(node_b_name) or node_b_file

    tasks: list[dict] = []

    # node_a: 主改造目标，始终 rewrite
    tasks.append({
        "action":        "rewrite",
        "target_file":   real_node_a_file,
        "target_type":   node_a_type,
        "target_name":   node_a_name,
        "original_code": _lookup_code_from_tree(tree, ancestor_patches, node_a_name),
        "direction":     direction,
        "reasoning":     reasoning,
    })

    # new_nodes: insert / split / extract 所需的新节点，追加到 node_b_file
    for nn in (new_nodes or []):
        name = nn.get("name", "")
        desc = nn.get("description", "")
        if not name:
            continue
        tasks.append({
            "action":        "create",
            "target_file":   real_node_b_file,
            "target_type":   "function",
            "target_name":   name,
            "original_code": "",
            "direction":     desc,
            "reasoning":     reasoning,
        })

    return MctsParseAtomicOpsResult(engineer_tasks=tasks)


# ── mcts_collect_patches ──────────────────────────────────────────────────────

class MctsCollectPatchesResult(ToolResult):
    patches:  list   # 通过 AST 检查的 patch 列表
    all_ok:   bool   # 所有任务均通过时为 True
    failures: list   # 未通过的任务名列表


def mcts_collect_patches(
    task_results: list,
) -> MctsCollectPatchesResult:
    """
    从 map(mcts_engineer_task) 的结果列表中收集有效 patch。

    每项结果格式：{"ok": bool, "patch": dict | None, "error": str}
    仅收集 ok=True 且 patch 非空的项；其余记录为 failures。
    """
    patches:  list[dict] = []
    failures: list[str]  = []

    for item in (task_results or []):
        if not isinstance(item, dict):
            failures.append("<invalid>")
            continue
        if item.get("ok") and item.get("patch"):
            patches.append(item["patch"])
        else:
            name = (item.get("patch") or {}).get("target_name") or item.get("error", "<unknown>")
            failures.append(str(name))

    return MctsCollectPatchesResult(
        patches=patches,
        all_ok=len(failures) == 0,
        failures=failures,
    )


# ── close_sandbox_session ──────────────────────────────────────────────────────

class CloseSandboxResult(ToolResult):
    ok: bool


def close_sandbox_session(sandbox_in_ch: str) -> CloseSandboxResult:
    """关闭共享沙箱会话输入通道，触发会话清理退出。

    向 sandbox_in_ch 发送 EOF 信号（close_channel），后台运行的
    sandbox_session_concurrent 将在处理完所有飞行中请求后自行清理并退出。
    若通道不存在（已关闭或从未创建），静默返回 ok=True。

    Args:
        sandbox_in_ch: 共享沙箱会话输入通道 ID（由 start_sandbox_session 创建）。
    """
    from utils import close_channel, channel_exists
    if channel_exists(sandbox_in_ch):
        close_channel(sandbox_in_ch)
    return CloseSandboxResult(ok=True)
