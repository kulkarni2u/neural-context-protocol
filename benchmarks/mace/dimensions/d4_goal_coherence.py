"""D4 goal-coherence benchmark."""

from __future__ import annotations

from ncp.types import AlertPayload, ConsciousBlock, Whisper

from benchmarks.mace.harness.pipeline import MACEPipeline
from benchmarks.mace.harness.scoring import clamp_score


class D4GoalCoherence:
    """Measure turns to full goal-version coherence after a goal change."""

    NEW_GOAL_VERSION = 2

    def __init__(self, *, goal_change_turn: int = 20) -> None:
        self.goal_change_turn = goal_change_turn
        self.coherence_log: list[dict[str, object]] = []
        self.change_fired_at = 0

    def fire_goal_change(self, pipeline: MACEPipeline, *, turn_n: int, new_goal: str) -> None:
        self.change_fired_at = turn_n
        pipeline.whisper_bus.emit_whisper(
            Whisper(
                from_agent="orchestrator",
                target="*",
                whisper_type="alert",
                payload=AlertPayload(
                    alert_code="goal_changed",
                    description=f"goal_version:{self.NEW_GOAL_VERSION} new_goal:{new_goal[:80]}",
                ),
                confidence=1.0,
                pipeline_id=pipeline.pipeline_id,
            )
        )

    def record_agent_state(self, *, turn_n: int, agent_id: str, conscious: ConsciousBlock) -> None:
        if turn_n >= self.goal_change_turn:
            self.coherence_log.append(
                {
                    "turn": turn_n,
                    "agent_id": agent_id,
                    "goal_version": conscious.goal_version,
                    "updated": conscious.goal_version == self.NEW_GOAL_VERSION,
                }
            )

    def baseline_score(self) -> dict[str, object]:
        return {
            "dimension": "D4_goal_coherence",
            "score": 0.0,
            "turns_to_full_coherence": None,
            "note": "baseline has no goal propagation mechanism",
        }

    def score(self, agents: list[str]) -> dict[str, object]:
        if not self.coherence_log:
            return {
                "dimension": "D4_goal_coherence",
                "score": 0.0,
                "note": "no coherence data recorded",
            }

        turns_after_change = sorted({int(row["turn"]) for row in self.coherence_log if int(row["turn"]) >= self.goal_change_turn})
        turns_to_coherence: int | None = None
        for turn in turns_after_change:
            turn_records = [row for row in self.coherence_log if int(row["turn"]) == turn]
            agents_updated = {str(row["agent_id"]) for row in turn_records if bool(row["updated"])}
            if set(agents).issubset(agents_updated):
                turns_to_coherence = turn - self.change_fired_at
                break

        score = 0.0 if turns_to_coherence is None else clamp_score(1.0 - (turns_to_coherence - 1) / 5)
        return {
            "dimension": "D4_goal_coherence",
            "score": round(score, 4),
            "goal_change_at_turn": self.change_fired_at,
            "turns_to_full_coherence": turns_to_coherence,
            "coherence_log_summary": self._summarize_log(agents),
            "trace": self.coherence_log,
        }

    def _summarize_log(self, agents: list[str]) -> dict[str, dict[str, int | None]]:
        summary: dict[str, dict[str, int | None]] = {}
        for agent in agents:
            agent_records = [row for row in self.coherence_log if row["agent_id"] == agent]
            first_updated = next((int(row["turn"]) for row in agent_records if bool(row["updated"])), None)
            summary[agent] = {
                "first_updated_at_turn": first_updated,
                "turns_after_change": first_updated - self.change_fired_at if first_updated is not None else None,
            }
        return summary

    def run(self, pipeline: MACEPipeline, task: dict[str, object], *, turns: int, agents: list[str]) -> dict[str, object]:
        current_goal = str(task["initial_goal"])
        goal_version = 1
        pending_goal = str(task["goal_change"]["new_goal"])  # type: ignore[index]
        for turn_n in range(1, turns + 1):
            outcomes = pipeline.run_round(
                turn_n=turn_n,
                task=str(task["description"]),
                goal=current_goal,
                goal_version=goal_version,
                query_text=f"{task['description']} goal version {goal_version}",
            )
            for outcome in outcomes:
                self.record_agent_state(turn_n=turn_n, agent_id=outcome.agent_id, conscious=outcome.conscious)
            if turn_n == self.goal_change_turn:
                self.fire_goal_change(pipeline, turn_n=turn_n, new_goal=pending_goal)
                current_goal = pending_goal
                goal_version = self.NEW_GOAL_VERSION
        return self.score(agents)
