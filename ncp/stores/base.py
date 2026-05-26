"""Base store contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ncp.types import NCPResponse, SubconsciousChunk, TurnRecord, Whisper


class NCPStoreError(RuntimeError):
    """Base class for store-related failures."""


class NCPStoreUnavailableError(NCPStoreError):
    """Raised when the configured store cannot be opened or used."""


class BaseStore(ABC):
    """Abstract persistence surface for NCP runtime state."""

    @abstractmethod
    def write(self, chunk: SubconsciousChunk) -> bool:
        """Persist a chunk and return whether a new row was written."""

    @abstractmethod
    def query(
        self,
        text: str,
        *,
        k: int = 4,
        min_score: float = 0.01,
        layer: str | None = None,
        pipeline_id: str | None = None,
        scope: str | None = None,
        zone: str = "working",
    ) -> list[SubconsciousChunk]:
        """Query stored chunks by text relevance."""

    @abstractmethod
    def emit_whisper(self, whisper: Whisper) -> None:
        """Persist a whisper for later drain."""

    @abstractmethod
    def drain_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        """Drain queued whispers for an agent."""

    @abstractmethod
    def get_working_zone(
        self,
        *,
        pipeline_id: str | None = None,
        layer: str | None = None,
    ) -> Sequence[SubconsciousChunk]:
        """Return working-zone chunks, optionally filtered."""

    @abstractmethod
    def log_turn_record(self, record: TurnRecord) -> None:
        """Persist a turn record for recent-ref resolution."""

    @abstractmethod
    def resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        """Resolve a recent ref like ``r:sub/<turn_id>``."""

    @abstractmethod
    def log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        """Persist cost telemetry for one turn."""

    def log_cost_raw(
        self,
        *,
        agent_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        pipeline_id: str | None = None,
        turn_id: str,
        latency_ms: int = 0,
    ) -> None:
        """Persist raw cost telemetry without a full NCPResponse."""

    def get_pipeline_goal_versions(
        self,
        *,
        pipeline_id: str,
        current_agent: str | None = None,
    ) -> dict[str, int]:
        """Return latest goal_version for each agent in a pipeline."""
        return {}
