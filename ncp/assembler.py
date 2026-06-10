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
from ncp.encoder import PidginEncoder
from ncp.middleware.base import MiddlewarePipeline
from ncp.stores.base import BaseStore
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
    ) -> tuple[
        ConsciousBlock,
        BudgetContext,
        list[SubconsciousChunk],
        list[Whisper],
        list[tuple[str, float]],
        list[tuple[str, float]],
    ]:
        conscious, budget = self.middleware.pre_assemble(conscious, budget)
        coherence_report = self.coherence.check(conscious)
        coherence_alerts = coherence_report.alerts
        hydrated = conscious if ctx_window is None else conscious.model_copy(update={"ctx_window": ctx_window})
        chunk_cap, whisper_cap = self._assembly_caps(budget=budget, k=k)
        recent_chunks = self._resolve_recent_refs(hydrated)
        subconscious = self._retrieve_chunks(
            hydrated,
            query_text=query_text,
            budget=budget,
            k=chunk_cap,
            diversity_limit=diversity_limit,
        )
        subconscious = self._cold_start_bootstrap(hydrated, subconscious)
        deduped_chunks = self._dedupe_chunks([*recent_chunks, *subconscious])
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
        return hydrated, budget, combined_chunks, combined_whispers, evicted_high_relevance, evicted_whispers

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
    ) -> AssemblyResult:
        hydrated, budget, combined_chunks, combined_whispers, evicted_high_relevance, evicted_whispers = self._prepare_assembly(
            conscious=conscious,
            budget=budget,
            query_text=query_text,
            ctx_window=ctx_window,
            k=k,
            diversity_limit=diversity_limit,
            max_tokens=max_tokens,
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
        hydrated, budget, combined_chunks, combined_whispers, _, _ = self._prepare_assembly(
            conscious=conscious,
            budget=budget,
            query_text=query_text,
            ctx_window=ctx_window,
            k=k,
            diversity_limit=diversity_limit,
            max_tokens=max_tokens,
        )

        conscious_text = self.encoder._encode_conscious(hydrated)
        yield "conscious", conscious_text

        if combined_chunks:
            yield "subconscious", self.encoder._encode_subconscious(combined_chunks)

        if combined_whispers:
            yield "whispers", self.encoder._encode_whispers(combined_whispers, now=None)

        budget_text = self.encoder._encode_budget(budget)
        yield "budget_header", budget_text

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

        for chunk in memory_chunks or []:
            chunk = self.middleware.pre_write(chunk)
            self._write_with_retry(chunk)

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

        async with anyio.create_task_group() as tg:
            tg.start_soon(self._alog_turn_record, record)
            tg.start_soon(self._alog_conscious, updated_conscious, snapshot_hash)
            tg.start_soon(self._alog_cost, conscious.agent_id, response)
            if ack_whisper_ids:
                tg.start_soon(self._aacknowledge_whispers, ack_whisper_ids, conscious.agent_id)
            for chunk in memory_chunks or []:
                chunk = self.middleware.pre_write(chunk)
                tg.start_soon(self._alog_write_with_retry, chunk)

        return record

    # ------------------------------------------------------------------
    # Step 2: resolve recent refs
    # ------------------------------------------------------------------

    def _resolve_recent_refs(self, conscious: ConsciousBlock) -> list[SubconsciousChunk]:
        chunks: list[SubconsciousChunk] = []
        for ref in conscious.recent:
            record = self.store.resolve_recent_ref(ref)
            if record is None:
                continue
            chunks.append(
                SubconsciousChunk(
                    chunk_id=f"recent_{record.turn_id}",
                    layer="episodic",
                    content=record.result,
                    src="subcon_retrieved",
                    pipeline_id=record.pipeline_id,
                    written_by=record.agent_id,
                    relevance=1.0,
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
    ) -> list[SubconsciousChunk]:
        if k is None:
            k = 2 if (budget is not None and budget.pressure == "critical") else 4
        search_text = query_text or f"{conscious.task} {conscious.slot}"
        extra: dict = {}
        if diversity_limit is not None:
            extra["diversity_limit"] = diversity_limit
        return self.store.query(
            search_text,
            k=k,
            pipeline_id=conscious.pipeline_id,
            zone="working",
            fallback_to_trust_recency=True,
            **extra,
        )

    def _cold_start_bootstrap(
        self,
        conscious: ConsciousBlock,
        chunks: list[SubconsciousChunk],
    ) -> list[SubconsciousChunk]:
        if chunks:
            return chunks
        cold_chunk = SubconsciousChunk(
            chunk_id=f"cold_{conscious.pipeline_id or 'init'}",
            layer="procedural",
            content=f"pipeline_summary agent:{conscious.agent_id} task:{conscious.task} intent:{conscious.intent}",
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
        recent_kept = recent_chunks[:recent_budget]
        kept_ids = {chunk.chunk_id for chunk in recent_kept}
        retrieved_kept = [chunk for chunk in retrieved_chunks if chunk.chunk_id not in kept_ids]
        retrieved_slots = max(0, chunk_cap - len(recent_kept))
        return [*recent_kept, *retrieved_kept[:retrieved_slots]]

    def _fit_token_budget(
        self,
        *,
        conscious: ConsciousBlock,
        budget: BudgetContext,
        chunks: list[SubconsciousChunk],
        whispers: list[Whisper],
        max_tokens: int,
    ) -> tuple[list[SubconsciousChunk], list[Whisper]]:
        fitted_whispers = self._fit_whispers_to_budget(
            conscious=conscious,
            budget=budget,
            whispers=whispers,
            max_tokens=max_tokens,
        )
        fitted_chunks: list[SubconsciousChunk] = []
        for chunk in chunks:
            candidate_chunks = [*fitted_chunks, chunk]
            candidate = self.encoder.assemble(
                conscious=conscious,
                chunks=candidate_chunks,
                whispers=fitted_whispers,
                budget=budget,
            )
            if estimate_tokens(candidate) <= max_tokens:
                fitted_chunks.append(chunk)
        return fitted_chunks, fitted_whispers

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
        fitted: list[Whisper] = []
        reserve = max(1, max_tokens // 4)
        for whisper in whispers:
            candidate = [*fitted, whisper]
            whisper_text = self.encoder._encode_whispers(candidate, now=None)
            full_context = self.encoder.assemble(
                conscious=conscious,
                chunks=[],
                whispers=candidate,
                budget=budget,
            )
            if estimate_tokens(whisper_text) <= reserve and estimate_tokens(full_context) <= max_tokens:
                fitted.append(whisper)
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

    def _write_with_retry(self, chunk: SubconsciousChunk, *, retries: int = 2, backoff_ms: int = 50) -> None:
        for attempt in range(retries + 1):
            try:
                self.store.write(chunk)
                return
            except Exception:
                if attempt < retries:
                    time.sleep(backoff_ms / 1000)
                    continue
                raise RuntimeError(
                    f"Failed to persist chunk after {retries + 1} attempts: {chunk.chunk_id}"
                ) from None

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

    async def _alog_write_with_retry(self, chunk: SubconsciousChunk, *, retries: int = 2, backoff_ms: int = 50) -> None:
        for attempt in range(retries + 1):
            try:
                await self.store.async_write(chunk)
                return
            except Exception:
                if attempt < retries:
                    await anyio.sleep(backoff_ms / 1000)
                    continue
                raise RuntimeError(
                    f"Failed to persist chunk after {retries + 1} attempts: {chunk.chunk_id}"
                ) from None

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
                detected_drift = float(data.get("detected_drift", 0.0))
                if 0.0 <= detected_drift <= 1.0:
                    conscious = conscious.model_copy(update={"drift_score": detected_drift})
                break
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return conscious
