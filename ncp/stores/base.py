"""Base store contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from functools import partial

import anyio

from ncp.types import (
    CalibrationReport,
    ChunkEdge,
    ConsolidationReport,
    ConsciousBlock,
    NCPResponse,
    OutcomeRecord,
    SubconsciousChunk,
    TurnRecord,
    Whisper,
)


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
        fallback_to_trust_recency: bool = False,
        as_of: float | None = None,
    ) -> list[SubconsciousChunk]:
        """Query stored chunks by text relevance.

        ``retrieval_mode`` controls the scoring strategy:
        - ``"hybrid"`` (default): BM25 + recency + trust weighted sum.
        - ``"trust_recency"``: recency + trust only; BM25 and the
          term-overlap filter are skipped.  Use this for non-BM25
          backends that perform their own similarity search.
        - ``"vector"``: cosine search using stored embeddings.
          Requires ``embedding`` to be provided or an embedding adapter
          configured.  Pgvector uses ANN; SQLite uses a brute-force scan.

        ``diversity_limit`` caps the number of results per author
        (``written_by``).  Default 2 preserves existing behavior.

        ``as_of`` (CAP-C5, epoch seconds): when ``None`` (default), returns
        the currently-valid view -- excludes chunks that are superseded or
        whose ``valid_to`` has passed. When given, returns the bi-temporal
        view as of that transaction time: only chunks recorded (written) by
        then, not superseded by a chunk recorded by then, and valid
        (``valid_from``/``valid_to``) at that instant.
        """

    @abstractmethod
    def get_working_zone(
        self,
        *,
        pipeline_id: str | None = None,
        layer: str | None = None,
        as_of: float | None = None,
    ) -> Sequence[SubconsciousChunk]:
        """Return working-zone chunks, optionally filtered.

        See ``query`` for ``as_of`` (CAP-C5 bi-temporal view) semantics.
        """

    def get_chunks_by_ids(
        self, ids: Sequence[str], *, as_of: float | None = None
    ) -> list[SubconsciousChunk]:
        """Fetch live (non-tombstoned) chunks by exact ids, any order.

        Used for 1-hop edge expansion over chunk relationships
        (``caused_by``, etc.). Backends that do not implement this return
        an empty list, which disables edge expansion gracefully.

        See ``query`` for ``as_of`` (CAP-C5 bi-temporal view) semantics.
        """
        return []

    def supersede(
        self,
        old_chunk_id: str,
        new_chunk_id: str,
        *,
        valid_to: float | None = None,
    ) -> bool:
        """CAP-C5: mark ``old_chunk_id`` as honestly replaced by ``new_chunk_id``.

        Sets ``old.superseded_by = new_chunk_id`` and ``old.valid_to =
        valid_to`` (the caller resolves ``valid_to`` to the new chunk's
        ``valid_from`` or "now"). The old chunk is never deleted -- this is
        the bi-temporal honesty story: supersession is recorded, not
        overwritten. Returns True if a row was found and updated. Backends
        that do not implement this return False.
        """
        return False

    async def async_supersede(
        self,
        old_chunk_id: str,
        new_chunk_id: str,
        *,
        valid_to: float | None = None,
    ) -> bool:
        """Asynchronously mark a chunk as superseded."""
        return await anyio.to_thread.run_sync(
            partial(self.supersede, old_chunk_id, new_chunk_id, valid_to=valid_to)
        )

    def record_dissent(self, chunk_id: str) -> bool:
        """Record that ``chunk_id`` was disputed (e.g. by a dissent whisper).

        Increments the chunk's dissent counter so feedback calibration can apply
        a trust penalty and propagate it along ``caused_by`` edges. Best-effort:
        backends that do not implement this return False.
        """
        return False

    def record_outcome(self, outcome: OutcomeRecord) -> bool:
        """Persist a task outcome for outcome-calibrated reputation (CAP-T3).

        Accepts an ``OutcomeRecord`` with either ``turn_id`` (will resolve to
        the chunks that turn wrote + retrieved) or ``chunk_ids``.  The outcome
        evidence feeds into calibration as the primary trust signal in place of
        retrieval counts.  Backends that do not implement this return False.
        """
        return False

    async def async_record_outcome(self, outcome: OutcomeRecord) -> bool:
        """Asynchronously record a task outcome."""
        return await anyio.to_thread.run_sync(self.record_outcome, outcome)

    # ------------------------------------------------------------------
    # Graph engineering: typed chunk_edges substrate

    def add_chunk_edges(self, edges: Sequence[ChunkEdge]) -> int:
        """Upsert typed edges between chunks. Returns the number of edges written.

        Idempotent on ``(src_chunk_id, dst_chunk_id, edge_type)``: re-adding
        an existing edge updates its ``weight``/``created_at``/``created_by``
        rather than duplicating it. Edges are a purely additive accelerator
        over the legacy ``caused_by``/``supersedes`` scalar columns, so
        backends that do not implement a typed edge graph return 0.
        """
        return 0

    def get_chunk_edges(
        self,
        chunk_ids: Sequence[str],
        *,
        edge_types: Sequence[str] | None = None,
        direction: str = "out",
        limit: int = 200,
    ) -> list[ChunkEdge]:
        """Return typed edges touching ``chunk_ids``.

        ``direction`` controls which endpoint must be in ``chunk_ids``:
        ``"out"`` (default) matches edges whose ``src_chunk_id`` is in the
        set, ``"in"`` matches ``dst_chunk_id``, ``"both"`` matches either.
        ``edge_types`` optionally restricts to a subset of the closed edge
        type set. Backends that do not implement this return an empty list,
        which disables graph traversal gracefully.
        """
        return []

    def graph_data(
        self,
        *,
        pipeline_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, object]:
        """Return structured node/edge data for graph export (``ncp graph``).

        Returns a dict with ``nodes`` (chunks, scoped to ``pipeline_id`` when
        given) and ``edges`` (typed ``chunk_edges`` rows plus legacy
        ``caused_by``/``supersedes`` column links for chunks that predate the
        edge table, deduplicated against real edge rows). Backends that do
        not implement this return empty nodes/edges.
        """
        return {"nodes": [], "edges": []}

    async def async_add_chunk_edges(self, edges: Sequence[ChunkEdge]) -> int:
        """Asynchronously upsert typed chunk edges using thread pool."""
        return await anyio.to_thread.run_sync(partial(self.add_chunk_edges, edges))

    async def async_get_chunk_edges(
        self,
        chunk_ids: Sequence[str],
        *,
        edge_types: Sequence[str] | None = None,
        direction: str = "out",
        limit: int = 200,
    ) -> list[ChunkEdge]:
        """Asynchronously fetch typed chunk edges using thread pool."""
        fn = partial(
            self.get_chunk_edges,
            chunk_ids,
            edge_types=edge_types,
            direction=direction,
            limit=limit,
        )
        return await anyio.to_thread.run_sync(fn)

    async def async_graph_data(
        self,
        *,
        pipeline_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, object]:
        """Asynchronously build graph export data using thread pool."""
        fn = partial(self.graph_data, pipeline_id=pipeline_id, limit=limit)
        return await anyio.to_thread.run_sync(fn)

    # ------------------------------------------------------------------
    # CAP-C3: Memoization

    def record_memo(
        self,
        signature: str,
        task: str,
        chunk_ids: list[str],
        result_summary: str | None = None,
        output_tokens_est: int | None = None,
    ) -> bool:
        """Persist a work memo for CAP-C3 semantic memoization.

        ``output_tokens_est`` records how many output tokens a memo hit is
        estimated to save; when None, backends estimate it from the stored
        result via ``ncp.tokens.estimate_tokens``. Returns True on success.

        Re-recording an existing ``signature`` is an upsert of the task/
        content fields only: ``created_at``, ``hit_count``, ``last_hit_at``,
        ``outcome``, and ``verified`` are preserved rather than reset, so a
        memo doesn't lose its accumulated telemetry or review state just
        because the same work was recorded again.

        Backends that do not implement this return False.
        """
        return False

    def lookup_memo(self, signature: str) -> dict | None:
        """Return a memo entry if found and not stale, or None.

        This unconditionally counts a hit whenever a fresh row is found — it
        has no notion of any downstream usability gating (e.g. outcome/verified
        thresholds applied by a caller). Callers that apply further validation
        before deciding whether a memo is actually *usable* should instead use
        ``peek_memo`` (no telemetry side effects) followed by an explicit
        ``record_memo_hit``/``record_memo_miss`` once that decision is made, so
        hit/miss telemetry only reflects memos actually returned to the caller.

        Backends that do not implement this return None.
        """
        return None

    def peek_memo(self, signature: str) -> dict | None:
        """Fetch a memo entry (applying staleness filtering) without touching telemetry.

        Returns None if the signature is unknown or the entry is stale. Does
        not increment hit_count/last_hit_at nor the miss counter — pair this
        with ``record_memo_hit``/``record_memo_miss`` so telemetry is only
        recorded once the caller has decided whether the memo is usable.
        Backends that do not implement this return None.
        """
        return None

    def record_memo_hit(self, signature: str) -> None:
        """Increment hit_count/last_hit_at for a memo actually returned as usable.

        Backends that do not implement memoization are no-ops.
        """
        return None

    def record_memo_miss(self) -> None:
        """Increment the S4.1 miss counter for a lookup that yielded no usable memo.

        Backends that do not implement memoization are no-ops.
        """
        return None

    def update_memo_outcome(self, signature: str, outcome: float, verified: bool = False) -> bool:
        """Update outcome and verified flag on an existing memo entry.

        Returns True if the row was found and updated. Backends that do
        not implement this return False.
        """
        return False

    def memo_stats(self) -> dict[str, int]:
        """Aggregate memoization telemetry (S4.1).

        Returns ``hits`` (total per-entry hit_count), ``misses`` (lookups
        that returned None), ``entry_count``, and ``estimated_tokens_saved``
        — an estimate computed as SUM(hit_count * output_tokens_est).
        Backends that do not implement memoization return zeros.
        """
        return {"hits": 0, "misses": 0, "entry_count": 0, "estimated_tokens_saved": 0}

    async def async_memo_stats(self) -> dict[str, int]:
        """Asynchronously fetch aggregate memoization telemetry."""
        return await anyio.to_thread.run_sync(self.memo_stats)

    async def async_record_memo(
        self,
        signature: str,
        task: str,
        chunk_ids: list[str],
        result_summary: str | None = None,
        output_tokens_est: int | None = None,
    ) -> bool:
        """Asynchronously record a work memo."""
        return await anyio.to_thread.run_sync(
            self.record_memo, signature, task, chunk_ids, result_summary, output_tokens_est
        )

    async def async_lookup_memo(self, signature: str) -> dict | None:
        """Asynchronously look up a memo by signature."""
        return await anyio.to_thread.run_sync(self.lookup_memo, signature)

    async def async_peek_memo(self, signature: str) -> dict | None:
        """Asynchronously fetch a memo without touching hit/miss telemetry."""
        return await anyio.to_thread.run_sync(self.peek_memo, signature)

    async def async_record_memo_hit(self, signature: str) -> None:
        """Asynchronously mark a memo as a counted hit."""
        await anyio.to_thread.run_sync(self.record_memo_hit, signature)

    async def async_record_memo_miss(self) -> None:
        """Asynchronously increment the miss counter."""
        await anyio.to_thread.run_sync(self.record_memo_miss)

    async def async_update_memo_outcome(self, signature: str, outcome: float, verified: bool = False) -> bool:
        """Asynchronously update memo outcome."""
        return await anyio.to_thread.run_sync(self.update_memo_outcome, signature, outcome, verified)

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
    def acknowledge_whispers(self, whisper_ids: Sequence[str], *, agent_id: str | None = None) -> int:
        """Delete already-processed whispers by id. Returns count deleted."""

    @abstractmethod
    def whisper_pending(self, whisper_id: str) -> bool:
        """Return True if the whisper is still queued and not yet expired."""

    # ------------------------------------------------------------------
    # Turn / cost / conscious logging

    @abstractmethod
    def log_turn_record(self, record: TurnRecord) -> None:
        """Persist a turn record for recent-ref resolution."""

    @abstractmethod
    def resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        """Resolve a recent ref like ``r:sub/<turn_id>``."""

    def recent_turns(self, *, pipeline_id: str | None, limit: int = 20) -> list[TurnRecord]:
        """CAP-T5: return up to ``limit`` recent turn records, oldest-first.

        Backs computed drift (``ncp/drift.py``). Default no-op implementation
        for stores that have not opted in -- computed drift then correctly
        degrades to ``0.0`` (no observable history) rather than erroring.
        """
        return []

    @abstractmethod
    def log_conscious(self, conscious: ConsciousBlock, *, snapshot_hash: str) -> None:
        """Persist a conscious-block snapshot for audit and goal-version tracking."""

    def load_latest_conscious(self, *, pipeline_id: str | None, agent_id: str) -> ConsciousBlock | None:
        """Return the latest conscious snapshot for an agent in a pipeline, if present."""
        return None

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
        propagation_factor: float = 0.5,
        dissent_weight: float = 0.2,
        propagation_max_hops: int = 1,
    ) -> CalibrationReport:
        """Re-score base_trust on existing chunks.

        Modes:
        - Manual override: provide chunk_id + trust to set a specific chunk's base_trust.
        - Batch decay: provide pipeline_id to apply decay to eligible chunks (age >
          recency_half_life_seconds, base_trust > 0.5, generation == 0). Chunks with
          src == "user_verified" are always protected.
        - Feedback: apply a net trust delta per chunk (retrieval boost minus dissent
          penalty) and propagate a fraction (``propagation_factor``) of it up to
          ``propagation_max_hops`` hops along ``caused_by`` ancestry (default 1
          hop, matching legacy behavior).
        """

    def query_precedents(
        self,
        query: str,
        *,
        pipeline_id: str | None = None,
        k: int = 5,
        tags: list[str] | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, object]]:
        """Query past decision traces by relevance, optionally filtered by tags and outcome.

        Returns a list of dicts with: chunk_id, decision, rationale, alternatives,
        outcome, evidence_refs, tags, base_trust, created_at, caused_by,
        retrieval_count, dissent_count.
        """
        raise NotImplementedError("query_precedents not implemented for this backend")

    def trust_drift_data(
        self,
        *,
        pipeline_id: str | None = None,
        top_k: int = 10,
    ) -> dict[str, object]:
        """Return structured trust-drift observability data.

        Returns a dict with:
        - trust_distribution: list of {band, count, avg_trust}
        - rising: top_k chunks ordered by retrieval_count DESC
        - falling: top_k chunks ordered by dissent_count DESC
        - feedback_summary: {total_chunks, with_retrievals, with_dissents,
          net_positive, net_negative, untouched}
        - drift_timeline: recent drift_history readings (if available)
        """
        raise NotImplementedError("trust_drift_data not implemented for this backend")

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
        as_of: float | None = None,
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
            as_of=as_of,
        )
        return await anyio.to_thread.run_sync(fn)

    async def async_emit_whisper(self, whisper: Whisper) -> None:
        """Asynchronously persist a whisper using thread pool."""
        await anyio.to_thread.run_sync(self.emit_whisper, whisper)

    async def async_record_dissent(self, chunk_id: str) -> bool:
        """Asynchronously record a dissent against a chunk using thread pool."""
        return await anyio.to_thread.run_sync(self.record_dissent, chunk_id)

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

    async def async_acknowledge_whispers(
        self,
        whisper_ids: Sequence[str],
        *,
        agent_id: str | None = None,
    ) -> int:
        """Asynchronously acknowledge queued whispers using thread pool."""
        fn = partial(self.acknowledge_whispers, whisper_ids, agent_id=agent_id)
        return await anyio.to_thread.run_sync(fn)

    async def async_log_turn_record(self, record: TurnRecord) -> None:
        """Asynchronously persist a turn record using thread pool."""
        await anyio.to_thread.run_sync(self.log_turn_record, record)

    async def async_resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        """Asynchronously resolve a recent ref using thread pool."""
        return await anyio.to_thread.run_sync(self.resolve_recent_ref, ref)

    async def async_recent_turns(self, *, pipeline_id: str | None, limit: int = 20) -> list[TurnRecord]:
        """Asynchronously fetch recent turn records using thread pool."""
        fn = partial(self.recent_turns, pipeline_id=pipeline_id, limit=limit)
        return await anyio.to_thread.run_sync(fn)

    async def async_log_drift_history(self, *, session_id: str, turn: int, drift_score: float) -> None:
        """Asynchronously persist a drift sensor reading using thread pool."""
        fn = partial(self.log_drift_history, session_id=session_id, turn=turn, drift_score=drift_score)
        await anyio.to_thread.run_sync(fn)

    async def async_log_conscious(self, conscious: ConsciousBlock, *, snapshot_hash: str) -> None:
        """Asynchronously persist a conscious-block snapshot using thread pool."""
        fn = partial(self.log_conscious, conscious, snapshot_hash=snapshot_hash)
        await anyio.to_thread.run_sync(fn)

    async def async_load_latest_conscious(self, *, pipeline_id: str | None, agent_id: str) -> ConsciousBlock | None:
        """Asynchronously load the latest conscious snapshot using thread pool."""
        fn = partial(self.load_latest_conscious, pipeline_id=pipeline_id, agent_id=agent_id)
        return await anyio.to_thread.run_sync(fn)

    async def async_log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        """Asynchronously persist cost telemetry using thread pool."""
        fn = partial(self.log_cost, agent_id=agent_id, response=response)
        await anyio.to_thread.run_sync(fn)

    async def async_query_precedents(
        self,
        query: str,
        *,
        pipeline_id: str | None = None,
        k: int = 5,
        tags: list[str] | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, object]]:
        """Asynchronously query precedents using thread pool."""
        fn = partial(
            self.query_precedents,
            query,
            pipeline_id=pipeline_id,
            k=k,
            tags=tags,
            outcome=outcome,
        )
        return await anyio.to_thread.run_sync(fn)

    async def async_trust_drift_data(
        self,
        *,
        pipeline_id: str | None = None,
        top_k: int = 10,
    ) -> dict[str, object]:
        """Asynchronously build trust-drift data using thread pool."""
        fn = partial(self.trust_drift_data, pipeline_id=pipeline_id, top_k=top_k)
        return await anyio.to_thread.run_sync(fn)

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
