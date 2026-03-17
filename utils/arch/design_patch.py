"""arch/design_patch.py — 设计级 patch 工具函数

提供工具函数：
    arch_apply_design_patches      — 将设计级 patch 链应用到架构树（增删改节点）
    arch_augment_get_next_item     — loop 辅助：从 remaining_items 弹出第一个计划项
    arch_augment_update_patches    — loop 辅助：将新生成的 patch 追加到 patches_so_far
    arch_pick_tree                 — 从重建树或原始树中选择最终设计树
    arch_combine_feedback          — 将需求和人类反馈合并为增强需求描述
"""
from __future__ import annotations

from copy import deepcopy

from utils import ToolResult


# ── 返回类型 ───────────────────────────────────────────────────────────────────

class ArchApplyDesignPatchesResult(ToolResult):
    tree: list          # 更新后的架构树（deepcopy）
    apply_report: list  # 每个 patch 的执行报告 [{index, op, target_id, status, detail}]


class ArchAugmentGetNextItemResult(ToolResult):
    current_item: dict   # 当前计划项
    remaining_items: list  # 弹出后剩余计划项
    has_remaining: bool  # 剩余是否非空（loop while 条件）


class ArchAugmentUpdatePatchesResult(ToolResult):
    patches_so_far: list   # 追加后的完整 patch 列表
    remaining_items: list  # 透传 remaining_items 供 state_update 引用
    has_remaining: bool    # 还有未处理的计划项（loop while 条件）


class ArchPickTreeResult(ToolResult):
    tree: list  # 最终选定的架构树


class ArchCombineFeedbackResult(ToolResult):
    enriched_requirement: str  # 合并后的增强需求描述


# ── arch_augment_get_next_item ────────────────────────────────────────────────

def arch_augment_get_next_item(
    remaining_items: list,
) -> ArchAugmentGetNextItemResult:
    """loop 辅助：从 remaining_items 弹出第一个计划项，返回剩余列表。

    Args:
        remaining_items: 尚未处理的计划项列表（由 arch_augment_plan 输出）。

    Returns:
        current_item:    本轮要处理的计划项（dict）。
        remaining_items: 弹出后的剩余列表。
        has_remaining:   剩余列表是否非空（供 loop while 条件判断）。
    """
    if not remaining_items:
        return ArchAugmentGetNextItemResult(
            current_item={}, remaining_items=[], has_remaining=False
        )
    return ArchAugmentGetNextItemResult(
        current_item=remaining_items[0],
        remaining_items=remaining_items[1:],
        has_remaining=len(remaining_items) > 1,
    )


# ── arch_augment_update_patches ───────────────────────────────────────────────

def arch_augment_update_patches(
    patches_so_far: list,
    new_patch: dict,
    remaining_items: list,
) -> ArchAugmentUpdatePatchesResult:
    """loop 辅助：将新生成的 patch 追加到 patches_so_far，更新剩余计划项数量。

    Args:
        patches_so_far:  本轮之前已生成的 patch 列表。
        new_patch:       本轮 arch_augment_gen_patch 生成的单个 patch dict。
        remaining_items: 弹出当前项后的剩余计划项（由 arch_augment_get_next_item 返回）。

    Returns:
        patches_so_far: 追加 new_patch 后的完整列表。
        has_remaining:  剩余计划项是否非空。
    """
    return ArchAugmentUpdatePatchesResult(
        patches_so_far=list(patches_so_far) + [new_patch],
        remaining_items=remaining_items,
        has_remaining=bool(remaining_items),
    )


# ── 内部辅助 ───────────────────────────────────────────────────────────────────

def _build_index(tree: list) -> dict[str, dict]:
    """构建 id → 节点引用的查找表（就地引用，可直接修改节点）。"""
    return {n["id"]: n for n in tree if n.get("id")}


# ── arch_apply_design_patches ──────────────────────────────────────────────────

