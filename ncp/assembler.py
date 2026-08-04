"""Assembly pipeline for conscious state, retrieved chunks, and whispers.

Implements the normative 7-step assembly sequence from NCP spec §6.1.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
import json
import time

import anyio

from ncp.coherence import CoherenceChecker
from ncp.config import NCPConfig
from ncp.distill import distill_chunk
from ncp.encoder import PidginEncoder
from ncp.middleware.base import MiddlewarePipeline
from ncp.stores.base import BaseStore
from ncp.stores.retrieval import DEFAULT_RETRIEVAL_POLICY, apply_mmr_selection, build_lexical_candidates, normalize_query_terms
from ncp.tokens import estimate_tokens
from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


@dataclass(slots=True)
class AssemblyResult:
    """Structured result from one assembly pass."""

    context: str
    conscious: ConsciousBlock
    chunks: list[SubconsciousChunk]
    whispers: list[Whisper]
    pending_whisper_ids: list[str]
    evicted_high_relevance: list[tuple[str, float]]
    evicted_whispers: list[tuple[str, float]]
    evicted_chunk_count: int = 0
    evicted_whisper_count: int = 0
    retrieval_used_fallback: bool = False


class Assembler:
    """Full V1 assembler implementing the normative 7-step sequence (§6.1).

    Steps
    -----
    0. Coherence check
    1. Hydrate conscious block
    2. Resolve recent refs
    3. Hybrid subconscious retrieval (BM25 + diversity cap)
    4. Drain whisper queue
    5. Encode pidgin
    6. Call adapter (delegated to caller via ``AssemblyResult``)
    7. Post-turn async writes (``post_turn_async``)
    """

    def __init__(
        self,
        *,
        store: BaseStore,
        encoder: PidginEncoder | None = None,
        middleware: MiddlewarePipeline | None = None,
        config: NCPConfig | None = None,
    ) -> None:
        self.store = store
        self.encoder = encoder or PidginEncoder()
        self.coherence = CoherenceChecker(store=store)
        self.middleware = middleware or MiddlewarePipeline()
        self._chunk_cap_default = config.chunk_cap_default if config else 4
        self._chunk_cap_high = config.chunk_cap_high if config else 3
        self._chunk_cap_critical = config.chunk_cap_critical if config else 2
        self._recent_slot_budget = config.recent_slot_budget if config else 2
        self._context_token_budget = config.context_token_budget if config else None
        self._whisper_cap_default = config.whisper_cap_default if config else 3
        self._whisper_cap_high = config.whisper_cap_high if config else 2
        self._whisper_cap_critical = config.whisper_cap_critical if config else 1
        self._edge_expansion = config.edge_expansion_enabled if config else True
        self._edge_expansion_decay = config.edge_expansion_decay if config else 0.7
        self._edge_max_hops = config.edge_max_hops if config else 1
        self._edge_expansion_types = config.edge_expansion_types if config else ["caused_by"]
        self._diversity_lambda = config.diversity_lambda if config else 1.0
        self._distillation_enabled = config.distillation_enabled if config else False
        self._distillation_min_chunk_tokens = config.distillation_min_chunk_tokens if config else 120
        self._fallback_to_trust_recency_enabled = (
            config.fallback_to_trust_recency_enabled if config else True
        )

    # ------------------------------------------------------------------
    # Step 0-5: assemble
    # ------------------------------------------------------------------

    def _prepare_assembly(
        self,
        *,
        conscious: ConsciousBlock,
        budget: BudgetContext,
        query_text: str | None = None,
        ctx_window: int | None = None,
        k: int | None = None,
        diversity_limit: int | None = None,
        max_tokens: int | None = None,
        as_of: float | None = None,
    ) -> tuple[
        ConsciousBlock,
        BudgetContext,
        list[SubconsciousChunk],
        list[Whisper],
        list[tuple[str, float]],
        list[tuple[str, float]],
        int,
        int,
        bool,
    ]:
        conscious, budget = self.middleware.pre_assemble(conscious, budget)
        coherence_report = self.coherence.check(conscious)
        coherence_alerts = coherence_report.alerts
        hydrated = conscious if ctx_window is None else conscious.model_copy(update={"ctx_window": ctx_window})
        chunk_cap, whisper_cap = self._assembly_caps(budget=budget, k=k)
        search_text = query_text or f"{hydrated.task} {hydrated.slot}"
        recent_chunks = self._resolve_recent_refs(hydrated, query_text=search_text)
        fallback_count_before = self._retrieval_fallback_count()
        subconscious = self._retrieve_chunks(
            hydrated,
            query_text=query_text,
            budget=budget,
            k=chunk_cap,
            diversity_limit=diversity_limit,
            diversity_lambda=self._diversity_lambda,
            as_of=as_of,
        )
        # Finding 8 (docs/NCP_SILENT_DISCONNECT_AUDIT.md): when the primary
        # hybrid pass finds nothing and the trust/recency fallback fires, the
        # returned chunks have no relationship to the query -- surface that
        # so callers don't mistake "frozen" trust/recency filler for a real
        # relevance-ranked result.
        retrieval_used_fallback = self._retrieval_fallback_count() > fallback_count_before
        subconscious = self._cold_start_bootstrap(hydrated, subconscious)
        if self._edge_expansion:
            expanded = self._expand_edges([*recent_chunks, *subconscious], limit=chunk_cap, as_of=as_of)
            subconscious = [*subconscious, *expanded]
        # Superseded-fact suppression is independent of edge expansion -- it
        # must always run against whatever the current candidate set is, or a
        # retracted fact and its correction can both survive to the final
        # context whenever edge_expansion is disabled.
        recent_chunks, subconscious = self._suppress_superseded(recent_chunks, subconscious)
        deduped_chunks = self._dedupe_chunks([*recent_chunks, *subconscious])
        if self._diversity_lambda < 1.0:
            combined_chunks = self._mmr_select_chunks(
                deduped_chunks,
                query_text=search_text,
                diversity_lambda=self._diversity_lambda,
                chunk_cap=chunk_cap,
            )
        else:
            combined_chunks = self._split_recent_and_retrieved_chunks(
                recent_chunks=recent_chunks,
                retrieved_chunks=subconscious,
                chunk_cap=chunk_cap,
                budget=budget,
            )
        combined_ids = {chunk.chunk_id for chunk in combined_chunks}
        evicted_high_relevance = [
            (chunk.chunk_id, float(chunk.relevance))
            for chunk in deduped_chunks
            if chunk.chunk_id not in combined_ids and float(chunk.relevance) >= 0.5
        ]
        alert_cap = self._alert_cap(budget=budget)
        queue_whispers = self._peek_whispers(hydrated, max_items=whisper_cap)
        hydrated = self._apply_drift_feedback(hydrated, queue_whispers)
        external_alerts = coherence_alerts[:alert_cap]
        all_whispers: list[Whisper] = [*external_alerts, *queue_whispers]
        evicted_whispers = [
            (whisper.whisper_id, float(whisper.confidence))
            for whisper in [*coherence_alerts[alert_cap:], *queue_whispers[whisper_cap:]]
            if float(whisper.confidence) >= 0.6
        ]
        combined_whispers = all_whispers
        effective_max_tokens = max_tokens if max_tokens is not None else self._context_token_budget
        if effective_max_tokens is not None:
            pre_budget_chunks = combined_chunks
            pre_budget_whispers = combined_whispers
            combined_chunks, combined_whispers = self._fit_token_budget(
                conscious=hydrated,
                budget=budget,
                chunks=combined_chunks,
                whispers=combined_whispers,
                max_tokens=max(1, effective_max_tokens),
                query_text=search_text,
            )
            kept_chunk_ids = {chunk.chunk_id for chunk in combined_chunks}
            evicted_ids = {chunk_id for chunk_id, _ in evicted_high_relevance}
            evicted_high_relevance.extend(
                (chunk.chunk_id, float(chunk.relevance))
                for chunk in pre_budget_chunks
                if chunk.chunk_id not in kept_chunk_ids
                and chunk.chunk_id not in evicted_ids
                and float(chunk.relevance) >= 0.5
            )
            kept_whisper_ids = {whisper.whisper_id for whisper in combined_whispers}
            evicted_whisper_ids = {whisper_id for whisper_id, _ in evicted_whispers}
            evicted_whispers.extend(
                (whisper.whisper_id, float(whisper.confidence))
                for whisper in pre_budget_whispers
                if whisper.whisper_id not in kept_whisper_ids
                and whisper.whisper_id not in evicted_whisper_ids
                and float(whisper.confidence) >= 0.6
            )
        # Finding 2: unconditional eviction totals, independent of the
        # relevance/confidence gate above -- captures cap- and budget-fit
        # evictions even when nothing cleared the >=0.5/>=0.6 threshold.
        evicted_chunk_count = len(deduped_chunks) - len(combined_chunks)
        evicted_whisper_count = len(all_whispers) - len(combined_whispers)
        return (
            hydrated,
            budget,
            combined_chunks,
            combined_whispers,
            evicted_high_relevance,
            evicted_whispers,
            evicted_chunk_count,
            evicted_whisper_count,
            retrieval_used_fallback,
        )

    def assemble(
        self,
        *,
        conscious: ConsciousBlock,
        budget: BudgetContext,
        query_text: str | None = None,
        ctx_window: int | None = None,
        k: int | None = None,
        diversity_limit: int | None = None,
        max_tokens: int | None = None,
        as_of: float | None = None,
    ) -> AssemblyResult:
        (
            hydrated,
            budget,
            combined_chunks,
            combined_whispers,
            evicted_high_relevance,
            evicted_whispers,
            evicted_chunk_count,
            evicted_whisper_count,
            retrieval_used_fallback,
        ) = self._prepare_assembly(
            conscious=conscious,
            budget=budget,
            query_text=query_text,
            ctx_window=ctx_window,
            k=k,
            diversity_limit=diversity_limit,
            max_tokens=max_tokens,
            as_of=as_of,
        )
        context = self.encoder.assemble(
            conscious=hydrated,
            chunks=combined_chunks,
            whispers=combined_whispers,
            budget=budget,
        )
        context = self.middleware.post_assemble(context)
        return AssemblyResult(
            context=context,
            conscious=hydrated,
            chunks=combined_chunks,
            whispers=combined_whispers,
            pending_whisper_ids=[
                whisper.whisper_id
                for whisper in combined_whispers
                if whisper.whisper_type not in {"alert", "world_check", "sensor"}
            ],
            evicted_high_relevance=evicted_high_relevance,
            evicted_whispers=evicted_whispers,
            evicted_chunk_count=evicted_chunk_count,
            evicted_whisper_count=evicted_whisper_count,
            retrieval_used_fallback=retrieval_used_fallback,
        )

    def assemble_incremental(
        self,
        *,
        conscious: ConsciousBlock,
        budget: BudgetContext,
        query_text: str | None = None,
        ctx_window: int | None = None,
        max_tokens: int | None = None,
        k: int | None = None,
        diversity_limit: int | None = None,
        as_of: float | None = None,
    ) -> Iterator[tuple[str, str]]:
        """Yield (label, section_text) in priority order, enforcing max_tokens.

        Labels in order: ``conscious``, ``subconscious`` (one per chunk),
        ``whispers``, ``budget_header``. Conscious and budget sections are always emitted.
        Subconscious chunks stop yielding once max_tokens would be exceeded.
        Token count is estimated via ``ncp.tokens.estimate_tokens``.

        Note: ``middleware.post_assemble`` is NOT called on yielded sections.
        Callers that use post_assemble middleware should apply it to the
        concatenated result: ``mw.post_assemble("\\n\\n".join(t for _, t in sections))``.
        """
        hydrated, budget, combined_chunks, combined_whispers, _, _, _, _, _ = self._prepare_assembly(
            conscious=conscious,
            budget=budget,
            query_text=query_text,
            ctx_window=ctx_window,
            k=k,
            diversity_limit=diversity_limit,
            max_tokens=max_tokens,
            as_of=as_of,
        )

        conscious_text = self.encoder._encode_conscious(hydrated)
        yield "conscious", conscious_text

        if combined_chunks:
            yield "subconscious", self.encoder._encode_subconscious(combined_chunks)

        if combined_whispers:
            yield "whispers", self.encoder._encode_whispers(combined_whispers, now=None)

        budget_text = self.encoder._encode_budget(budget)
        yield "budget_header", budget_text

    def sections_from_result(
        self,
        *,
        result: AssemblyResult,
        budget: BudgetContext,
    ) -> list[tuple[str, str]]:
        """Derive labeled sections from an existing ``AssemblyResult``.

        Mirrors ``assemble_incremental``'s label order (``conscious``,
        ``subconscious``, ``whispers``, ``budget_header``), reusing the
        encoder against already-assembled data with no store access.
        """
        sections: list[tuple[str, str]] = [("conscious", self.encoder._encode_conscious(result.conscious))]

        if result.chunks:
            sections.append(("subconscious", self.encoder._encode_subconscious(result.chunks)))

        if result.whispers:
            sections.append(("whispers", self.encoder._encode_whispers(result.whispers, now=None)))

        sections.append(("budget_header", self.encoder._encode_budget(budget)))
        return sections

    def apply_post_middleware(self, text: str) -> str:
        return self.middleware.post_assemble(text)

    # ------------------------------------------------------------------
    # Step 7: post-turn
    # ------------------------------------------------------------------

    def post_turn(
        self,
        *,
        conscious: ConsciousBlock,
        response: NCPResponse,
        result_summary: str,
        result_full: str,
        memory_chunks: list[SubconsciousChunk] | None = None,
        ack_whisper_ids: list[str] | None = None,
    ) -> TurnRecord:
        record = TurnRecord(
            turn_id=response.turn_id,
            agent_id=conscious.agent_id,
            pipeline_id=conscious.pipeline_id,
            task=conscious.task,
            slot=conscious.slot,
            result=result_summary,
            result_full=result_full,
        )
        self.store.log_turn_record(record)

        updated_recent = [f"r:sub/{record.turn_id}", *conscious.recent][:5]
        updated_conscious = conscious.model_copy(update={"recent": updated_recent})
        snapshot_hash = sha256(updated_conscious.model_dump_json().encode("utf-8")).hexdigest()
        self.store.log_conscious(updated_conscious, snapshot_hash=snapshot_hash)
        self.store.log_cost(agent_id=conscious.agent_id, response=response)
        if ack_whisper_ids:
            self.store.acknowledge_whispers(ack_whisper_ids, agent_id=conscious.agent_id)

        suppressed_chunk_ids: list[str] = []
        for chunk in memory_chunks or []:
            chunk = self.middleware.pre_write(chunk)
            # WI-007(a): surface dedup-suppressed writes to the caller instead
            # of silently dropping them.
            if not self._write_with_retry(chunk):
                suppressed_chunk_ids.append(chunk.chunk_id)
        record.suppressed_chunk_ids = suppressed_chunk_ids

        return record

    async def post_turn_async(
        self,
        *,
        conscious: ConsciousBlock,
        response: NCPResponse,
        result_summary: str,
        result_full: str,
        memory_chunks: list[SubconsciousChunk] | None = None,
        ack_whisper_ids: list[str] | None = None,
    ) -> TurnRecord:
        record = TurnRecord(
            turn_id=response.turn_id,
            agent_id=conscious.agent_id,
            pipeline_id=conscious.pipeline_id,
            task=conscious.task,
            slot=conscious.slot,
            result=result_summary,
            result_full=result_full,
        )
        updated_recent = [f"r:sub/{record.turn_id}", *conscious.recent][:5]
        updated_conscious = conscious.model_copy(update={"recent": updated_recent})
        snapshot_hash = sha256(updated_conscious.model_dump_json().encode("utf-8")).hexdigest()

        # WI-007(a): start_soon cannot consume return values, so track
        # dedup-suppressed writes via a shared list populated by a closure.
        suppressed_chunk_ids: list[str] = []

        async def _write_and_track(chunk: SubconsciousChunk) -> None:
            if not await self._alog_write_with_retry(chunk):
                suppressed_chunk_ids.append(chunk.chunk_id)

        async with anyio.create_task_group() as tg:
            tg.start_soon(self._alog_turn_record, record)
            tg.start_soon(self._alog_conscious, updated_conscious, snapshot_hash)
            tg.start_soon(self._alog_cost, conscious.agent_id, response)
            if ack_whisper_ids:
                tg.start_soon(self._aacknowledge_whispers, ack_whisper_ids, conscious.agent_id)
            for chunk in memory_chunks or []:
                chunk = self.middleware.pre_write(chunk)
                tg.start_soon(_write_and_track, chunk)

        record.suppressed_chunk_ids = suppressed_chunk_ids
        return record

    # ------------------------------------------------------------------
    # Step 2: resolve recent refs
    # ------------------------------------------------------------------

    def _resolve_recent_refs(self, conscious: ConsciousBlock, *, query_text: str | None = None) -> list[SubconsciousChunk]:
        records: list[TurnRecord] = []
        for ref in conscious.recent:
            record = self.store.resolve_recent_ref(ref)
            if record is None:
                continue
            records.append(record)
        if not records:
            return []

        policy = getattr(self.store, "retrieval_policy", DEFAULT_RETRIEVAL_POLICY)
        lexical_candidates = build_lexical_candidates(query_text or "", [record.result for record in records])
        query_terms = normalize_query_terms(query_text or "")
        now = time.time()
        chunks: list[SubconsciousChunk] = []
        for record, lexical_candidate in zip(records, lexical_candidates, strict=True):
            age_seconds = max(0.0, now - record.created_at)
            lexical_signal = lexical_candidate.lexical_signal
            if lexical_signal is not None and lexical_signal <= 0.0 and query_terms:
                matched_terms = query_terms.intersection(set(lexical_candidate.doc_tokens))
                lexical_signal = len(matched_terms) / len(query_terms)
            relevance = policy.score(
                bm25_normalized=0.0 if lexical_signal is None else lexical_signal,
                age_seconds=age_seconds,
                base_trust=0.7,
                generation=0,
            )
            chunks.append(
                SubconsciousChunk(
                    chunk_id=f"recent_{record.turn_id}",
                    layer="episodic",
                    content=record.result,
                    src="subcon_retrieved",
                    pipeline_id=record.pipeline_id,
                    written_by=record.agent_id,
                    relevance=max(0.0, min(1.0, relevance)),
                    age_seconds=age_seconds,
                )
            )
        return chunks

    # ------------------------------------------------------------------
    # Step 3: hybrid subconscious retrieval
    # ------------------------------------------------------------------

    def _retrieve_chunks(
        self,
        conscious: ConsciousBlock,
        *,
        query_text: str | None,
        budget: BudgetContext | None = None,
        k: int | None = None,
        diversity_limit: int | None = None,
        diversity_lambda: float | None = None,
        as_of: float | None = None,
    ) -> list[SubconsciousChunk]:
        if k is None:
            k = 2 if (budget is not None and budget.pressure == "critical") else 4
        store_k = k * 3 if (diversity_lambda is not None and diversity_lambda < 1.0) else k
        search_text = query_text or f"{conscious.task} {conscious.slot}"
        extra: dict = {}
        if diversity_limit is not None:
            extra["diversity_limit"] = diversity_limit
        if as_of is not None:
            extra["as_of"] = as_of
        return self.store.query(
            search_text,
            k=store_k,
            pipeline_id=conscious.pipeline_id,
            zone="working",
            fallback_to_trust_recency=self._fallback_to_trust_recency_enabled,
            **extra,
        )

    def _retrieval_fallback_count(self) -> int:
        """Best-effort read of the store's trust/recency-fallback counter.

        Not every store backend implements ``retrieval_fallback_count()``
        (e.g. it's a no-op signature-only parameter on the sync pgvector
        store today), so this degrades to 0 rather than raising.
        """
        getter = getattr(self.store, "retrieval_fallback_count", None)
        return int(getter()) if callable(getter) else 0

    def _cold_start_bootstrap(
        self,
        conscious: ConsciousBlock,
        chunks: list[SubconsciousChunk],
    ) -> list[SubconsciousChunk]:
        if chunks:
            return chunks
        content = f"pipeline_summary agent:{conscious.agent_id} task:{conscious.task} intent:{conscious.intent}"
        if len(content) > 2000:
            # A legitimate long task/intent must not blow the SubconsciousChunk
            # content cap and crash the whole assemble() call -- truncate with
            # a trailing marker instead of dropping the cold-start signal.
            content = content[:1900] + "...[truncated]"
        cold_chunk = SubconsciousChunk(
            chunk_id=f"cold_{conscious.pipeline_id or 'init'}",
            layer="procedural",
            content=content,
            src="synthesis",
            written_by=conscious.agent_id,
            pipeline_id=conscious.pipeline_id,
            relevance=0.1,
        )
        return [cold_chunk]

    # ------------------------------------------------------------------
    # Step 4: drain whisper queue
    # ------------------------------------------------------------------

    def _peek_whispers(self, conscious: ConsciousBlock, *, max_items: int = 3) -> list[Whisper]:
        return self.store.peek_whispers(
            agent_id=conscious.agent_id,
            pipeline_id=conscious.pipeline_id,
            max_items=max_items,
            min_confidence=0.60,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Multi-hop edge expansion + supersession suppression
    # ------------------------------------------------------------------

    def _expand_edges(
        self,
        chunks: list[SubconsciousChunk],
        *,
        limit: int,
        as_of: float | None = None,
    ) -> list[SubconsciousChunk]:
        """Pull in up to ``edge_max_hops`` hops of typed-edge neighbors.

        The cause of a relevant chunk is often more useful than the next
        marginal lexical hit, and fetching it here saves the model an
        ``ncp_fetch`` round trip. Neighbors inherit relevance decayed by
        ``edge_expansion_decay`` per hop from the chunk that referenced them
        (hop *h* gets ``decay ** h`` of the original), so they stay
        subordinate to primary results and still compete inside the existing
        ``chunk_cap`` -- expansion never widens the bounded budget. A visited
        set makes traversal cycle-safe. At the defaults (``edge_max_hops=1``,
        ``edge_expansion_types=["caused_by"]``) this reproduces the legacy
        1-hop behavior exactly.

        Typed edges come from ``store.get_chunk_edges``; when ``caused_by``
        is among the configured types, the legacy per-chunk ``caused_by``
        scalar is also consulted as a fallback neighbor source for chunks
        that predate the edge table. If a chunk's current scalar disagrees
        with an older ``caused_by`` edge row (the row is stale -- rewritten
        by a later write that the additive-only edge table never retracted),
        the scalar wins and the stale row is skipped for that chunk.
        """
        types = self._edge_expansion_types
        if not types or self._edge_max_hops < 1:
            return []
        caused_by_active = "caused_by" in types
        visited_ids = {chunk.chunk_id for chunk in chunks}
        expanded: dict[str, SubconsciousChunk] = {}
        frontier = chunks
        for _hop in range(self._edge_max_hops):
            frontier_ids = [chunk.chunk_id for chunk in frontier]
            if not frontier_ids:
                break
            frontier_relevance = {chunk.chunk_id: float(chunk.relevance) for chunk in frontier}
            scalar_targets: dict[str, str] = {}
            if caused_by_active:
                for chunk in frontier:
                    if chunk.caused_by and chunk.caused_by != chunk.chunk_id:
                        scalar_targets[chunk.chunk_id] = chunk.caused_by

            edge_rows = self.store.get_chunk_edges(frontier_ids, edge_types=types, direction="out")
            hop_candidates: dict[str, float] = {}
            covered_scalar_pairs: set[tuple[str, str]] = set()
            for edge in edge_rows:
                src, dst = edge.src_chunk_id, edge.dst_chunk_id
                if edge.edge_type == "caused_by":
                    scalar = scalar_targets.get(src)
                    if scalar is not None:
                        if scalar != dst:
                            continue  # stale edge row: current scalar disagrees, skip it
                        covered_scalar_pairs.add((src, dst))
                if dst in visited_ids:
                    continue
                referrer_relevance = frontier_relevance.get(src)
                if referrer_relevance is None:
                    continue
                weight = max(0.0, min(1.0, float(edge.weight)))
                inherited = referrer_relevance * self._edge_expansion_decay * weight
                if inherited > hop_candidates.get(dst, -1.0):
                    hop_candidates[dst] = inherited

            if caused_by_active:
                # Legacy fallback: chunks whose caused_by predates the edge
                # table (no matching edge row) still expand via the scalar.
                for src, dst in scalar_targets.items():
                    if (src, dst) in covered_scalar_pairs or dst in visited_ids:
                        continue
                    referrer_relevance = frontier_relevance.get(src)
                    if referrer_relevance is None:
                        continue
                    inherited = referrer_relevance * self._edge_expansion_decay
                    if inherited > hop_candidates.get(dst, -1.0):
                        hop_candidates[dst] = inherited

            if not hop_candidates:
                break
            # Bound the fetch so a fan-out of edges can't blow up the query.
            neighbor_ids = sorted(hop_candidates, key=lambda cid: -hop_candidates[cid])[: max(1, limit)]
            fetched = self.store.get_chunks_by_ids(neighbor_ids, as_of=as_of)
            next_frontier: list[SubconsciousChunk] = []
            for neighbor in fetched:
                if neighbor.chunk_id in visited_ids:
                    continue
                visited_ids.add(neighbor.chunk_id)
                updated = neighbor.model_copy(update={"relevance": hop_candidates[neighbor.chunk_id]})
                expanded[neighbor.chunk_id] = updated
                next_frontier.append(updated)
            frontier = next_frontier

        return list(expanded.values())

    @staticmethod
    def _superseded_ids(chunks: list[SubconsciousChunk]) -> set[str]:
        """Collect chunk ids that any present chunk declares it supersedes.

        ``supersedes`` is a single id on manual writes and a JSON list of ids
        after consolidation; both shapes are handled.
        """
        superseded: set[str] = set()
        for chunk in chunks:
            raw = chunk.supersedes
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                superseded.add(str(raw))
                continue
            if isinstance(parsed, list):
                superseded.update(str(item) for item in parsed)
            else:
                superseded.add(str(parsed))
        return superseded

    def _suppress_superseded(
        self,
        recent_chunks: list[SubconsciousChunk],
        retrieved_chunks: list[SubconsciousChunk],
    ) -> tuple[list[SubconsciousChunk], list[SubconsciousChunk]]:
        """Drop candidates whose successor is already present in the set."""
        superseded = self._superseded_ids([*recent_chunks, *retrieved_chunks])
        if not superseded:
            return recent_chunks, retrieved_chunks
        recent = [chunk for chunk in recent_chunks if chunk.chunk_id not in superseded]
        retrieved = [chunk for chunk in retrieved_chunks if chunk.chunk_id not in superseded]
        return recent, retrieved

    def _dedupe_chunks(self, chunks: list[SubconsciousChunk]) -> list[SubconsciousChunk]:
        seen: set[str] = set()
        deduped: list[SubconsciousChunk] = []
        for chunk in chunks:
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            deduped.append(chunk)
        return deduped

    def _split_recent_and_retrieved_chunks(
        self,
        *,
        recent_chunks: list[SubconsciousChunk],
        retrieved_chunks: list[SubconsciousChunk],
        chunk_cap: int,
        budget: BudgetContext,
    ) -> list[SubconsciousChunk]:
        recent_budget = min(self._recent_slot_budget, chunk_cap)
        if budget.pressure == "critical" and chunk_cap > 1:
            recent_budget = min(recent_budget, 1)
        candidates: list[tuple[str, int, SubconsciousChunk]] = [
            ("recent", index, chunk) for index, chunk in enumerate(recent_chunks)
        ]
        candidates.extend(("retrieved", index, chunk) for index, chunk in enumerate(retrieved_chunks))
        ranked = sorted(candidates, key=lambda item: (-float(item[2].relevance), 0 if item[0] == "recent" else 1, item[1]))
        selected: list[SubconsciousChunk] = []
        seen_ids: set[str] = set()
        recent_count = 0
        for source, _, chunk in ranked:
            if chunk.chunk_id in seen_ids:
                continue
            if source == "recent":
                if recent_count >= recent_budget:
                    continue
                recent_count += 1
            selected.append(chunk)
            seen_ids.add(chunk.chunk_id)
            if len(selected) >= chunk_cap:
                break
        return selected

    def _mmr_select_chunks(
        self,
        chunks: list[SubconsciousChunk],
        *,
        query_text: str,
        diversity_lambda: float,
        chunk_cap: int,
    ) -> list[SubconsciousChunk]:
        return apply_mmr_selection(
            chunks,
            query_text=query_text,
            diversity_lambda=diversity_lambda,
            chunk_cap=chunk_cap,
        )

    def _fit_token_budget(
        self,
        *,
        conscious: ConsciousBlock,
        budget: BudgetContext,
        chunks: list[SubconsciousChunk],
        whispers: list[Whisper],
        max_tokens: int,
        query_text: str,
    ) -> tuple[list[SubconsciousChunk], list[Whisper]]:
        fitted_whispers = self._fit_whispers_to_budget(
            conscious=conscious,
            budget=budget,
            whispers=whispers,
            max_tokens=max_tokens,
        )
        # Conscious/budget/whisper text is invariant across the chunk-fitting
        # loop below, and each already-fitted chunk's entry doesn't change
        # once encoded — so both are encoded once and reused instead of
        # re-encoding the whole growing context on every candidate chunk.
        conscious_text = self.encoder._encode_conscious(conscious)
        budget_text = self.encoder._encode_budget(budget)
        whispers_text = self.encoder._encode_whispers(fitted_whispers, now=None) if fitted_whispers else None

        fitted_chunks: list[SubconsciousChunk] = []
        fitted_entries: list[str] = []
        for chunk in chunks:
            entry = self.encoder._encode_chunk_entry(chunk)
            candidate = self.encoder.assemble_from_parts(
                conscious_text=conscious_text,
                chunk_entries=[*fitted_entries, entry],
                whispers_text=whispers_text,
                budget_text=budget_text,
            )
            if estimate_tokens(candidate) <= max_tokens:
                fitted_chunks.append(chunk)
                fitted_entries.append(entry)
                continue
            distilled = self._distill_for_budget(
                conscious_text=conscious_text,
                budget_text=budget_text,
                whispers_text=whispers_text,
                fitted_entries=fitted_entries,
                chunk=chunk,
                max_tokens=max_tokens,
                query_text=query_text,
            )
            if distilled is not None:
                fitted_chunks.append(distilled)
                fitted_entries.append(self.encoder._encode_chunk_entry(distilled))
        return fitted_chunks, fitted_whispers

    def _distill_for_budget(
        self,
        *,
        conscious_text: str,
        budget_text: str,
        whispers_text: str | None,
        fitted_entries: list[str],
        chunk: SubconsciousChunk,
        max_tokens: int,
        query_text: str,
    ) -> SubconsciousChunk | None:
        if not self._distillation_enabled:
            return None
        if estimate_tokens(chunk.content) < self._distillation_min_chunk_tokens:
            return None
        base_context = self.encoder.assemble_from_parts(
            conscious_text=conscious_text,
            chunk_entries=fitted_entries,
            whispers_text=whispers_text,
            budget_text=budget_text,
        )
        remaining = max_tokens - estimate_tokens(base_context)
        if remaining <= 0:
            return None
        distilled_content = distill_chunk(
            chunk.content,
            query_text,
            max_tokens=max(1, remaining),
        )
        if not distilled_content or distilled_content == chunk.content:
            return None
        distilled = chunk.model_copy(update={"content": distilled_content, "distilled": True})
        candidate = self.encoder.assemble_from_parts(
            conscious_text=conscious_text,
            chunk_entries=[*fitted_entries, self.encoder._encode_chunk_entry(distilled)],
            whispers_text=whispers_text,
            budget_text=budget_text,
        )
        if estimate_tokens(candidate) <= max_tokens:
            return distilled
        return None

    def _fit_whispers_to_budget(
        self,
        *,
        conscious: ConsciousBlock,
        budget: BudgetContext,
        whispers: list[Whisper],
        max_tokens: int,
    ) -> list[Whisper]:
        if not whispers:
            return []
        conscious_text = self.encoder._encode_conscious(conscious)
        budget_text = self.encoder._encode_budget(budget)
        now = time.time()
        fitted: list[Whisper] = []
        fitted_entries: list[str] = []
        reserve = max(1, max_tokens // 4)
        for whisper in whispers:
            entry = self.encoder._encode_whisper_entry(whisper, now=now)
            candidate_entries = [*fitted_entries, entry]
            whisper_text = "\n".join(["[NCP:WHISPERS]", *candidate_entries])
            full_context = self.encoder.assemble_from_parts(
                conscious_text=conscious_text,
                chunk_entries=[],
                whispers_text=whisper_text,
                budget_text=budget_text,
            )
            if estimate_tokens(whisper_text) <= reserve and estimate_tokens(full_context) <= max_tokens:
                fitted.append(whisper)
                fitted_entries.append(entry)
        return fitted

    def _assembly_caps(
        self,
        *,
        budget: BudgetContext,
        k: int | None,
    ) -> tuple[int, int]:
        if k is not None:
            return max(1, k), self._whisper_cap_default
        if budget.pressure == "critical":
            return self._chunk_cap_critical, self._whisper_cap_critical
        if budget.pressure == "high":
            return self._chunk_cap_high, self._whisper_cap_high
        return self._chunk_cap_default, self._whisper_cap_default

    def _alert_cap(self, *, budget: BudgetContext) -> int:
        if budget.pressure == "critical":
            return 1
        return 2

    def _write_with_retry(self, chunk: SubconsciousChunk, *, retries: int = 2, backoff_ms: int = 50) -> bool:
        # WI-007(a): return whether the chunk was actually persisted. A
        # deduplication-suppressed write returns False (deterministic — not
        # retried) so callers do not treat silent data loss as success.
        for attempt in range(retries + 1):
            try:
                return bool(self.store.write(chunk))
            except Exception:
                if attempt < retries:
                    time.sleep(backoff_ms / 1000)
                    continue
                raise RuntimeError(
                    f"Failed to persist chunk after {retries + 1} attempts: {chunk.chunk_id}"
                ) from None
        return False

    # ------------------------------------------------------------------
    # Async helpers for post_turn_async
    # ------------------------------------------------------------------

    async def _alog_turn_record(self, record: TurnRecord) -> None:
        await self.store.async_log_turn_record(record)

    async def _alog_conscious(self, conscious: ConsciousBlock, snapshot_hash: str) -> None:
        await self.store.async_log_conscious(conscious, snapshot_hash=snapshot_hash)

    async def _alog_cost(self, agent_id: str, response: NCPResponse) -> None:
        await self.store.async_log_cost(agent_id=agent_id, response=response)

    async def _aacknowledge_whispers(self, whisper_ids: list[str], agent_id: str) -> None:
        await self.store.async_acknowledge_whispers(whisper_ids, agent_id=agent_id)

    async def _alog_write_with_retry(self, chunk: SubconsciousChunk, *, retries: int = 2, backoff_ms: int = 50) -> bool:
        # WI-007(a): mirror the sync path — surface a dedup-suppressed write as
        # False rather than swallowing it as success.
        for attempt in range(retries + 1):
            try:
                return bool(await self.store.async_write(chunk))
            except Exception:
                if attempt < retries:
                    await anyio.sleep(backoff_ms / 1000)
                    continue
                raise RuntimeError(
                    f"Failed to persist chunk after {retries + 1} attempts: {chunk.chunk_id}"
                ) from None
        return False

    # ------------------------------------------------------------------
    # Drift feedback loop
    # ------------------------------------------------------------------

    def _apply_drift_feedback(
        self,
        conscious: ConsciousBlock,
        drained: list[Whisper],
    ) -> ConsciousBlock:
        for whisper in drained:
            if whisper.whisper_type != "world_check":
                continue
            try:
                payload = whisper.payload
                if isinstance(payload, str):
                    data = json.loads(payload)
                elif isinstance(payload, dict):
                    data = payload
                else:
                    continue
                # WI-007(c): a world_check without detected_drift must not zero
                # the agent's drift — skip it instead of defaulting to 0.0.
                if "detected_drift" not in data:
                    continue
                detected_drift = float(data["detected_drift"])
                if 0.0 <= detected_drift <= 1.0:
                    conscious = conscious.model_copy(update={"drift_score": detected_drift})
                    break
                # Out-of-range detected_drift is malformed input, not a
                # terminal signal -- skip it and keep checking the rest of
                # the drained whispers for a well-formed one.
                continue
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return conscious
