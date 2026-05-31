"""D2 handoff-quality benchmark."""

from __future__ import annotations

from ncp.types import HandoffPayload, SubconsciousChunk, Whisper

from benchmarks.mace.harness.pipeline import MACEPipeline


class D2HandoffQuality:
    """Inject specific signals and verify they surface in downstream context."""

    def __init__(self) -> None:
        self.results: dict[str, dict[str, float | bool]] = {}
        self._h1_chunk_id: str | None = None
        self._h2_chunk_id: str | None = None

    def inject_h1_signal(self, pipeline: MACEPipeline) -> None:
        chunk = SubconsciousChunk(
            layer="episodic",
            content=(
                "oauth implementation completed approach last_resort "
                "result_confidence:0.41 result_attempts:3"
            ),
            src="agent_inferred",
            written_by="executor",
            pipeline_id=pipeline.pipeline_id,
            result_confidence=0.41,
            result_attempts=3,
            relevance=0.94,
        )
        pipeline.store.write(chunk)
        self._h1_chunk_id = chunk.chunk_id

    def inject_h2_signal(self, pipeline: MACEPipeline) -> None:
        chunk = SubconsciousChunk(
            layer="procedural",
            content="auth oauth required api_key_forbidden security_policy",
            src="user_verified",
            written_by="planner",
            pipeline_id=pipeline.pipeline_id,
            conditions=["task_involves_auth"],
            relevance=0.95,
        )
        pipeline.store.write(chunk)
        self._h2_chunk_id = chunk.chunk_id

    def inject_h3_signal(self, pipeline: MACEPipeline) -> None:
        payload = HandoffPayload(
            slice="mace_h3",
            files=["oauth_impl.py"],
            ask="approach_last_resort apis_tried_3 confidence_low review_carefully_true",
        )
        pipeline.whisper_bus.emit_whisper(
            Whisper(
                from_agent="executor",
                target="critic",
                whisper_type="share",
                payload=payload,
                confidence=0.85,
                pipeline_id=pipeline.pipeline_id,
            )
        )

    def verify_h1(self, critic_context: str) -> float:
        has_confidence = "result_confidence:0.41" in critic_context
        has_attempts = "result_attempts:3" in critic_context
        has_chunk = bool(self._h1_chunk_id and self._h1_chunk_id in critic_context)
        score = sum([has_confidence, has_attempts, has_chunk]) / 3
        self.results["h1"] = {
            "score": score,
            "has_confidence": has_confidence,
            "has_attempts": has_attempts,
            "has_chunk": has_chunk,
        }
        return score

    def verify_h2(self, executor_context: str) -> float:
        normalized = executor_context.lower()
        has_constraint = "oauth" in normalized and "api_key_forbidden" in normalized
        has_chunk = bool(self._h2_chunk_id and self._h2_chunk_id in executor_context)
        score = (float(has_constraint) + float(has_chunk)) / 2
        self.results["h2"] = {
            "score": score,
            "has_constraint": has_constraint,
            "has_chunk": has_chunk,
        }
        return score

    def verify_h3(self, critic_context: str) -> float:
        has_payload = "approach_last_resort" in critic_context and "review_carefully_true" in critic_context
        has_whisper_block = "[NCP:WHISPERS]" in critic_context
        score = (float(has_payload) + float(has_whisper_block)) / 2
        self.results["h3"] = {
            "score": score,
            "has_whisper_content": has_payload,
            "has_whisper_block": has_whisper_block,
        }
        return score

    def run(self, pipeline: MACEPipeline, task: dict[str, object]) -> dict[str, object]:
        self.inject_h1_signal(pipeline)
        self.inject_h2_signal(pipeline)
        self.inject_h3_signal(pipeline)
        critic = pipeline.run_turn(
            agent_id="critic",
            turn_n=1,
            task=str(task["description"]),
            goal=str(task["initial_goal"]),
            query_text="oauth implementation result_confidence result_attempts last_resort review carefully",
        )
        executor = pipeline.run_turn(
            agent_id="executor",
            turn_n=1,
            task=str(task["description"]),
            goal=str(task["initial_goal"]),
            query_text="auth oauth required api_key_forbidden security_policy",
        )
        h1 = self.verify_h1(critic.context)
        h2 = self.verify_h2(executor.context)
        h3 = self.verify_h3(critic.context)
        score = (h1 + h2 + h3) / 3
        return {
            "dimension": "D2_handoff_quality",
            "score": round(score, 4),
            "h1_uncertainty_propagation": round(h1, 4),
            "h2_constraint_propagation": round(h2, 4),
            "h3_whisper_delivery": round(h3, 4),
            "detail": self.results,
            "trace": {
                "critic_context": critic.context,
                "executor_context": executor.context,
            },
        }
