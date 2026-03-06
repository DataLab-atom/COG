from __future__ import annotations
import sys
import os
import json
try:
    from json_repair import repair_json  # type: ignore
except Exception:
    def repair_json(text: str) -> str:
        return text
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from dotenv import load_dotenv
load_dotenv()
from LLM_MODULE.llm import OpenAILLM
from LLM_MODULE.tools import tool_spec, make_tool_handler,load_plugins
from .dataset_mas_sys_prompts import data_analyse_agent_system_prompt, sci_sys_p
loaded = load_plugins(on_error="raise")
@dataclass
class reporter_config:
    demand: str
    dataset_brief_info: str
    dataset_dir: str
    problem_name: str  # <--- [新增] 用于确定输出文件夹
    max_turns: int = 4
    temperature_sci: float = 0.2
    temperature_coder: float = 0.2
    sci_model: Optional[str] = None
    coder_model: Optional[str] = None

class Reporter:
    def __init__(self, config: reporter_config, workspace: Optional[Path] = None):
        self.config = config
        
        # [优雅路径逻辑]
        # 1. 自动定位项目根目录 (Project Root)
        # 假设当前文件在 <Project_Root>/dataset_report_module/reporter.py
        if workspace:
            self.project_root = Path(workspace).resolve()
        else:
            self.project_root = Path(__file__).resolve().parent.parent

        # 2. 构建绝对输出路径
        # 结果例如: C:\Users\...\HMACF_PRO_MAX\problems\classfiy\reports
        self.problems_dir = self.project_root / "problems"
        self.outputs_dir = self.problems_dir / self.config.problem_name / "reports"
        
        # 3. 创建目录 (如果不存在会自动创建)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        
        # 打印路径确认
        print(f"[Reporter] Init: Project Root detected at: {self.project_root}")
        print(f"[Reporter] Output Dir set to: {self.outputs_dir}")

        self.sci_llm = OpenAILLM(model=config.sci_model,module_name="dataset_report",agent_name="sci_agent")
        self.coder_llm = OpenAILLM(model=config.coder_model,module_name="dataset_report",agent_name="coder_agent")

    def _dataset_abs_path(self) -> Path:
        """
        获取数据集的绝对路径
        """
        p = Path(self.config.dataset_dir)
        
        # 如果 config 里传的是绝对路径，直接返回
        if p.is_absolute():
            return p
            
        # 如果是相对路径，基于项目根目录拼接
        # 这样无论相对路径是 "problems/..." 还是 "data/..." 都能正确解析
        return (self.project_root / p).resolve()

    def _scientist_initial_messages(self) -> List[Dict[str, str]]:
        dataset_path = self._dataset_abs_path()
        sys_prompt = sci_sys_p
        init_user = (
        "[Task Background]\n"
        f"User Demand: {self.config.demand}\n"
        f"Dataset Brief Info: {self.config.dataset_brief_info}\n"
        f"Data File Path: {str(dataset_path)}\n" # 确保转为字符串
        "Please begin the first step (you can think or analyze first, or propose the initial code request), and respond strictly according to the JSON protocol.\n"
        )
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": init_user},
        ]

    def _coder_messages(self, instruction: str) -> List[Dict[str, str]]:
        dataset_path = self._dataset_abs_path()
        sys_prompt = (
            data_analyse_agent_system_prompt
            + "\n\n"
            + "In your code, you can directly read the dataset, datasets path: \"" + str(dataset_path) + "\"\n"
        )
        user = (
            "Write and execute Python code according to the instruction below,\n"
            "printing any required tables or results to stdout as needed.\n"
            f"[Instruction] {instruction}\n"
        )
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ]

    def run(self, verbose: bool = False) -> List[Dict[str, str]]:
        # === 以下核心逻辑保持原样 ===
        transcript: List[Dict[str, str]] = []

        # 科学家回合
        sci_msgs = self._scientist_initial_messages()
        finalized = False

        def _vprint(msg: str) -> None:
            if verbose:
                print(msg)

        def _clip(text: str, limit: int = 300) -> str:
            if text is None:
                return ""
            if len(text) <= limit:
                return text
            return text[:limit] + f"... (共{len(text)}字)"

        _vprint(f"[MAS] 开始对话，最大回合数: {self.config.max_turns}")
        for turn in range(self.config.max_turns):
            _vprint("=" * 60)
            _vprint(f"[回合] 第 {turn + 1} 轮：请求科学家回复…")
            sci_reply = self.sci_llm.chat(sci_msgs,temperature=self.config.temperature_sci)
            transcript.append({"speaker": "scientist", "content": sci_reply})
            _vprint(f"[科学家原文] {_clip(sci_reply, 400)}")

            # 解析科学家输出为 JSON
            try:
                sci_obj = json.loads(sci_reply)
            except Exception:
                try:
                    repaired = repair_json(sci_reply)
                    sci_obj = json.loads(repaired)
                    _vprint("[提示] 原始输出非标准JSON，已修复并继续。")
                except Exception:
                    _vprint("[错误] 科学家输出非JSON且无法修复，终止对话。")
                    break

            # 兼容旧协议（is_final/output），以及新协议（status/output）
            status = sci_obj.get("status")
            if status is None and "is_final" in sci_obj:
                status = "结束" if sci_obj.get("is_final") else "编写代码分析"

            _vprint(f"[状态] 科学家状态: {status or '未知'}")

            if status == "Done":
                report_md = sci_obj.get("output", "")
                _vprint("[收尾] 科学家给出最终报告，保存数据分析报告。")
                self._save_report(report_md)
                _vprint(f"[保存到] 路径: {self.outputs_dir / 'data_analysis_report.md'}")
                finalized = True
                break

            # 思考/分析：直接继续下一步由科学家推进
            if status in {"Thinking", "Analyzing"}:
                # 将上轮内容作为 assistant 回显，随后作为 user 促使其继续
                sci_msgs.append({"role": "assistant", "content": sci_reply})
                sci_msgs.append({
                    "role": "user",
                    "content": "Noted. Please continue with the next step of thinking or analysis, and continue to respond strictly according to the JSON protocol.",
                })
                _vprint("[继续] 科学家处于思考/分析阶段，已提示继续。")
                continue

            # 需要代码执行：交给代码智能体，并进入循环以提升鲁棒性
            if status in {"Write code for analysis", "Require code", "Need code", "Code", "Write code","Writing Code"}:
                instruction = sci_obj.get("output", "")
                _vprint("[派发] 将需求转交代码智能体执行…")
                _vprint(f"[指令] {_clip(instruction, 400)}")
                
                # 代码执行循环，提升鲁棒性
                max_code_attempts = 3  # 最大代码尝试次数
                code_success = False
                
                for code_attempt in range(max_code_attempts):
                    _vprint(f"[代码尝试] 第 {code_attempt + 1} 次尝试")
                    
                    coder_msgs = self._coder_messages(instruction)
                    coder_reply = self.coder_llm.chat_with_tools(
                        coder_msgs, tools=tool_spec(toolset="dataset_coder"), tool_handler=make_tool_handler(toolset="dataset_coder", enforce_policy=True),temperature=self.config.temperature_coder
                    )
                    transcript.append({"speaker": "coder", "content": coder_reply})
                    
                    # 尝试解析工具返回概览
                    try:
                        final_result = coder_msgs[-1]["content"]
                        try:
                            
                            _obj = json.loads(final_result)
                        except Exception:
                            _obj = json.loads(repair_json(final_result))

                        success = _obj.get("success", False)
                        stdout = _obj.get("stdout", "")
                        stderr = _obj.get("stderr", "")
                        _vprint(f"[代码返回] success={success} | stdout={len(stdout)}字 | stderr={len(stderr)}字")
                        if stdout:
                            _vprint(f"[stdout预览]\n{_clip(stdout, 600)}")
                        if stderr:
                            _vprint(f"[stderr预览]\n{_clip(stderr, 400)}")
                        
                        # 如果代码执行成功，跳出循环
                        if success:
                            code_success = True
                            _vprint("[成功] 代码执行成功，跳出循环。")
                            break
                        else:
                            _vprint(f"[重试] 代码执行失败，准备第 {code_attempt + 2} 次尝试。")
                            # 更新 coder_msgs 以包含错误信息，让代码智能体知道需要修复
                            coder_msgs.append({"role": "assistant", "content": coder_reply})
                            coder_msgs.append({
                                "role": "user",
                                "content": f"Code execution failed, an error exists. Please fix the following error and regenerate the code:\n{stderr if stderr else 'Unknown error'}\nPlease regenerate the correct code based on the original instruction."
                            })
                            
                    except Exception as e:
                        _vprint(f"[代码返回-原文]\n{_clip(coder_reply, 60000)}")
                        _vprint(f"[错误] 解析代码返回失败: {e}")
                        # 如果解析失败，也尝试让代码智能体重新生成
                        if code_attempt < max_code_attempts - 1:
                            coder_msgs.append({"role": "assistant", "content": coder_reply})
                            coder_msgs.append({
                                "role": "user",
                                "content": f"The code returned a format error and could not be parsed. Please regenerate the correct code to satisfy the original instruction: {instruction}"
                            })
                
                # 如果所有尝试都失败了
                if not code_success:
                    _vprint(f"[警告] 代码执行经过 {max_code_attempts} 次尝试仍然失败，将错误信息返回给科学家。")
                    # 将失败的执行结果返回给科学家
                    sci_msgs.append({"role": "assistant", "content": sci_reply})
                    sci_msgs.append({
                        "role": "user",
                        "content": (
                            f"Code execution failed after {max_code_attempts} attempts. The original error message is as follows:\n" + coder_reply 
                            + "\nPlease reconsider the code implementation plan, or adjust the analysis strategy, and still output JSON according to the protocol."
                        ),
                    })
                else:
                    # 代码执行成功，将结果返回给科学家
                    sci_msgs.append({"role": "assistant", "content": sci_reply})
                    sci_msgs.append({
                        "role": "user",
                        "content": (
                            "The following is the raw result (JSON) returned from the code execution. Please analyze it and proceed to the next step, still outputting in JSON according to the protocol.\n" 
                            + coder_reply
                        ),
                    })
                
                _vprint("[反馈] 已将代码执行结果回传给科学家。")
                continue

            # 未识别的状态：尝试提示其遵循协议
            sci_msgs.append({"role": "assistant", "content": sci_reply})
            sci_msgs.append({
                "role": "user",
                "content": "Unrecognized status field. Please strictly output a JSON of{\"status\": \"Thinking|Analyzing|Writing Code|Done\", \"output\": string}",
            })
            _vprint("[警告] 未识别的科学家状态，已提示其遵循协议。")
            continue

        if not finalized:
            _vprint("=" * 60)
            _vprint("[收敛] 强制输出报告")
            sci_msgs.append({
                "role": "user",
                "content": (
                    "Based on the current information, please converge and output the final Markdown analysis report. Strictly return as JSON: {\"status\": \"Done\", \"output\": \"...\"}"
                ),
            })
            sci_reply = self.sci_llm.chat(sci_msgs,temperature=self.config.temperature_sci)
            transcript.append({"speaker": "scientist", "content": sci_reply})
            try:
                sci_obj = json.loads(sci_reply)
                status = sci_obj.get("status")
                if status is None and "is_final" in sci_obj:
                    status = "Done" if sci_obj.get("is_final") else None
                if status == "Done":
                    _vprint("[收尾] 科学家给出最终报告，保存数据分析报告。")
                    report_md = sci_obj.get("output", "")
                    self._save_report(report_md)
                    _vprint(f"[输出] 数据分析报告: {self.outputs_dir / 'data_analysis_report.md'}")
                else:
                    _vprint("[提示] 科学家仍未按协议返回结束状态。")
            except Exception:
                _vprint("[错误] 收敛阶段科学家输出非JSON或解析失败。")

        return report_md # transcript


    def _save_report(self, md: str) -> None:
        # 使用自动计算出的绝对路径
        path = self.outputs_dir / "data_analysis_report.md"
        path.write_text(md, encoding="utf-8")