def arch_apply_design_patches(
    tree: list,
    patch_chain: list,
) -> ArchApplyDesignPatchesResult:
    """将设计级 patch 链（由 arch_feedback_classifier 生成）顺序应用到架构树。

    支持的 op：
        add_fn             — 在指定 file 中新增函数节点
        remove_fn          — 删除函数节点及所有 calls 引用
        modify_fn          — 修改函数节点字段（description / params / returns / is_async / calls）
        add_file           — 在指定 module 中新增文件节点
        remove_file        — 删除文件节点及其所有子函数/子类型
        modify_file        — 修改文件节点字段（description / imports）
        add_module         — 新增模块节点
        remove_module      — 删除模块节点及其所有子文件/子函数/子类型
        add_fn_call        — 在指定函数的 calls 中添加调用引用
        remove_fn_call     — 从指定函数的 calls 中移除调用引用
        add_file_import    — 在指定文件的 imports 中添加引用
        remove_file_import — 从指定文件的 imports 中移除引用

    patch_chain 每项格式：
        {
            "op":          str,        # 操作类型
            "target_id":   str,        # 主要目标节点 id
            "target_file": str,        # 可选，add_fn 时指定所属 file id
            "depends_on":  list[int],  # 前置 patch 索引（文档用，当前顺序执行不强制等待）
            "data":        dict        # op 相关附加数据
        }

    Args:
        tree:        原始架构树（list[dict]，与 arch_build 输出格式一致）。
        patch_chain: arch_feedback_classifier 产出的 patch 列表。

    Returns:
        tree:         更新后的架构树（deepcopy，不修改原树）。
        apply_report: 每个 patch 对应一项 {index, op, target_id, status, detail}。
    """
    updated: list[dict] = deepcopy(tree)
    report: list[dict] = []

    for i, patch in enumerate(patch_chain):
        op          = patch.get("op", "")
        target_id   = patch.get("target_id", "")
        target_file = patch.get("target_file", "")
        data        = patch.get("data") or {}

        entry: dict = {
            "index": i, "op": op, "target_id": target_id,
            "status": "ok", "detail": "",
        }

        # 每次 patch 后重建索引，以反映上一步的增删
        idx = _build_index(updated)

        try:
            # ── add_fn ──────────────────────────────────────────────────────
            if op == "add_fn":
                file_id = data.get("file_id") or target_file
                fn_id   = target_id
                if fn_id in idx:
                    entry.update(status="skip", detail=f"{fn_id} 已存在")
                else:
                    new_fn: dict = {
                        "id":          fn_id,
                        "kind":        "function",
                        "description": data.get("description", ""),
                        "file":        file_id,
                        "params":      data.get("params", []),
                        "returns":     data.get("returns", {"type": "None"}),
                        "calls":       data.get("calls", []),
                        "is_async":    data.get("is_async", False),
                    }
                    if data.get("code"):
                        new_fn["code"] = data["code"]
                    updated.append(new_fn)
                    # 优先按 id 查找文件节点；若 file_id 是路径（如 "metrics.py"），
                    # 则回退到按 path 字段匹配，兼容 LLM 输出路径而非节点 id 的情况。
                    file_node = idx.get(file_id)
                    if file_node is None:
                        for node in updated:
                            if node.get("kind") == "file" and node.get("path") == file_id:
                                file_node = node
                                new_fn["file"] = node.get("id", file_id)  # 修正 file 字段为真实节点 id
                                break
                    if file_node is not None:
                        fns: list = list(file_node.get("functions", []))
                        if fn_id not in fns:
                            fns.append(fn_id)
                        file_node["functions"] = fns
                    else:
                        entry["detail"] = f"file {file_id} 不存在，fn 已加入但无父节点"

            # ── remove_fn ───────────────────────────────────────────────────
            elif op == "remove_fn":
                fn_node = idx.get(target_id)
                if fn_node is None:
                    entry.update(status="skip", detail=f"{target_id} 不存在")
                else:
                    file_id   = fn_node.get("file", "")
                    file_node = idx.get(file_id)
                    if file_node is not None:
                        file_node["functions"] = [
                            f for f in file_node.get("functions", []) if f != target_id
                        ]
                    # 清除所有其他 fn 节点的 calls 引用
                    for node in updated:
                        if node.get("kind") == "function" and node.get("id") != target_id:
                            node["calls"] = [c for c in node.get("calls", []) if c != target_id]
                    updated = [n for n in updated if n.get("id") != target_id]

            # ── modify_fn ───────────────────────────────────────────────────
            elif op == "modify_fn":
                fn_node = idx.get(target_id)
                if fn_node is None:
                    entry.update(status="skip", detail=f"{target_id} 不存在")
                else:
                    # 规格变更字段：任意一个变化都意味着旧实现已过期，清除 code 让 arch_to_project 重新生成
                    SPEC_FIELDS = ("description", "params", "returns", "is_async")
                    spec_changed = any(
                        field in data and data[field] != fn_node.get(field)
                        for field in SPEC_FIELDS
                    )
                    for field in ("description", "params", "returns", "is_async", "calls"):
                        if field in data:
                            fn_node[field] = data[field]
                    if spec_changed:
                        fn_node.pop("code", None)  # 清除旧实现，触发 arch_to_project 重新 LLM 生成

            # ── add_file ────────────────────────────────────────────────────
            elif op == "add_file":
                file_id = target_id
                mod_id  = data.get("module_id", "")
                if file_id in idx:
                    entry.update(status="skip", detail=f"{file_id} 已存在")
                else:
                    new_file: dict = {
                        "id":          file_id,
                        "kind":        "file",
                        "path":        data.get("path", ""),
                        "description": data.get("description", ""),
                        "module":      mod_id,
                        "imports":     data.get("imports", []),
                        "exports":     data.get("exports", []),
                        "functions":   data.get("functions", []),
                        "types":       data.get("types", []),
                        "root":        data.get("root", False),
                    }
                    updated.append(new_file)
                    mod_node = idx.get(mod_id)
                    if mod_node is not None:
                        files: list = list(mod_node.get("files", []))
                        if file_id not in files:
                            files.append(file_id)
                        mod_node["files"] = files

            # ── remove_file ─────────────────────────────────────────────────
            elif op == "remove_file":
                file_node = idx.get(target_id)
                if file_node is None:
                    entry.update(status="skip", detail=f"{target_id} 不存在")
                else:
                    fn_ids   = set(file_node.get("functions", []))
                    type_ids = set(file_node.get("types", []))
                    mod_id   = file_node.get("module", "")
                    mod_node = idx.get(mod_id)
                    if mod_node is not None:
                        mod_node["files"] = [f for f in mod_node.get("files", []) if f != target_id]
                    # 从其他文件的 imports 中移除对被删文件的引用
                    for node in updated:
                        if node.get("kind") == "file" and node.get("id") != target_id:
                            node["imports"] = [
                                imp for imp in node.get("imports", [])
                                if not (isinstance(imp, dict) and imp.get("from") == target_id)
                            ]
                    # 清除其他 fn 对被删函数的 calls 引用
                    for node in updated:
                        if node.get("kind") == "function" and node.get("id") not in fn_ids:
                            node["calls"] = [c for c in node.get("calls", []) if c not in fn_ids]
                    remove_ids = {target_id} | fn_ids | type_ids
                    updated = [n for n in updated if n.get("id") not in remove_ids]

            # ── modify_file ─────────────────────────────────────────────────
            elif op == "modify_file":
                file_node = idx.get(target_id)
                if file_node is None:
                    entry.update(status="skip", detail=f"{target_id} 不存在")
                else:
                    for field in ("description", "imports"):
                        if field in data:
                            file_node[field] = data[field]

            # ── add_module ──────────────────────────────────────────────────
            elif op == "add_module":
                mod_id = target_id
                if mod_id in idx:
                    entry.update(status="skip", detail=f"{mod_id} 已存在")
                else:
                    new_mod: dict = {
                        "id":          mod_id,
                        "kind":        "module",
                        "description": data.get("description", ""),
                        "root":        data.get("root", False),
                        "imports":     data.get("imports", []),
                        "exports":     data.get("exports", []),
                        "files":       data.get("files", []),
                    }
                    updated.append(new_mod)

            # ── remove_module ───────────────────────────────────────────────
            elif op == "remove_module":
                mod_node = idx.get(target_id)
                if mod_node is None:
                    entry.update(status="skip", detail=f"{target_id} 不存在")
                else:
                    file_ids: set[str] = set(mod_node.get("files", []))
                    fn_ids_mod: set[str] = set()
                    type_ids_mod: set[str] = set()
                    for fid in file_ids:
                        f = idx.get(fid)
                        if f:
                            fn_ids_mod.update(f.get("functions", []))
                            type_ids_mod.update(f.get("types", []))
                    remove_ids = {target_id} | file_ids | fn_ids_mod | type_ids_mod
                    # 清除 calls 引用
                    for node in updated:
                        if node.get("kind") == "function" and node.get("id") not in fn_ids_mod:
                            node["calls"] = [c for c in node.get("calls", []) if c not in fn_ids_mod]
                    updated = [n for n in updated if n.get("id") not in remove_ids]

            # ── add_fn_call ─────────────────────────────────────────────────
            elif op == "add_fn_call":
                from_id = data.get("from_fn_id") or target_id
                to_id   = data.get("to_fn_id", "")
                fn_node = idx.get(from_id)
                if fn_node is None:
                    entry.update(status="skip", detail=f"{from_id} 不存在")
                else:
                    calls: list = list(fn_node.get("calls", []))
                    if to_id not in calls:
                        calls.append(to_id)
                    fn_node["calls"] = calls

            # ── remove_fn_call ──────────────────────────────────────────────
            elif op == "remove_fn_call":
                from_id = data.get("from_fn_id") or target_id
                to_id   = data.get("to_fn_id", "")
                fn_node = idx.get(from_id)
                if fn_node is None:
                    entry.update(status="skip", detail=f"{from_id} 不存在")
                else:
                    fn_node["calls"] = [c for c in fn_node.get("calls", []) if c != to_id]

            # ── add_file_import ─────────────────────────────────────────────
            elif op == "add_file_import":
                file_id    = data.get("file_id") or target_id
                imp_from   = data.get("import_from") or data.get("import_id", "")
                imp_names  = data.get("import_names") or data.get("names", [])
                file_node  = idx.get(file_id)
                if file_node is None:
                    entry.update(status="skip", detail=f"{file_id} 不存在")
                elif not imp_from:
                    entry.update(status="skip", detail="import_from 为空")
                else:
                    imports: list = list(file_node.get("imports", []))
                    # 检查是否已存在同 from 的 import dict
                    existing = next(
                        (d for d in imports if isinstance(d, dict) and d.get("from") == imp_from),
                        None,
                    )
                    if existing is not None:
                        # 合并 names（去重）
                        cur = list(existing.get("names", []))
                        for n in imp_names:
                            if n not in cur:
                                cur.append(n)
                        existing["names"] = cur
                    else:
                        imports.append({"from": imp_from, "names": imp_names})
                    file_node["imports"] = imports

            # ── remove_file_import ──────────────────────────────────────────
            elif op == "remove_file_import":
                file_id    = data.get("file_id") or target_id
                imp_from   = data.get("import_from") or data.get("import_id", "")
                imp_names  = data.get("import_names") or data.get("names", [])
                file_node  = idx.get(file_id)
                if file_node is None:
                    entry.update(status="skip", detail=f"{file_id} 不存在")
                elif not imp_from:
                    entry.update(status="skip", detail="import_from 为空")
                else:
                    new_imports: list = []
                    for imp in file_node.get("imports", []):
                        if not isinstance(imp, dict) or imp.get("from") != imp_from:
                            new_imports.append(imp)
                            continue
                        if imp_names:
                            # 只移除指定的 names，保留其余
                            remaining = [n for n in imp.get("names", []) if n not in imp_names]
                            if remaining:
                                new_imports.append({"from": imp_from, "names": remaining})
                            # else: 全部移除，不添加
                        # else: imp_names 为空表示移除整个 from 条目
                    file_node["imports"] = new_imports

            else:
                entry.update(status="unknown_op", detail=f"op '{op}' 不支持")

        except Exception as exc:  # noqa: BLE001
            entry.update(status="error", detail=str(exc))

        report.append(entry)

    # ── 后处理：修复 fn::main 的 calls 字段 ────────────────────────────────────
    # LLM 生成的 add_fn/modify_fn patch 经常忘记在 fn::main 的 calls 中列出被调用函数，
    # 导致 arch_prepare_codegen_vars 传给 codegen LLM 的 calls_context_json 为空，
    # 进而使 codegen LLM 凭空捏造硬编码数值。
    # 此处程序化修复：对所有 fn::main 节点，若 calls 为空，自动填入同树其他所有函数 ID。
    all_fn_ids = [
        n["id"] for n in updated
        if n.get("kind") == "function"
    ]
    for node in updated:
        if node.get("kind") != "function":
            continue
        fn_id_str: str = node.get("id", "")
        # 匹配 fn::main::main 或 fn::*::main 或 fn::main 等形式
        fn_name = fn_id_str.split("::")[-1] if "::" in fn_id_str else fn_id_str
        if fn_name == "main" and not node.get("calls"):
            # 填入所有非 main 的函数 ID
            node["calls"] = [fid for fid in all_fn_ids if fid != fn_id_str]
            fix_entry = {
                "index": -1, "op": "auto_fix_main_calls",
                "target_id": fn_id_str, "status": "ok",
                "detail": f"auto-populated calls with {len(node['calls'])} fn ids",
            }
            report.append(fix_entry)

    return ArchApplyDesignPatchesResult(tree=updated, apply_report=report)


