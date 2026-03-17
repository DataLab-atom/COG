"""
Open Agents 本地服务器

启动方式：
    python serve.py
    # 自动在浏览器打开 http://localhost:8765
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from utils import set_run_id, setup_logging, safe_serialize

setup_logging(
    level=settings.get("LOG_LEVEL", "INFO"),
    log_dir=settings.get("LOG_DIR", None) or str(Path(__file__).parent / "logs"),
    json_output=settings.get("LOG_JSON", False),
)
logger = logging.getLogger(__name__)

# 确保项目根目录在 Python 路径中
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

RUN_CONFIGS_DIR = ROOT / "run_configs"
RUN_CONFIGS_DIR.mkdir(exist_ok=True)

from utils import create_channel, send_to_channel, close_channel
from utils import run_graph_from_file

app = FastAPI(title="Open Agents")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 运行管理 ──────────────────────────────────────────────────────────────────

# 每个运行最多缓存的历史事件数（防止长时间运行导致内存膨胀）
_MAX_EVENT_HISTORY = 2000

# 最后一个 WebSocket 断开后，等待多少秒再自动 abort（给前端重连的窗口）
_ORPHAN_TIMEOUT_SECONDS = 30

RUNS: dict[str, dict] = {}


def _new_run(run_id: str, config: dict) -> dict:
    """创建一条运行记录（普通 dict）。"""
    return {
        "run_id":             run_id,
        "config":             config,
        "task":               None,           # asyncio.Task
        "subscribers":        [],             # list[asyncio.Queue]
        "arch_gate_ch":       f"arch_gate_{run_id}",
        "mcts_gate_ch":       f"mcts_gate_{run_id}",
        "auto_approve_arch":  config.get("auto_approve_arch", False),
        "auto_approve_mcts":  config.get("auto_approve_mcts", False),
        "event_history":      [],             # list[str]  WebSocket 重连回放
        "orphan_timer":       None,           # asyncio.Task | None
        "finished":           False,
    }


async def _publish(record: dict, event: dict):
    """向所有 subscriber 推送事件，同时追加到历史缓冲区。"""
    data = json.dumps(event, ensure_ascii=False)
    history = record["event_history"]
    history.append(data)
    if len(history) > _MAX_EVENT_HISTORY:
        record["event_history"] = history[-_MAX_EVENT_HISTORY:]
    for q in list(record["subscribers"]):
        await q.put(data)


def _cancel_orphan_timer(record: dict):
    """有新 subscriber 连接时，取消孤儿倒计时。"""
    timer = record.get("orphan_timer")
    if timer and not timer.done():
        timer.cancel()
        record["orphan_timer"] = None


def _start_orphan_timer(record: dict):
    """最后一个 subscriber 断开时，启动孤儿倒计时。"""
    if record["finished"] or record["subscribers"]:
        return
    record["orphan_timer"] = asyncio.create_task(_orphan_countdown(record))


async def _orphan_countdown(record: dict):
    """等待 _ORPHAN_TIMEOUT_SECONDS 秒，若仍无 subscriber 则自动 abort。"""
    try:
        await asyncio.sleep(_ORPHAN_TIMEOUT_SECONDS)
        if not record["subscribers"] and not record["finished"]:
            logger.warning(
                "Run %s 已 %ds 无客户端连接，自动中止",
                record["run_id"], _ORPHAN_TIMEOUT_SECONDS,
            )
            await _cleanup_run(record)
    except asyncio.CancelledError:
        pass


async def _cleanup_run(record: dict):
    """统一清理逻辑：取消 task、关闭 channel、标记完成。"""
    if record["finished"]:
        return
    record["finished"] = True
    _cancel_orphan_timer(record)
    task = record.get("task")
    if task and not task.done():
        task.cancel()
    for ch in (record["arch_gate_ch"], record["mcts_gate_ch"]):
        try:
            close_channel(ch)
        except Exception:
            pass
    await _publish(record, {"type": "run_error", "error": "运行已中止"})


# ── 配置注册表 ────────────────────────────────────────────────────────────────

# 密钥字段注册表 —— 新增密钥只需在此添加一行
SECRET_FIELDS: dict[str, str] = {
    "openai_api_key":    "OPENAI_API_KEY",
    "pdf_md_token":      "PDF_MD_TOKEN",
    "embedding_api_key": "EMBEDDING_API_KEY",
}

# 仅控制 serve.py 自身行为、不传入图的字段
_SERVE_ONLY_FIELDS = {"resume_checkpoint_dir", "auto_approve_arch", "auto_approve_mcts"}

# 运行请求的默认值，全部从 Dynaconf settings 读取
_RUN_DEFAULTS = {
    "sandbox_timeout":        ("SANDBOX_TIMEOUT", 300),
    "beam_width":             ("BEAM_WIDTH", 3),
    "consecutive_bad_limit":  ("CONSECUTIVE_BAD_LIMIT", 3),
    "max_generations":        ("MAX_GENERATIONS", 50),
    "pop_size":               ("POP_SIZE", 10),
    "mutation_rate":          ("MUTATION_RATE", 0.3),
    "max_fe":                 ("MAX_FE", 5000),
    "user_generator_prompt":  (None, ""),
    "external_knowledge":     (None, ""),
    "existing_project_root":  (None, ""),
    "existing_direction":     ("EXISTING_DIRECTION", "maximize"),
    "auto_approve_arch":      (None, False),
    "auto_approve_mcts":      (None, False),
    "tex_template_path":      (None, ""),
    "reference_bib_path":     (None, ""),
    "expand_references":      ("EXPAND_REFERENCES", True),
    "s2_api_key":             (None, ""),
    "unpaywall_email":        (None, ""),
    "openai_api_key":         (None, ""),
    "pdf_md_token":           (None, ""),
    "embedding_api_key":      (None, ""),
    "text_model_name":        ("TEXT_MODEL_NAME", "claude-sonnet-4-6"),
    "write_model_name":       ("WRITE_MODEL_NAME", "claude-sonnet-4-6"),
    "embedding_model":        ("EMBEDDING_MODEL", "BAAI/bge-m3"),
    "embedding_model_source": ("EMBEDDING_MODEL_SOURCE", "api"),
    "reranker_path":          ("RERANKER_PATH", "BAAI/bge-reranker-v2-m3"),
    "reranker_model_source":  ("RERANKER_MODEL_SOURCE", "api"),
    "database_device":        ("DATABASE_DEVICE", "cpu"),
    "pdf_transform_mode":     ("PDF_TRANSFORM_MODE", "api"),
    "all_paper_num":          ("ALL_PAPER_NUM", 70),
    "resume_checkpoint_dir":  (None, ""),
}


def _apply_defaults(req: dict) -> dict:
    """用 Dynaconf settings 填充请求中缺失的字段。"""
    merged = {}
    for field, (settings_key, fallback) in _RUN_DEFAULTS.items():
        if field in req and req[field] != "":
            merged[field] = req[field]
        elif settings_key:
            merged[field] = settings.get(settings_key, fallback)
        else:
            merged[field] = fallback
    # 保留 requirement / output_dir 等非默认字段
    for k, v in req.items():
        if k not in merged:
            merged[k] = v
    return merged


# 可暴露给前端的默认值字段
_DEFAULTS_FIELDS = (
    "text_model_name", "write_model_name",
    "embedding_model", "embedding_model_source",
    "reranker_path", "reranker_model_source",
    "pdf_transform_mode", "database_device",
    "sandbox_timeout", "beam_width", "consecutive_bad_limit",
    "max_generations", "pop_size", "mutation_rate", "max_fe",
    "existing_direction", "expand_references", "all_paper_num",
)


# ── 路由 ──────────────────────────────────────────────────────────────────────

@app.get("/api/defaults")
async def get_defaults():
    """返回 settings.toml 中配置的表单默认值 + 密钥是否已配置的标记。"""
    defaults: dict = {}
    for field_name in _DEFAULTS_FIELDS:
        val = settings.get(field_name.upper(), "")
        if val != "":
            defaults[field_name] = val

    for form_field, settings_key in SECRET_FIELDS.items():
        val = settings.get(settings_key, "") or ""
        defaults[f"has_{form_field}"] = bool(val)

    return defaults


@app.post("/api/preflight")
async def preflight(req: dict = Body(...)):
    """启动前批量配置检查：一次性返回所有缺失的配置项。"""
    missing: list[dict] = []

    api_key = req.get("openai_api_key") or settings.get("OPENAI_API_KEY", "") or ""
    if not api_key:
        missing.append({
            "key": "OPENAI_API_KEY",
            "label": "OpenAI API Key",
            "hint": "请在「模型与密钥配置」中填写，或设置环境变量 OPENAGENTS_OPENAI_API_KEY",
            "required": True,
        })

    pdf_mode = req.get("pdf_transform_mode") or settings.get("PDF_TRANSFORM_MODE", "api")
    if pdf_mode == "api":
        token = req.get("pdf_md_token") or settings.get("PDF_MD_TOKEN", "") or ""
        if not token:
            missing.append({
                "key": "PDF_MD_TOKEN",
                "label": "PDF 解析 Token（MineRU）",
                "hint": "请在「模型与密钥配置」中填写，或将 PDF 解析模式改为 local",
                "required": True,
            })

    emb_source = req.get("embedding_model_source") or settings.get("EMBEDDING_MODEL_SOURCE", "api")
    if emb_source == "api":
        emb_key = (req.get("embedding_api_key") or
                   settings.get("RERANK_RETRIEVE_KEY", "") or
                   settings.get("EMBEDDING_API_KEY", "") or "")
        if not emb_key:
            missing.append({
                "key": "EMBEDDING_API_KEY",
                "label": "嵌入模型 API Key",
                "hint": "请在「模型与密钥配置」中填写",
                "required": True,
            })

    reranker_source = req.get("reranker_model_source") or settings.get("RERANKER_MODEL_SOURCE", "api")
    if reranker_source == "api":
        rerank_key = (req.get("embedding_api_key") or
                      settings.get("RERANK_RETRIEVE_KEY", "") or "")
        if not rerank_key:
            emb_key = settings.get("EMBEDDING_API_KEY", "") or ""
            if not emb_key:
                already = any(m["key"] == "EMBEDDING_API_KEY" for m in missing)
                if not already:
                    missing.append({
                        "key": "RERANK_RETRIEVE_KEY",
                        "label": "重排模型 API Key",
                        "hint": "请在「模型与密钥配置」中填写嵌入/重排模型 API Key",
                        "required": True,
                    })

    expand = req.get("expand_references", settings.get("EXPAND_REFERENCES", True))
    if expand and not req.get("s2_api_key"):
        s2 = settings.get("S2_API_KEY", "") or ""
        if not s2:
            missing.append({
                "key": "S2_API_KEY",
                "label": "Semantic Scholar API Key",
                "hint": "用于扩充参考文献；可在 .secrets.toml 中设置，或在启动配置中填写",
                "required": False,
            })

    if not req.get("unpaywall_email"):
        uw = settings.get("UNPAYWALL_EMAIL", "") or ""
        if not uw:
            missing.append({
                "key": "UNPAYWALL_EMAIL",
                "label": "Unpaywall Email",
                "hint": "用于下载开放获取论文；可在 .secrets.toml 中设置，或在启动配置中填写",
                "required": False,
            })

    has_critical = any(m["required"] for m in missing)
    return {"ok": not has_critical, "missing": missing}


def _persist_secrets(secrets: dict[str, str]) -> None:
    """将前端提交的密钥持久化到 .secrets.toml 并热重载 dynaconf。"""
    if not secrets:
        return

    secrets_path = Path(__file__).parent / ".secrets.toml"

    existing_lines: list[str] = []
    if secrets_path.exists():
        existing_lines = secrets_path.read_text(encoding="utf-8").splitlines()

    existing_keys: dict[str, int] = {}
    in_default = False
    for i, line in enumerate(existing_lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_default = stripped.lower() == "[default]"
            continue
        if in_default and "=" in stripped and not stripped.startswith("#"):
            k = stripped.split("=", 1)[0].strip()
            existing_keys[k] = i

    has_default = any(l.strip().lower() == "[default]" for l in existing_lines)
    if not has_default:
        if existing_lines and existing_lines[-1].strip():
            existing_lines.append("")
        existing_lines.append("[default]")

    for key, value in secrets.items():
        if not key.replace("_", "").isalnum() or not key.isupper():
            continue
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        new_line = f'{key} = "{escaped}"'
        if key in existing_keys:
            existing_lines[existing_keys[key]] = new_line
        else:
            insert_idx = len(existing_lines)
            found_default = False
            for i, line in enumerate(existing_lines):
                if line.strip().lower() == "[default]":
                    found_default = True
                    continue
                if found_default and line.strip().startswith("["):
                    insert_idx = i
                    break
            existing_lines.insert(insert_idx, new_line)

    secrets_path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")

    settings.reload()


@app.post("/api/runs")
async def start_run(req: dict = Body(...)):
    if "requirement" not in req or "output_dir" not in req:
        raise HTTPException(400, "requirement 和 output_dir 为必填字段")

    req = _apply_defaults(req)

    # 取消同 output_dir 的旧 run，防止孤儿协程泄漏
    for old in list(RUNS.values()):
        if not old["finished"] and old["config"].get("output_dir") == req["output_dir"]:
            logger.info("新 run 启动，自动取消旧 run %s", old["run_id"])
            await _cleanup_run(old)

    # 从 SECRET_FIELDS 注册表自动处理所有密钥
    form_secrets = {
        settings_key: req[field]
        for field, settings_key in SECRET_FIELDS.items()
        if req.get(field)
    }
    _persist_secrets(form_secrets)
    for field, settings_key in SECRET_FIELDS.items():
        if req.get(field):
            os.environ[f"OPENAGENTS_{settings_key}"] = req[field]

    run_id = str(uuid.uuid4())[:8]
    record = _new_run(run_id, req)
    RUNS[run_id] = record

    create_channel(record["arch_gate_ch"])
    create_channel(record["mcts_gate_ch"])

    checkpoint_dir = req.get("resume_checkpoint_dir") or str(
        Path(req["output_dir"]) / ".checkpoints" / run_id
    )

    run_vars = {k: v for k, v in req.items() if k not in _SERVE_ONLY_FIELDS}
    variables = {
        **run_vars,
        "arch_gate_channel_id": record["arch_gate_ch"],
        "mcts_gate_channel_id": record["mcts_gate_ch"],
        "checkpoint_dir":       checkpoint_dir,
    }

    async def run_and_notify():
        set_run_id(run_id)
        run_t0 = time.time()
        try:
            from utils import stream_graph
            _arch_step_outputs: dict = {}
            _mcts_tree_text_outputs: dict = {}
            _mcts_build_children_outputs: dict = {}
            async for event in stream_graph(
                "graphs/arch_then_mcts_then_u2e_ppw.json",
                variables=variables,
            ):
                event_type = event.get("type", "")
                if event_type == "graph_done":
                    await _publish(record, {"type": "run_done", "outputs": safe_serialize(event.get("final", {}))})
                else:
                    await _publish(record, safe_serialize(event))
                    if event_type == "llm_output":
                        logger.info(
                            "  LLM 输出  step=%s  ref=%s",
                            event.get("step_id"), event.get("ref"),
                        )
                    elif event_type == "tool_output":
                        logger.info(
                            "  工具输出  step=%s  ref=%s",
                            event.get("step_id"), event.get("ref"),
                        )

                    # ── 补发 metrics_update ──────────────────────────────
                    if event_type == "step_done":
                        step_id = event.get("step_id")
                        outputs = event.get("outputs") or {}
                        _metrics_patch: dict = {}

                        if step_id == "baseline" and isinstance(outputs, dict):
                            score = outputs.get("score")
                            if score is not None:
                                _metrics_patch["baseline"] = score
                        elif step_id == "tree_text" and isinstance(outputs, dict):
                            best = outputs.get("best_node")
                            gen = outputs.get("generation")
                            if isinstance(best, dict) and best.get("score") is not None:
                                _metrics_patch["mcts_best"] = best["score"]
                            if gen is not None:
                                _metrics_patch["mcts_generations"] = gen
                        elif step_id == "mcts_optimize" and isinstance(outputs, dict):
                            best = outputs.get("best_node")
                            if isinstance(best, dict) and best.get("score") is not None:
                                _metrics_patch["mcts_best"] = best["score"]
                        elif step_id == "update_state" and isinstance(outputs, dict):
                            best_obj = outputs.get("best_obj_overall")
                            fe = outputs.get("function_evals")
                            it = outputs.get("iteration")
                            if best_obj is not None:
                                _metrics_patch["u2e_best"] = best_obj
                            if fe is not None:
                                _metrics_patch["function_evals"] = fe
                            if it is not None:
                                _metrics_patch["u2e_iterations"] = it

                        if _metrics_patch:
                            await _publish(record, safe_serialize({
                                "type": "metrics_update",
                                "metrics": _metrics_patch,
                            }))

                        # ── 补发 log_line ────────────────────────────────
                        if isinstance(outputs, dict) and outputs.get("output_log"):
                            log_text = str(outputs["output_log"])
                            for line in log_text.splitlines():
                                await _publish(record, {
                                    "type": "log_line",
                                    "step_id": step_id,
                                    "text": line,
                                })

                    # 缓存 arch_step 输出
                    if event_type == "step_done" and event.get("step_id") == "arch_step":
                        _arch_step_outputs = event.get("outputs") or {}
                    # arch_gate 等待
                    if event_type == "step_start" and event.get("step_id") == "arch_gate":
                        if record["auto_approve_arch"]:
                            logger.info("arch_gate 自动放行（auto_approve_arch=true）")
                            await send_to_channel(record["arch_gate_ch"], {"feedback": ""})
                        else:
                            await _publish(record, safe_serialize({
                                "type": "gate_waiting",
                                "gate_id": "arch_gate",
                                "payload": {
                                    "gate_id":    "arch_gate",
                                    "gate_type":  "arch",
                                    "arch_tree":  _arch_step_outputs.get("tree"),
                                    "arch_ok":    _arch_step_outputs.get("ok"),
                                    "arch_errors": _arch_step_outputs.get("errors"),
                                },
                            }))
                    # 缓存 MCTS 输出
                    if event_type == "step_done" and event.get("step_id") == "tree_text":
                        _mcts_tree_text_outputs = event.get("outputs") or {}
                    if event_type == "step_done" and event.get("step_id") == "build_children":
                        _mcts_build_children_outputs = event.get("outputs") or {}
                    # mcts gate 等待
                    if event_type == "step_start" and event.get("step_id") == "gate":
                        if record["auto_approve_mcts"]:
                            logger.info("mcts_gate 自动放行（auto_approve_mcts=true）")
                            await send_to_channel(record["mcts_gate_ch"], {
                                "action":       "continue",
                                "next_mode":    "llm",
                                "selected_ids": [],
                            })
                        else:
                            await _publish(record, safe_serialize({
                                "type": "gate_waiting",
                                "gate_id": "mcts_gate",
                                "payload": {
                                    "gate_id":    "mcts_gate",
                                    "gate_type":  "mcts",
                                    "generation": _mcts_tree_text_outputs.get("generation"),
                                    "tree_text":  _mcts_tree_text_outputs.get("tree_text"),
                                    "new_nodes":  _mcts_build_children_outputs.get("new_nodes"),
                                },
                            }))
        except asyncio.CancelledError:
            elapsed = time.time() - run_t0
            logger.info("运行被取消  run_id=%s  已运行=%.1fs", run_id, elapsed)
            return
        except Exception as e:
            elapsed = time.time() - run_t0
            logger.error("运行异常  run_id=%s  已运行=%.1fs  error_type=%s  error=%s",
                         run_id, elapsed, type(e).__name__, e, exc_info=True)
            await _publish(record, {"type": "run_error", "error": str(e)})
        else:
            elapsed = time.time() - run_t0
            logger.info("运行完成  run_id=%s  总耗时=%.1fs", run_id, elapsed)
        finally:
            record["finished"] = True

    _log_vars = {k: v for k, v in variables.items() if k not in SECRET_FIELDS}
    logger.info("运行启动  run_id=%s  variables=%s", run_id, json.dumps(_log_vars, ensure_ascii=False, default=str))

    record["task"] = asyncio.create_task(run_and_notify())

    _save_run_config(run_id, req)

    return {"run_id": run_id, "checkpoint_dir": checkpoint_dir}


@app.get("/api/checkpoints")
async def list_checkpoints(output_dir: str):
    """扫描 output_dir/.checkpoints/ 目录，返回可用断点列表。"""
    ckpt_base = Path(output_dir) / ".checkpoints"
    if not ckpt_base.exists():
        return {"checkpoints": []}
    checkpoints = []
    for run_dir in sorted(ckpt_base.iterdir()):
        if not run_dir.is_dir():
            continue
        step_files = list(run_dir.glob("*.json"))
        if not step_files:
            continue
        completed_steps = [f.stem for f in step_files]
        last_mtime = max(f.stat().st_mtime for f in step_files)
        checkpoints.append({
            "run_id":          run_dir.name,
            "checkpoint_dir":  str(run_dir),
            "completed_steps": completed_steps,
            "step_count":      len(completed_steps),
            "last_updated":    last_mtime,
        })
    checkpoints.sort(key=lambda x: x["last_updated"], reverse=True)
    return {"checkpoints": checkpoints}


@app.post("/api/runs/{run_id}/gate/{gate_id}")
async def submit_gate(run_id: str, gate_id: str, body: dict = Body(...)):
    record = RUNS.get(run_id)
    if not record:
        logger.warning("gate 提交失败: run_id=%s 不存在", run_id)
        raise HTTPException(404, "Run not found")

    logger.info("gate 提交  run_id=%s  gate_id=%s  body=%s", run_id, gate_id, json.dumps(body, ensure_ascii=False, default=str))

    if gate_id == "arch_gate":
        await send_to_channel(record["arch_gate_ch"], body)
    elif gate_id == "mcts_gate":
        await send_to_channel(record["mcts_gate_ch"], body)
    else:
        logger.warning("gate 提交失败: 未知 gate_id=%s", gate_id)
        raise HTTPException(400, f"Unknown gate: {gate_id}")

    return {"ok": True}


@app.post("/api/runs/{run_id}/abort")
async def abort_run(run_id: str):
    record = RUNS.get(run_id)
    if not record:
        logger.warning("abort 失败: run_id=%s 不存在", run_id)
        raise HTTPException(404, "Run not found")
    logger.info("手动中止运行  run_id=%s", run_id)
    await _cleanup_run(record)
    return {"ok": True}


# ── 运行配置管理 ──────────────────────────────────────────────────────────────

def _save_run_config(run_id: str, config: dict) -> str:
    """将运行配置保存为 JSON 文件，返回文件名。密钥字段不落盘。"""
    safe_config = {k: v for k, v in config.items() if k not in SECRET_FIELDS}
    filename = f"{run_id}.json"
    data = {
        "run_id":     run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "config":     safe_config,
    }
    (RUN_CONFIGS_DIR / filename).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return filename


@app.get("/api/run-configs")
async def list_run_configs():
    """列出所有已保存的运行配置，按创建时间降序。"""
    entries = []
    for f in sorted(RUN_CONFIGS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            entries.append({
                "filename":   f.name,
                "run_id":     data.get("run_id", f.stem),
                "created_at": data.get("created_at", ""),
                "requirement": (data.get("config") or {}).get("requirement", ""),
            })
        except Exception:
            pass
    return {"configs": entries}


@app.get("/api/run-configs/{filename}")
async def load_run_config(filename: str):
    """加载指定配置文件，返回完整 config 字段。"""
    p = RUN_CONFIGS_DIR / filename
    if not p.exists():
        raise HTTPException(404, "Config not found")
    return json.loads(p.read_text(encoding="utf-8"))


@app.delete("/api/run-configs/{filename}")
async def delete_run_config(filename: str):
    """删除指定配置文件。"""
    p = RUN_CONFIGS_DIR / filename
    if not p.exists():
        raise HTTPException(404, "Config not found")
    p.unlink()
    return {"ok": True}


@app.get("/api/files")
async def get_file(path: str):
    """直接读取本地文件（paper.tex、日志等）"""
    p = Path(path)
    if not p.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(p)


# ── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/runs/{run_id}/events")
async def ws_events(websocket: WebSocket, run_id: str):
    await websocket.accept()
    record = RUNS.get(run_id)
    if not record:
        logger.warning("WebSocket 连接失败: run_id=%s 不存在", run_id)
        await websocket.close(1008, "Run not found")
        return

    q: asyncio.Queue[str] = asyncio.Queue()
    record["subscribers"].append(q)
    _cancel_orphan_timer(record)
    logger.info("WebSocket 连接  run_id=%s  subscribers=%d  回放历史事件=%d", run_id, len(record["subscribers"]), len(record["event_history"]))

    for evt_str in record["event_history"]:
        await websocket.send_text(evt_str)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30)
                await websocket.send_text(msg)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        record["subscribers"].remove(q)
        logger.info("WebSocket 断开  run_id=%s  剩余subscribers=%d", run_id, len(record["subscribers"]))
        _start_orphan_timer(record)


# ── 静态文件（前端）─────────────────────────────────────────────────────────

DIST = ROOT / "frontend_dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        index = DIST / "index.html"
        return FileResponse(index)
else:
    @app.get("/")
    async def dev_notice():
        return JSONResponse({"message": "前端未构建。请先运行 cd frontend && npm run build"})


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    logger.info("Open Agents 服务启动中，本地地址: http://localhost:%d", port)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def _open():
            await asyncio.sleep(1.5)
            webbrowser.open(f"http://localhost:{port}")
        asyncio.create_task(_open())
        yield

    app.router.lifespan_context = lifespan

    uvicorn.run(
        "serve:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
