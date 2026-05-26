"""Base store contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ncp.types import CalibrationReport, ConsolidationReport, ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


class NCPStoreError(RuntimeError):
    """Base class for store-related failures."""


class NCPStoreUnavailableError(NCPStoreError):
    """Raised when the configured store cannot be opened or used."""


class BaseStore(ABC):
    """Abstract persistence surface for NCP runtime state.

    Every method listed here is implemented by both SQLiteStore and
    PgvectorStore.  Subclasses must implement all abstract methods.
    """

    # ------------------------------------------------------------------
    # Chunk persistence

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
    def get_working_zone(
        self,
        *,
        pipeline_id: str | None = None,
        layer: str | None = None,
    ) -> Sequence[SubconsciousChunk]:
        """Return working-zone chunks, optionally filtered."""

    # ------------------------------------------------------------------
    # Whisper queue

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
    def peek_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        """Return eligible whispers without consuming them."""

    @abstractmethod
    def acknowledge_whispers(self, whisper_ids: Sequence[str]) -> int:
        """Delete already-processed whispers by id. Returns count deleted."""

    # ------------------------------------------------------------------
    # Turn / cost / conscious logging

    @abstractmethod
    def log_turn_record(self, record: TurnRecord) -> None:
        """Persist a turn record for recent-ref resolution."""

    @abstractmethod
    def resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        """Resolve a recent ref like ``r:sub/<turn_id>``."""

    @abstractmethod
    def log_conscious(self, conscious: ConsciousBlock, *, snapshot_hash: str) -> None:
        """Persist a conscious-block snapshot for audit and goal-version tracking."""

    @abstractmethod
    def log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        """Persist cost telemetry for one turn."""

    @abstractmethod
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

    @abstractmethod
    def get_pipeline_goal_versions(
        self,
        *,
        pipeline_id: str,
        current_agent: str | None = None,
    ) -> dict[str, int]:
        """Return latest goal_version for each agent in a pipeline."""

    @abstractmethod
    def consolidate(
        self,
        *,
        pipeline_id: str | None = None,
        dry_run: bool = False,
        similarity_threshold: float = 0.65,
        trust_floor: float = 0.10,
    ) -> ConsolidationReport:
        """Merge redundant chunks and tombstone noise. Returns a report of what changed."""

    @abstractmethod
    def calibrate(
        self,
        *,
        pipeline_id: str | None = None,
        chunk_id: str | None = None,
        trust: float | None = None,
        dry_run: bool = False,
        decay_factor: float = 0.85,
        recency_half_life_seconds: float = 14400,
        feedback_mode: bool = False,
        feedback_weight: float = 0.15,
    ) -> CalibrationReport:
        """Re-score base_trust on existing chunks.

        Two modes:
        - Manual override: provide chunk_id + trust to set a specific chunk's base_trust.
        - Batch decay: provide pipeline_id to apply decay to eligible chunks (age >
          recency_half_life_seconds, base_trust > 0.5, generation == 0). Chunks with
          src == "user_verified" are always protected.
        """

    @abstractmethod
    def viz_data(self, *, pipeline_id: str | None = None) -> dict[str, object]:
        """Return structured data for the operator viz view.

        Returns a dict with these exact top-level keys:
        - chunk_distribution: list of {layer, zone, count}
        - age_brackets: list of {bracket, count, avg_trust, top_layer}
        - top_chunks: list of top 5 chunks by base_trust DESC
        - pipeline_summary: list of {pipeline_id, chunk_count, last_activity}
        - whisper_queue: {total, by_type: {type: count}}
        """
