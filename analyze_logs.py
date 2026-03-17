"""
analyze_logs.py — Sience-OS 日志异常分析工具

用法:
    # 分析日志文件
    python analyze_logs.py run.log

    # 从 stdin 读取（管道）
    python serve.py 2>&1 | python analyze_logs.py

    # 指定慢步骤阈值（秒）
    python analyze_logs.py run.log --slow-threshold 60

    # 只显示指定 run_id 的日志
    python analyze_logs.py run.log --run-id abc12345

    # 输出 JSON 格式报告
    python analyze_logs.py run.log --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ── 日志行正则 ─────────────────────────────────────────────────────────────────

# serve.py / _graph.py 控制台格式（含 step_path）:
#   2025-03-13 19:48:30  INFO      [abc12345]  [arch_step → codegen]  utils._graph  步骤开始 ...
_RE_CONSOLE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"\s+(?P<level>\w+)\s+"
    r"\[(?P<run_id>[^\]]*)\]\s+"
    r"\[(?P<step_path>[^\]]*)\]\s+"
    r"(?P<module>\S+)\s+"
    r"(?P<msg>.+)$"
)

# papw 文件格式:
#   2025-03-13 19:48:30 - database_building.py - INFO - processing ...
_RE_FILE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"\s+-\s+(?P<module>\S+)"
    r"\s+-\s+(?P<level>\w+)"
    r"\s+-\s+(?P<msg>.+)$"
)

# 从消息中提取耗时（如 "耗时=12.3s" 或 "12.3fs" 等写法）
_RE_ELAPSED = re.compile(r"耗时[=:]\s*([\d.]+)s")

# 步骤 id 提取（如 "[arch_step] 步骤开始"）
_RE_STEP_ID = re.compile(r"\[([a-zA-Z0-9_\-]+)\]")

# loop_to 回跳迭代次数提取（如 "第 3 次"）
_RE_LOOP_ITER = re.compile(r"第\s*(\d+)\s*次")

# ── 异常定义 ──────────────────────────────────────────────────────────────────

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH     = "HIGH"
SEVERITY_MEDIUM   = "MEDIUM"
SEVERITY_LOW      = "LOW"
SEVERITY_INFO     = "INFO"

_SEVERITY_ORDER = {
    SEVERITY_CRITICAL: 4,
    SEVERITY_HIGH:     3,
    SEVERITY_MEDIUM:   2,
    SEVERITY_LOW:      1,
    SEVERITY_INFO:     0,
}


@dataclass
class Anomaly:
    severity:  str
    category:  str
    run_id:    str
    message:   str
    line_no:   int
    raw_line:  str
    ts:        str = ""
    step_id:   str = ""
    extra:     dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "run_id":   self.run_id,
            "ts":       self.ts,
            "step_id":  self.step_id,
            "line_no":  self.line_no,
            "message":  self.message,
            "extra":    self.extra,
        }


# ── 解析 ───────────────────────────────────────────────────────────────────────

@dataclass
class LogLine:
    ts:        str
    level:     str
    run_id:    str
    module:    str
    msg:       str
    line_no:   int
    raw:       str
    step_path: str = ""


def parse_line(raw: str, line_no: int) -> LogLine | None:
    raw = raw.rstrip()
    m = _RE_CONSOLE.match(raw)
    if m:
        return LogLine(
            ts=m["ts"], level=m["level"].upper(),
            run_id=m["run_id"].strip(), module=m["module"],
            msg=m["msg"].strip(), line_no=line_no, raw=raw,
            step_path=m["step_path"].strip(),
        )
    m = _RE_FILE.match(raw)
    if m:
        return LogLine(
            ts=m["ts"], level=m["level"].upper(),
            run_id="", module=m["module"],
            msg=m["msg"].strip(), line_no=line_no, raw=raw,
        )
    return None


# ── 异常检测规则 ───────────────────────────────────────────────────────────────

def detect_anomalies(
    lines: list[LogLine],
    slow_threshold: float = 30.0,
    loop_warn_threshold: int = 3,
    run_id_filter: str = "",
) -> list[Anomaly]:
    """对解析后的日志行列表执行全量异常检测，返回 Anomaly 列表。"""
    anomalies: list[Anomaly] = []

    # 按 run_id 分组，统计每个运行的步骤耗时
    run_step_elapsed: dict[str, dict[str, float]] = defaultdict(dict)

    for ll in lines:
        if run_id_filter and ll.run_id and ll.run_id != run_id_filter:
            continue

        msg = ll.msg

        # ── 1. ERROR / CRITICAL 级别 ─────────────────────────────────────
        if ll.level in ("ERROR", "CRITICAL"):
            step_m = _RE_STEP_ID.search(msg)
            step_id = step_m.group(1) if step_m else ""
            anomalies.append(Anomaly(
                severity=SEVERITY_HIGH if ll.level == "ERROR" else SEVERITY_CRITICAL,
                category="error_log",
                run_id=ll.run_id,
                ts=ll.ts,
                step_id=step_id,
                message=f"[{ll.level}] {msg}",
                line_no=ll.line_no,
                raw_line=ll.raw,
            ))

        # ── 2. 步骤执行失败 ──────────────────────────────────────────────
        if "步骤执行失败" in msg:
            step_m = _RE_STEP_ID.search(msg)
            step_id = step_m.group(1) if step_m else ""
            anomalies.append(Anomaly(
                severity=SEVERITY_HIGH,
                category="step_failure",
                run_id=ll.run_id,
                ts=ll.ts,
                step_id=step_id,
                message=f"步骤执行失败: {msg}",
                line_no=ll.line_no,
                raw_line=ll.raw,
            ))

        # ── 3. 断点写入失败 ──────────────────────────────────────────────
        if "断点写入失败" in msg:
            step_m = _RE_STEP_ID.search(msg)
            step_id = step_m.group(1) if step_m else ""
            anomalies.append(Anomaly(
                severity=SEVERITY_MEDIUM,
                category="checkpoint_failure",
                run_id=ll.run_id,
                ts=ll.ts,
                step_id=step_id,
                message=f"断点写入失败: {msg}",
                line_no=ll.line_no,
                raw_line=ll.raw,
            ))

        # ── 4. 孤儿运行自动中止 ──────────────────────────────────────────
        if "无客户端连接，自动中止" in msg:
            anomalies.append(Anomaly(
                severity=SEVERITY_MEDIUM,
                category="orphan_abort",
                run_id=ll.run_id,
                ts=ll.ts,
                message=f"孤儿运行自动中止: {msg}",
                line_no=ll.line_no,
                raw_line=ll.raw,
            ))

        # ── 5. run_error 事件 ────────────────────────────────────────────
        if "run_error" in msg or "运行已中止" in msg:
            anomalies.append(Anomaly(
                severity=SEVERITY_HIGH,
                category="run_error",
                run_id=ll.run_id,
                ts=ll.ts,
                message=f"运行错误/中止: {msg}",
                line_no=ll.line_no,
                raw_line=ll.raw,
            ))

        # ── 6. 慢步骤检测 ────────────────────────────────────────────────
        elapsed_m = _RE_ELAPSED.search(msg)
        if elapsed_m and ("步骤完成" in msg or "完成" in msg):
            elapsed = float(elapsed_m.group(1))
            step_m = _RE_STEP_ID.search(msg)
            step_id = step_m.group(1) if step_m else ""
            run_step_elapsed[ll.run_id][step_id] = elapsed
            if elapsed >= slow_threshold:
                anomalies.append(Anomaly(
                    severity=SEVERITY_MEDIUM if elapsed < slow_threshold * 3 else SEVERITY_HIGH,
                    category="slow_step",
                    run_id=ll.run_id,
                    ts=ll.ts,
                    step_id=step_id,
                    message=f"步骤耗时过长: {step_id} 耗时 {elapsed:.1f}s（阈值 {slow_threshold}s）",
                    line_no=ll.line_no,
                    raw_line=ll.raw,
                    extra={"elapsed_s": elapsed, "threshold_s": slow_threshold},
                ))

        # ── 7. loop_to 高迭代次数 ─────────────────────────────────────────
        if "loop_to 回跳" in msg:
            iter_m = _RE_LOOP_ITER.search(msg)
            iteration = int(iter_m.group(1)) if iter_m else 0
            if iteration >= loop_warn_threshold:
                step_m = _RE_STEP_ID.search(msg)
                step_id = step_m.group(1) if step_m else ""
                anomalies.append(Anomaly(
                    severity=SEVERITY_LOW if iteration < loop_warn_threshold * 2 else SEVERITY_MEDIUM,
                    category="high_loop_count",
                    run_id=ll.run_id,
                    ts=ll.ts,
                    step_id=step_id,
                    message=f"loop_to 高迭代次数（第 {iteration} 次）: {msg}",
                    line_no=ll.line_no,
                    raw_line=ll.raw,
                    extra={"iteration": iteration},
                ))

        # ── 8. WebSocket 连接失败 ────────────────────────────────────────
        if "WebSocket 连接失败" in msg:
            anomalies.append(Anomaly(
                severity=SEVERITY_LOW,
                category="websocket_failure",
                run_id=ll.run_id,
                ts=ll.ts,
                message=f"WebSocket 连接失败: {msg}",
                line_no=ll.line_no,
                raw_line=ll.raw,
            ))

        # ── 9. gate 提交失败 ──────────────────────────────────────────────
        if "gate 提交失败" in msg:
            anomalies.append(Anomaly(
                severity=SEVERITY_MEDIUM,
                category="gate_failure",
                run_id=ll.run_id,
                ts=ll.ts,
                message=f"gate 提交失败: {msg}",
                line_no=ll.line_no,
                raw_line=ll.raw,
            ))

        # ── 10. WARNING 级别（低优先级汇总）───────────────────────────────
        if ll.level == "WARNING" and not any(
            kw in msg for kw in [
                "无客户端连接", "gate 提交失败", "WebSocket 连接失败", "断点写入失败",
            ]
        ):
            anomalies.append(Anomaly(
                severity=SEVERITY_LOW,
                category="warning_log",
                run_id=ll.run_id,
                ts=ll.ts,
                message=f"[WARNING] {msg}",
                line_no=ll.line_no,
                raw_line=ll.raw,
            ))

        # ── 11. 循环依赖检测 ─────────────────────────────────────────────
        if "循环依赖" in msg:
            anomalies.append(Anomaly(
                severity=SEVERITY_CRITICAL,
                category="cycle_dependency",
                run_id=ll.run_id,
                ts=ll.ts,
                message=f"DAG 循环依赖: {msg}",
                line_no=ll.line_no,
                raw_line=ll.raw,
            ))

        # ── 12. LLM API 相关错误 ─────────────────────────────────────────
        for kw in ("RateLimitError", "APIError", "timeout", "Timeout", "Connection refused"):
            if kw.lower() in msg.lower():
                anomalies.append(Anomaly(
                    severity=SEVERITY_HIGH,
                    category="llm_api_error",
                    run_id=ll.run_id,
                    ts=ll.ts,
                    message=f"LLM/API 错误（{kw}）: {msg}",
                    line_no=ll.line_no,
                    raw_line=ll.raw,
                    extra={"keyword": kw},
                ))
                break

    return anomalies


# ── 统计摘要 ───────────────────────────────────────────────────────────────────

def build_summary(lines: list[LogLine], anomalies: list[Anomaly]) -> dict[str, Any]:
    run_ids = {ll.run_id for ll in lines if ll.run_id}
    levels: dict[str, int] = defaultdict(int)
    for ll in lines:
        levels[ll.level] += 1

    by_severity: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)
    for a in anomalies:
        by_severity[a.severity] += 1
        by_category[a.category] += 1

    return {
        "total_lines":   len(lines),
        "parsed_lines":  len(lines),
        "run_ids":       sorted(run_ids),
        "log_levels":    dict(levels),
        "total_anomalies": len(anomalies),
        "by_severity":   dict(by_severity),
        "by_category":   dict(by_category),
    }


# ── 报告输出 ───────────────────────────────────────────────────────────────────

_SEVERITY_COLOR = {
    SEVERITY_CRITICAL: "\033[1;31m",   # 红色加粗
    SEVERITY_HIGH:     "\033[31m",     # 红色
    SEVERITY_MEDIUM:   "\033[33m",     # 黄色
    SEVERITY_LOW:      "\033[34m",     # 蓝色
    SEVERITY_INFO:     "\033[0m",      # 默认
}
_RESET = "\033[0m"


def _color(text: str, severity: str, use_color: bool) -> str:
    if not use_color:
        return text
    return _SEVERITY_COLOR.get(severity, "") + text + _RESET


def print_text_report(
    summary: dict[str, Any],
    anomalies: list[Anomaly],
    use_color: bool = True,
    min_severity: str = SEVERITY_LOW,
) -> None:
    min_order = _SEVERITY_ORDER.get(min_severity, 0)
    filtered = [a for a in anomalies if _SEVERITY_ORDER.get(a.severity, 0) >= min_order]
    filtered.sort(key=lambda a: (_SEVERITY_ORDER.get(a.severity, 0) * -1, a.line_no))

    sep = "=" * 70
    print(sep)
    print("  Sience-OS 日志异常分析报告")
    print(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(sep)

    print("\n【基础统计】")
    print(f"  解析行数  : {summary['total_lines']}")
    print(f"  涉及运行  : {', '.join(summary['run_ids']) or '(无 run_id)'}")
    print("  级别分布  :", "  ".join(f"{k}={v}" for k, v in sorted(summary["log_levels"].items())))
    print(f"  发现异常  : {summary['total_anomalies']} 条")

    if summary["by_severity"]:
        print("  严重度分布:", "  ".join(
            f"{k}={v}" for k, v in sorted(
                summary["by_severity"].items(),
                key=lambda x: _SEVERITY_ORDER.get(x[0], 0), reverse=True,
            )
        ))
    if summary["by_category"]:
        print("  分类分布  :", "  ".join(f"{k}={v}" for k, v in summary["by_category"].items()))

    print(f"\n【异常明细】（显示 {min_severity} 及以上，共 {len(filtered)} 条）")
    if not filtered:
        print("  ✓ 未发现异常")
    else:
        for a in filtered:
            tag = _color(f"[{a.severity:<8}]", a.severity, use_color)
            run = f"[{a.run_id}]" if a.run_id else "[global]"
            step = f"  步骤={a.step_id}" if a.step_id else ""
            print(f"\n  {tag} {a.ts}  {run}  行{a.line_no}{step}")
            print(f"    分类: {a.category}")
            print(f"    描述: {a.message}")
            if a.extra:
                print(f"    附加: {a.extra}")
            print(f"    原行: {a.raw_line[:120]}{'...' if len(a.raw_line) > 120 else ''}")

    print("\n" + sep)


# ── 主函数 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="分析 Sience-OS 日志，检测异常模式",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "logfile", nargs="?",
        help="日志文件路径（不指定则从 stdin 读取）",
    )
    parser.add_argument(
        "--slow-threshold", type=float, default=30.0, metavar="SECONDS",
        help="慢步骤告警阈值（秒），默认 30",
    )
    parser.add_argument(
        "--loop-warn", type=int, default=3, metavar="N",
        help="loop_to 回跳次数告警阈值，默认 3",
    )
    parser.add_argument(
        "--run-id", default="", metavar="RUN_ID",
        help="只分析指定 run_id 的日志",
    )
    parser.add_argument(
        "--min-severity", default=SEVERITY_LOW,
        choices=[SEVERITY_INFO, SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_HIGH, SEVERITY_CRITICAL],
        help="输出的最低严重度，默认 LOW",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="输出 JSON 格式报告",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="禁用终端颜色",
    )
    args = parser.parse_args()

    # 读取输入
    if args.logfile:
        p = Path(args.logfile)
        if not p.exists():
            print(f"错误：文件不存在：{args.logfile}", file=sys.stderr)
            sys.exit(1)
        raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        raw_lines = sys.stdin.read().splitlines()

    if not raw_lines:
        print("警告：未读取到任何日志内容", file=sys.stderr)
        sys.exit(0)

    # 解析
    parsed: list[LogLine] = []
    unparsed = 0
    for i, raw in enumerate(raw_lines, start=1):
        ll = parse_line(raw, i)
        if ll:
            parsed.append(ll)
        elif raw.strip():
            unparsed += 1

    # 检测
    anomalies = detect_anomalies(
        parsed,
        slow_threshold=args.slow_threshold,
        loop_warn_threshold=args.loop_warn,
        run_id_filter=args.run_id,
    )

    summary = build_summary(parsed, anomalies)
    summary["unparsed_lines"] = unparsed

    # 输出
    if args.json:
        report = {
            "summary":   summary,
            "anomalies": [a.to_dict() for a in anomalies],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        use_color = not args.no_color and sys.stdout.isatty()
        print_text_report(summary, anomalies, use_color=use_color, min_severity=args.min_severity)


if __name__ == "__main__":
    main()
