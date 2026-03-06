import asyncio
import json
import os
import sys
import uuid
from typing import Any, Dict
try:
    from json_repair import repair_json  # type: ignore
except Exception:  # pragma: no cover
    def repair_json(text: str) -> str:
        return text

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
 
from LLM_MODULE.llm import OpenAILLM
from LLM_MODULE.tools import load_plugins, make_tool_handler, tool_spec
from tree_cot.utils import extract_code_with_structures, generate_project_tree_text
from user_input import DEMAND, PROJECT_ROOT, TASK_METRIC, TASK_TYPE,PROPOSAL_COUNT, N_GENERATED_VARIANTS 

from mcretro_mas.engine_agents import (
    MonteCarloEngineerAgent,
    build_critic_agent_sys_prompt,
    build_messages,
    compute_critic_temperature,
)
from mcretro_mas.engine_history import EvolutionRecord, HistoryManager, OptimizationProposal, OptimizationStatus
from mcretro_mas.engine_logging import log_dialog, log_event
from mcretro_mas.engine_optimizer import AutoOptimizer

def find_original_code(results, target_name, target_type):
    for result in results:
        if result["code_name"] == target_name and result["type"] == target_type:
            return result["code_source"]
    return None


def main() -> None:
    load_dotenv()

    # Ensure tool plugins are registered before building tool specs.
    load_plugins(on_error="skip")

    root_path = PROJECT_ROOT
    project_root = PROJECT_ROOT
    tree_text = generate_project_tree_text(str(project_root))

    generation = 0
    counts = int(os.environ.get("MCR_COUNTS", "3"))
    ignore_list = ["__pycache__", "*.pyc", ".git", "venv", "data", "datasets", "logs", ".idea"]

    #llm = OpenAILLM(model=os.environ.get("MCR_MODEL", "gpt-4.1-mini"))
    critic_llm = OpenAILLM(model=os.environ.get("MCR_CRITIC_MODEL", "gpt-4.1-mini"))
    engineer_llm = OpenAILLM(model=os.environ.get("MCR_ENGINEER_MODEL", "gpt-4.1-mini"))
    history = HistoryManager()
    optimizer = AutoOptimizer(
        project_root=project_root,
        entry_point="main.py",
        timeout=int(os.environ.get("MCR_TIMEOUT", "300")),
        ignore_list=ignore_list,
        history_dir="./optimization_history",
    )

    log_event(
        "engine_config",
        cwd=os.getcwd(),
        root_path=root_path,
        project_root=project_root,
        entry_point="main.py",
        timeout=int(os.environ.get("MCR_TIMEOUT", "300")),
        counts=counts,
        ignore_list=ignore_list,
        llm_model=getattr(critic_llm, "model", None) if hasattr(critic_llm, "model") else str(type(critic_llm)),
    )

    critic_tools = tool_spec(toolset="memory_tools")
    tool_handler = make_tool_handler(toolset="memory_tools", enforce_policy=True)
    critic_system_prompt = build_critic_agent_sys_prompt(TASK_METRIC,TASK_TYPE,PROPOSAL_COUNT)

    baseline_score, baseline_output_log = optimizer.get_baseline_score()
    log_event("engine_run_start", generation=generation, baseline_score=baseline_score)

    stagnation_streak = 0
    reset_streak = 0
    for loop_index in range(counts):
        log_event(
            "generation_start",
            loop_index=loop_index,
            generation=generation,
            baseline_score=baseline_score,
            baseline_output_log_chars=len(baseline_output_log or ""),
        )

        critic_base_temperature = float(os.environ.get("MCR_CRITIC_BASE_TEMPERATURE", "0.2"))
        critic_temp_increment = float(os.environ.get("MCR_CRITIC_TEMP_INCREMENT", "0.2"))
        critic_temp_min = float(os.environ.get("MCR_CRITIC_TEMP_MIN", "0.0"))
        critic_temp_max = float(os.environ.get("MCR_CRITIC_TEMP_MAX", "1.2"))
        critic_temperature = compute_critic_temperature(
            stagnation_streak=stagnation_streak,
            base_temperature=critic_base_temperature,
            increment_per_gen=critic_temp_increment,
            min_temperature=critic_temp_min,
            max_temperature=critic_temp_max,
        )

        msgs = build_messages(
            tree_text=tree_text,
            history_prompt=history.get_critic_prompt(),
            baseline_log=baseline_output_log,
            demand=DEMAND,
            critic_system_prompt=critic_system_prompt,
            stagnation_streak=stagnation_streak,
        )

        log_event(
            "critic_chat_start",
            loop_index=loop_index,
            generation=generation,
            messages_count=len(msgs),
            stagnation_streak=stagnation_streak,
            critic_temperature=critic_temperature,
        )
        reply = critic_llm.chat_with_tools(
            msgs,
            tools=critic_tools,
            tool_handler=tool_handler,
            temperature=critic_temperature,
        )
        log_event("critic_chat_done", loop_index=loop_index, generation=generation, reply_chars=len(reply or ""))
        log_dialog("critic.assistant", reply or "", loop_index=loop_index, generation=generation)

        try:
            payload = json.loads(reply)
        except Exception as e:
            log_event("critic_reply_json_error", loop_index=loop_index, generation=generation, error=str(e))
            try:
                repaired = repair_json(reply)
                payload = json.loads(repaired) if isinstance(repaired, str) else repaired
            except Exception as repair_e:
                log_event("critic_reply_json_repair_failed", loop_index=loop_index, generation=generation, error=str(repair_e))
                stagnation_streak += 1
                generation += 1
                continue

        generation_has_improvement = False
        for proposal in payload.get("proposals", []):
            target_file = proposal["target_file"]
            target_type = proposal["target_type"]
            target_name = proposal["target_name"]

            target_info = {"file": target_file, "type": target_type, "name": target_name}
            code_blocks = extract_code_with_structures(root_path + target_file)

            generation_baseline_score = baseline_score
            best_direction = None
            best_direction_score = float("-inf")
            best_result = None
            best_candidate_code = ""
            best_reasoning = ""
            all_other_directions: Dict[str, float] = {}

            for i, modification in enumerate(proposal.get("modifications", []), start=1):
                direction_key = f"direction_{i}"
                reasoning_key = f"reasoning_{i}"
                direction = modification.get(direction_key, "Unknown Direction")
                reasoning = modification.get(reasoning_key, "No reasoning provided")

                original_code = find_original_code(code_blocks, target_name, target_type) or ""
                optimization_proposal = OptimizationProposal(
                    target_file=target_file,
                    target_type=target_type,
                    target_name=target_name,
                    original_code=original_code,
                    direction=direction,
                    reasoning=reasoning,
                )

                engineer = MonteCarloEngineerAgent(engineer_llm)
                candidates = asyncio.run(engineer.generate_variants(optimization_proposal, n=N_GENERATED_VARIANTS))

                best_result_current_direction = optimizer.run(
                    target_info=target_info,
                    code_candidates=candidates,
                    current_best_score=generation_baseline_score,
                    max_workers=N_GENERATED_VARIANTS,
                )
                direction_score = (
                    best_result_current_direction["score"] if best_result_current_direction else float("-inf")
                )

                if direction_score > best_direction_score:
                    if best_direction is not None:
                        all_other_directions[best_direction] = best_direction_score
                    best_direction = direction
                    best_direction_score = direction_score
                    best_reasoning = reasoning
                    best_result = best_result_current_direction
                    if best_result_current_direction and best_result_current_direction.get("candidate"):
                        best_candidate_code = best_result_current_direction["candidate"].get("code", "") or ""
                else:
                    all_other_directions[direction] = direction_score

            if best_result and best_direction_score > generation_baseline_score:
                optimizer.apply_change(best_result)
                baseline_score = best_direction_score
                if best_result.get("output"):
                    baseline_output_log = best_result.get("output") or baseline_output_log
                log_event(
                    "baseline_updated_from_improvement",
                    generation=generation,
                    baseline_score=baseline_score,
                    baseline_output_log_chars=len(baseline_output_log or ""),
                    target_file=target_file,
                    target_type=target_type,
                    target_name=target_name,
                    best_direction=best_direction,
                )

            final_optimization_proposal = OptimizationProposal(
                target_file=target_file,
                target_type=target_type,
                target_name=target_name,
                original_code=best_candidate_code,
                direction=best_direction,
                other_directions=all_other_directions,
                reasoning=best_reasoning,
            )
            status = history.calculate_optimization_status(best_direction_score, generation_baseline_score)
            evolution_record = EvolutionRecord(
                generation=generation,
                proposal=final_optimization_proposal,
                score=best_direction_score,
                status=status,
            )
            history.add_record(evolution_record)

            log_event(
                "evolution_record_added",
                generation=generation,
                status=status.value,
                score=best_direction_score,
                target_file=target_file,
                target_type=target_type,
                target_name=target_name,
                best_direction=best_direction,
                other_directions=all_other_directions,
            )

            if status == OptimizationStatus.SUCCESS:
                generation_has_improvement = True

        if generation_has_improvement:
            stagnation_streak = 0
            reset_streak = 0
        else:
            stagnation_streak += 1
            reset_streak += 1
            if reset_streak == 5:
                history.perform_reset(generation + 1)
                reset_streak_before = reset_streak
                reset_streak = 0
                log_event(
                    "trajectory_reset",
                    generation=generation,
                    next_generation=generation + 1,
                    stagnation_streak=stagnation_streak,
                    reset_streak_before=reset_streak_before,
                    reset_streak_after=reset_streak,
                )

        generation += 1
        log_event(
            "generation_done",
            loop_index=loop_index,
            generation=generation,
            stagnation_streak=stagnation_streak,
            reset_streak=reset_streak,
        )


if __name__ == "__main__":
    main()
