import math
import os
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class OptimizationStatus(Enum):
    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STAGNANT = "STAGNANT"


@dataclass
class OptimizationProposal:
    target_file: str
    target_type: str
    target_name: str
    original_code: str
    direction: str
    reasoning: str
    other_directions: Dict[str, float] = None


@dataclass
class EvolutionRecord:
    generation: int
    proposal: OptimizationProposal
    score: float
    status: OptimizationStatus


class HistoryManager:
    """管理进化历史与记忆。"""

    def __init__(self) -> None:
        self.failed_records: List[EvolutionRecord] = []
        self.best_record: Optional[EvolutionRecord] = None
        self.all_records: List[EvolutionRecord] = []
        self._trajectory_start_gen = 0

    def calculate_optimization_status(
        self, current_score: float, baseline_score: Optional[float] = None
    ) -> OptimizationStatus:
        if baseline_score is None:
            if self.best_record is not None:
                baseline_score = self.best_record.score
            else:
                return OptimizationStatus.SUCCESS

        if self.best_record is not None:
            baseline_score = max(baseline_score, self.best_record.score)

        if not math.isfinite(float(current_score)):
            return OptimizationStatus.FAILED

        fail_ratio = float(os.environ.get("MCR_FAIL_RATIO", "0.75"))
        if baseline_score > 0 and current_score < baseline_score * fail_ratio:
            return OptimizationStatus.FAILED

        improvement_threshold = 1e-6
        degradation_threshold = -1e-6
        score_diff = current_score - baseline_score

        if score_diff > improvement_threshold:
            return OptimizationStatus.SUCCESS
        if score_diff < degradation_threshold:
            return OptimizationStatus.DEGRADED
        return OptimizationStatus.STAGNANT

    def add_record(self, record: EvolutionRecord) -> None:
        self.all_records.append(record)
        if record.status in {
            OptimizationStatus.FAILED,
            OptimizationStatus.DEGRADED,
            OptimizationStatus.STAGNANT,
        }:
            self.failed_records.append(record)

        if record.status == OptimizationStatus.SUCCESS:
            if self.best_record is None or record.score > self.best_record.score:
                self.best_record = record
        elif self.best_record is None:
            self.best_record = record

    def perform_reset(self, current_gen: int) -> None:
        self._trajectory_start_gen = current_gen

    def _format_record(self, record: EvolutionRecord) -> str:
        return (
            f"Generation {record.generation} | Status={record.status.value} | "
            f"Score={record.score:.4f} | Target={record.proposal.target_file}:{record.proposal.target_name} | "
            f"Most effective strategy this generation={record.proposal.direction} | "
            f"Other strategies this generation={record.proposal.other_directions}"
        )

    def get_critic_prompt(self) -> str:
        sections: List[str] = []

        if self.best_record:
            sections.append(
                "[Global SOTA]\n"
                + self._format_record(self.best_record)
                + f"\nKey Insight: {self.best_record.proposal.reasoning}"
            )
        else:
            sections.append("[Global SOTA]\nNo successful experience yet. Please boldly explore new optimization directions.")

        if self.failed_records:
            failed_text = "\n".join(self._format_record(r) for r in self.failed_records)
            sections.append("[Failed Lessons]\n" + failed_text)
        else:
            sections.append("[Failed Lessons]\nNo failure examples available.")

        recent = [
            record
            for record in self.all_records
            if record.generation >= self._trajectory_start_gen
        ]
        if recent:
            recent_text = "\n".join(self._format_record(r) for r in recent)
            sections.append("[Recent Trajectory]\n" + recent_text)
        else:
            sections.append("[Recent Trajectory]\nNo short-term trajectory formed yet.")

        return "\n\n".join(sections)

