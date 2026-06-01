"""D3 dead-end prevention benchmark."""

from __future__ import annotations

from benchmarks.mace.harness.pipeline import MACEPipeline


class D3DeadEndPrevention:
    """Check whether deterministic agents retry known failed paths."""

    def __init__(self, known_dead_ends: list[str] | None = None) -> None:
        self.known_dead_ends = known_dead_ends or ["api/v1", "api/v2", "oauth_basic"]
        self.retry_results: dict[str, object] = {}

    def score_output(self, agent_output: str, attempted_paths: list[str] | None = None) -> dict[str, object]:
        retried: list[str] = []
        if attempted_paths is None:
            # No structured paths provided — treat as no-memory: all dead ends retried
            retried = list(self.known_dead_ends)
        else:
            attempted = {path.lower() for path in attempted_paths}
            retried = [path for path in self.known_dead_ends if path.lower() in attempted]
        score = 1.0 - (len(retried) / len(self.known_dead_ends))
        self.retry_results = {
            "known_dead_ends": self.known_dead_ends,
            "retried": retried,
            "not_retried": [path for path in self.known_dead_ends if path not in retried],
        }
        return {
            "dimension": "D3_deadend_prevention",
            "score": round(score, 4),
            "retried_count": len(retried),
            "total_dead_ends": len(self.known_dead_ends),
            "detail": self.retry_results,
        }

    def baseline_score(self) -> dict[str, object]:
        return {
            "dimension": "D3_deadend_prevention",
            "score": 0.0,
            "retried_count": len(self.known_dead_ends),
            "total_dead_ends": len(self.known_dead_ends),
            "note": "baseline assumes no dead-end memory",
        }

    def run(self, pipeline: MACEPipeline, task: dict[str, object]) -> dict[str, object]:
        critic = pipeline.run_turn(
            agent_id="critic",
            turn_n=2,
            task=str(task["description"]),
            goal=str(task["initial_goal"]),
            tried=self.known_dead_ends,
            failed=["timeout", "404_not_found", "auth_rejected"],
            query_text="known failed auth path timeout 404 auth rejected",
        )
        executor = pipeline.run_turn(
            agent_id="executor",
            turn_n=2,
            task=str(task["description"]),
            goal=str(task["initial_goal"]),
            tried=self.known_dead_ends,
            failed=["timeout", "404_not_found", "auth_rejected"],
            query_text="known failed auth path timeout 404 auth rejected",
        )
        result = self.score_output(
            f"{critic.output.summary}\n{executor.output.summary}",
            attempted_paths=[*critic.output.attempted_paths, *executor.output.attempted_paths],
        )
        result["trace"] = {
            "critic_output": critic.output.summary,
            "executor_output": executor.output.summary,
            "critic_context": critic.context,
            "executor_context": executor.context,
        }
        return result
