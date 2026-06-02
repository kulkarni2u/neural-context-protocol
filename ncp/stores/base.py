"""Base store contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from functools import partial

import anyio

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
        retrieval_mode: str = "hybrid",
        embedding: list[float] | None = None,
        diversity_limit: int = 2,
    ) -> list[SubconsciousChunk]:
        """Query stored chunks by text relevance.

        ``retrieval_mode`` controls the scoring strategy:
        - ``"hybrid"`` (default): BM25 + recency + trust weighted sum.
        - ``"trust_recency"``: recency + trust only; BM25 and the
          term-overlap filter are skipped.  Use this for non-BM25
          backends that perform their own similarity search.
        - ``"vector"``: cosine ANN search using stored embeddings.
          Requires ``embedding`` to be provided.  Only supported on
          the pgvector backend; raises ``ValueError`` on SQLite.

        ``diversity_limit`` caps the number of results per author
        (``written_by``).  Default 2 preserves existing behavior.
        """

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

    def log_drift_history(self, *, session_id: str, turn: int, drift_score: float) -> None:
        """Persist a drift sensor reading for time-series tracking."""

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

    # ------------------------------------------------------------------
    # Async Counterpart Surface

    async def async_write(self, chunk: SubconsciousChunk) -> bool:
        """Asynchronously persist a chunk using thread pool."""
        return await anyio.to_thread.run_sync(self.write, chunk)

    async def async_query(
        self,
        text: str,
        *,
        k: int = 4,
        min_score: float = 0.01,
        layer: str | None = None,
        pipeline_id: str | None = None,
        scope: str | None = None,
        zone: str = "working",
        retrieval_mode: str = "hybrid",
        embedding: list[float] | None = None,
    ) -> list[SubconsciousChunk]:
        """Asynchronously query stored chunks by text relevance using thread pool."""
        fn = partial(
            self.query,
            text,
            k=k,
            min_score=min_score,
            layer=layer,
            pipeline_id=pipeline_id,
            scope=scope,
            zone=zone,
            retrieval_mode=retrieval_mode,
            embedding=embedding,
        )
        return await anyio.to_thread.run_sync(fn)

    async def async_emit_whisper(self, whisper: Whisper) -> None:
        """Asynchronously persist a whisper using thread pool."""
        await anyio.to_thread.run_sync(self.emit_whisper, whisper)

    async def async_drain_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        """Asynchronously drain queued whispers using thread pool."""
        fn = partial(
            self.drain_whispers,
            agent_id=agent_id,
            pipeline_id=pipeline_id,
            max_items=max_items,
            min_confidence=min_confidence,
        )
        return await anyio.to_thread.run_sync(fn)

    async def async_log_turn_record(self, record: TurnRecord) -> None:
        """Asynchronously persist a turn record using thread pool."""
        await anyio.to_thread.run_sync(self.log_turn_record, record)

    async def async_resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        """Asynchronously resolve a recent ref using thread pool."""
        return await anyio.to_thread.run_sync(self.resolve_recent_ref, ref)

    async def async_log_drift_history(self, *, session_id: str, turn: int, drift_score: float) -> None:
        """Asynchronously persist a drift sensor reading using thread pool."""
        fn = partial(self.log_drift_history, session_id=session_id, turn=turn, drift_score=drift_score)
        await anyio.to_thread.run_sync(fn)

    async def async_log_conscious(self, conscious: ConsciousBlock, *, snapshot_hash: str) -> None:
        """Asynchronously persist a conscious-block snapshot using thread pool."""
        fn = partial(self.log_conscious, conscious, snapshot_hash=snapshot_hash)
        await anyio.to_thread.run_sync(fn)

    async def async_log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        """Asynchronously persist cost telemetry using thread pool."""
        fn = partial(self.log_cost, agent_id=agent_id, response=response)
        await anyio.to_thread.run_sync(fn)

    async def async_viz_data(self, *, pipeline_id: str | None = None) -> dict[str, object]:
        """Asynchronously build operator viz data using thread pool."""
        fn = partial(self.viz_data, pipeline_id=pipeline_id)
        return await anyio.to_thread.run_sync(fn)

    async def async_status_detail(self, *, pipeline_id: str | None = None) -> dict[str, object]:
        """Asynchronously build status detail using thread pool."""
        fn = partial(self.status_detail, pipeline_id=pipeline_id)
        return await anyio.to_thread.run_sync(fn)

    async def async_cost_summary(
        self,
        *,
        pipeline_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, object]:
        """Asynchronously build cost summary using thread pool."""
        fn = partial(self.cost_summary, pipeline_id=pipeline_id, limit=limit)
        return await anyio.to_thread.run_sync(fn)
