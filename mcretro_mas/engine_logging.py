import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

ENGINE_LOG_DIR = os.environ.get("MCR_ENGINE_LOG_DIR") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(ENGINE_LOG_DIR, exist_ok=True)

ENGINE_RUN_ID = os.environ.get("MCR_ENGINE_RUN_ID") or (
    f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
)
os.environ.setdefault("MCR_ENGINE_RUN_ID", ENGINE_RUN_ID)
os.environ.setdefault("MCR_ENGINE_MAIN_PID", str(os.getpid()))
ENGINE_MAIN_PID = int(os.environ["MCR_ENGINE_MAIN_PID"])
ENGINE_PID = os.getpid()
ENGINE_PID_SUFFIX = "" if ENGINE_PID == ENGINE_MAIN_PID else f"_pid{ENGINE_PID}"

ENGINE_EVENTS_LOG_PATH = os.path.join(
    ENGINE_LOG_DIR, f"engine_events_{ENGINE_RUN_ID}{ENGINE_PID_SUFFIX}.jsonl"
)
ENGINE_DIALOG_LOG_PATH = os.path.join(
    ENGINE_LOG_DIR, f"engine_dialog_{ENGINE_RUN_ID}{ENGINE_PID_SUFFIX}.log"
)
ENGINE_LOG_MAX_CHARS = int(os.environ.get("MCR_ENGINE_LOG_MAX_CHARS", "0"))


def _clip_text(text: str) -> str:
    if ENGINE_LOG_MAX_CHARS <= 0:
        return text
    if len(text) <= ENGINE_LOG_MAX_CHARS:
        return text
    return text[:ENGINE_LOG_MAX_CHARS] + f"\n...[truncated {len(text) - ENGINE_LOG_MAX_CHARS} chars]"


def _json_safe(value: Any) -> Any:
    if isinstance(value, str):
        return _clip_text(value)
    if isinstance(value, bytes):
        return _clip_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return _clip_text(str(value))


def _append_text_log(line: str) -> None:
    try:
        with open(ENGINE_DIALOG_LOG_PATH, "a", encoding="utf-8-sig") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_event(event: str, **fields: Any) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "run_id": ENGINE_RUN_ID,
        "pid": ENGINE_PID,
        **fields,
    }
    try:
        with open(ENGINE_EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(_json_safe(record), ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_dialog(tag: str, content: str, **meta: Any) -> None:
    prefix = f"[{datetime.now().isoformat(timespec='seconds')}] [{ENGINE_RUN_ID}] [{tag}]"
    if meta:
        prefix += " " + json.dumps(_json_safe(meta), ensure_ascii=False)
    _append_text_log(prefix)
    _append_text_log(_clip_text(content))
    _append_text_log("-" * 80)

