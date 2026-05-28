"""Assembly pipeline for conscious state, retrieved chunks, and whispers.

Implements the normative 7-step assembly sequence from NCP spec §6.1.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
import time

import anyio

from ncp.coherence import CoherenceChecker
from ncp.encoder import PidginEncoder
from ncp.middleware.base import MiddlewarePipeline
from ncp.stores.base import BaseStore
from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


@dataclass(slots=True)
class AssemblyResult:
    """Structured result from one assembly pass."""

    context: str
    conscious: ConsciousBlock
    chunks: list[SubconsciousChunk]
    whispers: list[Whisper]


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
    ) -> None:
        self.store = store
        self.encoder = encoder or PidginEncoder()
        self.coherence = CoherenceChecker(store=store)
        self.middleware = middleware or MiddlewarePipeline()

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
    ) -> tuple[ConsciousBlock, BudgetContext, list[SubconsciousChunk], list[Whisper]]:
        conscious, budget = self.middleware.pre_assemble(conscious, budget)
        coherence_report = self.coherence.check(conscious)
        coherence_alerts = coherence_report.alerts
        hydrated = conscious if ctx_window is None else conscious.model_copy(update={"ctx_window": ctx_window})
        recent_chunks = self._resolve_recent_refs(hydrated)
        subconscious = self._retrieve_chunks(hydrated, query_text=query_text, budget=budget)
        subconscious = self._cold_start_bootstrap(hydrated, subconscious)
        combined_chunks = self._dedupe_chunks([*recent_chunks, *subconscious])
        drained_whispers = self._drain_whispers(hydrated)
        combined_whispers: list[Whisper] = [*coherence_alerts, *drained_whispers]
        if budget.pressure == "critical":
            combined_chunks = combined_chunks[:2]
            combined_whispers = combined_whispers[:1]
        else:
            combined_chunks = combined_chunks[:4]
            combined_whispers = combined_whispers[:3]
        return hydrated, budget, combined_chunks, combined_whispers

    def assemble(
        self,
        *,
        conscious: ConsciousBlock,
        budget: BudgetContext,
        query_text: str | None = None,
        ctx_window: int | None = None,
    ) -> AssemblyResult:
        hydrated, budget, combined_chunks, combined_whispers = self._prepare_assembly(
            conscious=conscious,
            budget=budget,
            query_text=query_text,
            ctx_window=ctx_window,
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
        )

    def assemble_incremental(
        self,
        *,
        conscious: ConsciousBlock,
        budget: BudgetContext,
        query_text: str | None = None,
        ctx_window: int | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[tuple[str, str]]:
        """Yield (label, section_text) in priority order, enforcing max_tokens.

        Labels in order: ``budget_header``, ``conscious``, ``subconscious`` (one
        per chunk), ``whispers``. Budget and conscious sections are always emitted.
        Subconscious chunks stop yielding once max_tokens would be exceeded.
        Token count is estimated via word-split proxy (len(text.split())).

        Note: ``middleware.post_assemble`` is NOT called on yielded sections.
        Callers that use post_assemble middleware should apply it to the
        concatenated result: ``mw.post_assemble("\\n\\n".join(t for _, t in sections))``.
        """
        hydrated, budget, combined_chunks, combined_whispers = self._prepare_assembly(
            conscious=conscious,
            budget=budget,
            query_text=query_text,
            ctx_window=ctx_window,
        )

        tokens_used = 0

        budget_text = self.encoder._encode_budget(budget)
        tokens_used += len(budget_text.split())
        yield "budget_header", budget_text

        conscious_text = self.encoder._encode_conscious(hydrated)
        tokens_used += len(conscious_text.split())
        yield "conscious", conscious_text

        fitting_chunks: list[SubconsciousChunk] = []
        for chunk in combined_chunks:
            chunk_tokens = len(self.encoder._encode_subconscious([chunk]).split())
            if max_tokens is not None and tokens_used + chunk_tokens > max_tokens:
                break
            tokens_used += chunk_tokens
            fitting_chunks.append(chunk)

        if fitting_chunks:
            yield "subconscious", self.encoder._encode_subconscious(fitting_chunks)

        if combined_whispers:
            yield "whispers", self.encoder._encode_whispers(combined_whispers, now=None)

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
    ) -> list[SubconsciousChunk]:
        k = 2 if (budget is not None and budget.pressure == "critical") else 4
        search_text = query_text or f"{conscious.task} {conscious.slot}"
        return self.store.query(
            search_text,
            k=k,
            pipeline_id=conscious.pipeline_id,
            zone="working",
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

    def _drain_whispers(self, conscious: ConsciousBlock) -> list[Whisper]:
        return self.store.drain_whispers(
            agent_id=conscious.agent_id,
            pipeline_id=conscious.pipeline_id,
            max_items=3,
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