# ── arch_pick_tree ─────────────────────────────────────────────────────────────

def arch_pick_tree(
    original_tree: list,
    rebuilt_tree: list | None = None,
    feedback: str = "",
) -> ArchPickTreeResult:
    """从重建树或原始树中选择最终设计树。

    规则：feedback 非空且 rebuilt_tree 非空 → 使用 rebuilt_tree；否则使用 original_tree。

    Args:
        original_tree: arch_step 产出的设计树。
        rebuilt_tree:  arch_refine 重建后的树（无反馈时为 None）。
        feedback:      人类反馈文本（空串表示无反馈）。

    Returns:
        tree: 最终选定的架构树。
    """
    if feedback and rebuilt_tree:
        return ArchPickTreeResult(tree=rebuilt_tree)
    return ArchPickTreeResult(tree=original_tree)


# ── arch_combine_feedback ──────────────────────────────────────────────────────

def arch_combine_feedback(
    requirement: str,
    feedback: str,
    metric_name: str = "",
    direction: str = "",
) -> ArchCombineFeedbackResult:
    """将原始需求和人类反馈合并为增强需求描述，供 arch_build 重新生成架构。

    Args:
        requirement:  原始需求描述。
        feedback:     人类反馈文本。
        metric_name:  评估指标名（非空时嵌入增强需求以锁定指标）。
        direction:    优化方向（"max" 或 "min"），与 metric_name 配合使用。

    Returns:
        enriched_requirement: 合并后的增强需求描述。
    """
    parts: list[str] = [requirement.strip()]

    if feedback.strip():
        parts.append(
            f"\n## 架构调整意见（人类审核反馈，必须严格遵守）\n{feedback.strip()}"
        )

    if metric_name:
        dir_desc = (
            "越大越好（maximize）"
            if direction in ("max", "maximize")
            else "越小越好（minimize）"
        )
        parts.append(
            f"\n## 评估指标约束（必须保留，不得修改）\n"
            f"- 指标名：{metric_name}\n"
            f"- 优化方向：{direction}（{dir_desc}）"
        )

    return ArchCombineFeedbackResult(enriched_requirement="\n".join(parts))
