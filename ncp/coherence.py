"""Pipeline coherence checks — goal alignment, slot health, drift detection."""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from ncp.types import AlertPayload, ConsciousBlock, Whisper

_log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CoherenceReport:
    """Result of a coherence check pass."""

    alerts: list[Whisper]
    goal_versions: dict[str, int]
    passed: bool
    sensors: list[Whisper] = field(default_factory=list)


class CoherenceChecker:
    """Lightweight pipeline health checks.

    Checks goal_version consistency, slot confidence, and drift
    against the agent's own conscious state.  All checks are best-effort:
    missing data (no store, no pipeline) produces an empty pass.
    """

    def __init__(self, store: object | None = None, turn: int = 0) -> None:
        self._store = store
        self._turn = turn

    def check(self, conscious: ConsciousBlock) -> CoherenceReport:
        alerts: list[Whisper] = []
        sensors: list[Whisper] = []
        goal_versions: dict[str, int] = {}

        goal_versions[conscious.agent_id] = conscious.goal_version
        alerts.extend(self._check_slot_health(conscious))
        sensors.extend(self._emit_drift_sensor(conscious))

        if self._store is not None and conscious.pipeline_id is not None:
            try:
                agent_versions = self._load_pipeline_goal_versions(conscious)
                goal_versions.update(agent_versions)
                alerts.extend(self._check_goal_versions(conscious, agent_versions))
            except Exception as exc:
                _log.warning("ncp.coherence.check_failed", error=str(exc))

        return CoherenceReport(
            alerts=alerts,
            goal_versions=goal_versions,
            passed=len(alerts) == 0,
            sensors=sensors,
        )

    def _check_slot_health(self, conscious: ConsciousBlock) -> list[Whisper]:
        alerts: list[Whisper] = []
        if conscious.slot_age > 5 and conscious.slot_confidence < 0.5:
            alerts.append(
                Whisper(
                    from_agent="ncp_system",
                    target=conscious.agent_id,
                    whisper_type="alert",
                    payload=AlertPayload(alert_code="slot_confidence_low", description="review_slot"),
                    confidence=1.0,
                    pipeline_id=conscious.pipeline_id,
                )
            )
        if conscious.drift_score > 0.3:
            alerts.append(
                Whisper(
                    from_agent="ncp_system",
                    target=conscious.agent_id,
                    whisper_type="alert",
                    payload=AlertPayload(alert_code="drift_score_high", description="review_intent_anchor"),
                    confidence=1.0,
                    pipeline_id=conscious.pipeline_id,
                )
            )
        return alerts

    def _check_goal_versions(self, conscious: ConsciousBlock, agent_versions: dict[str, int]) -> list[Whisper]:
        all_versions = {**agent_versions, conscious.agent_id: conscious.goal_version}
        max_version = max(all_versions.values()) if all_versions else conscious.goal_version
        alerts: list[Whisper] = []
        if conscious.goal_version < max_version:
            alerts.append(
                Whisper(
                    from_agent="ncp_system",
                    target=conscious.agent_id,
                    whisper_type="alert",
                    payload=AlertPayload(
                        alert_code="goal_version_mismatch",
                        description=f"local:v{conscious.goal_version} pipeline_max:v{max_version}",
                    ),
                    confidence=1.0,
                    pipeline_id=conscious.pipeline_id,
                )
            )
        return alerts

    def _emit_drift_sensor(self, conscious: ConsciousBlock) -> list[Whisper]:
        # Drift state is visible in the ConsciousBlock itself; no whisper needed.
        # Only persist the time-series reading to the store.
        self._log_drift_history(conscious)
        return []

    def _log_drift_history(self, conscious: ConsciousBlock) -> None:
        store = self._store
        if store is None or conscious.pipeline_id is None:
            return
        if hasattr(store, "log_drift_history"):
            try:
                store.log_drift_history(
                    session_id=conscious.pipeline_id,
                    turn=self._turn,
                    drift_score=conscious.drift_score,
                )
            except Exception:
                pass

    def _load_pipeline_goal_versions(self, conscious: ConsciousBlock) -> dict[str, int]:
        versions: dict[str, int] = {}
        store = self._store
        if hasattr(store, "get_pipeline_goal_versions"):
            method = store.get_pipeline_goal_versions
            return method(pipeline_id=conscious.pipeline_id, current_agent=conscious.agent_id)
        return versions
