"""Public API helpers for the NCP runtime."""

from __future__ import annotations

from pathlib import Path
import time
from uuid import uuid4

from ncp.adapters.base import BaseAdapter
from ncp.adapters.local import LocalAdapter
from ncp.assembler import Assembler
from ncp.config import NCPConfig, load_config
from ncp.costs import calculate_cost
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.tokens import estimate_tokens
from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk, Whisper

_CONFIG: NCPConfig | None = None


def configure(
    *,
    path: str | Path | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> NCPConfig:
    """Load and cache the active NCP configuration."""

    global _CONFIG
    _CONFIG = load_config(path=path, cwd=cwd, env=env)
    return _CONFIG


def agent(
    *,
    id: str,
    role: str,
    owns: list[str] | None = None,
    must_not: list[str] | None = None,
    task: str = "idle",
    slot: str = "unassigned",
    intent: str = "maintain_context",
    **overrides: object,
) -> ConsciousBlock:
    """Create a conscious-block template through the public API."""

    payload = {
        "agent_id": id,
        "role": role,
        "owns": owns or [],
        "must_not": must_not or [],
        "task": task,
        "slot": slot,
        "intent": intent,
        **overrides,
    }
    return ConsciousBlock(**payload)


def get_context(
    *,
    agent: ConsciousBlock,
    budget: BudgetContext | None = None,
    query_text: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    k: int | None = None,
    diversity_limit: int | None = None,
    max_tokens: int | None = None,
) -> str:
    """Assemble raw pidgin context for one agent turn."""

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    assembler = Assembler(store=resolved_store, config=resolved_config)
    return assembler.assemble(
        conscious=agent,
        budget=budget or BudgetContext(),
        query_text=query_text,
        k=k,
        diversity_limit=diversity_limit,
        max_tokens=max_tokens,
    ).context


def write_memory(
    chunk: SubconsciousChunk,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> bool:
    """Persist one chunk through the public API."""

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    return resolved_store.write(chunk)


def emit(
    whisper: Whisper,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> str:
    """Persist one whisper and return its whisper_id for delivery tracking."""

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    resolved_store.emit_whisper(whisper)
    return whisper.whisper_id


def run(
    *,
    agent: ConsciousBlock,
    turn: str,
    adapter: BaseAdapter | None = None,
    budget: BudgetContext | None = None,
    query_text: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    k: int | None = None,
    diversity_limit: int | None = None,
    max_tokens: int | None = None,
) -> NCPResponse:
    """Run one blocking local-runtime call through an adapter."""

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    resolved_budget = budget or BudgetContext()
    resolved_adapter = adapter or LocalAdapter()
    assembler = Assembler(store=resolved_store, config=resolved_config)

    start = time.perf_counter()
    assembly = assembler.assemble(
        conscious=agent,
        budget=resolved_budget,
        query_text=query_text or turn,
        ctx_window=resolved_adapter.ctx_window,
        k=k,
        diversity_limit=diversity_limit,
        max_tokens=max_tokens,
    )
    content = resolved_adapter.call(assembly.context, turn)
    response = _build_response(
        agent=agent,
        adapter=resolved_adapter,
        context=assembly.context,
        turn=turn,
        content=content,
        start=start,
        config=resolved_config,
    )
    assembler.post_turn(
        conscious=agent,
        response=response,
        result_summary=content.splitlines()[0] if content else "",
        result_full=content,
    )
    return response


def stream(
    *,
    agent: ConsciousBlock,
    turn: str,
    adapter: BaseAdapter | None = None,
    budget: BudgetContext | None = None,
    query_text: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    k: int | None = None,
    diversity_limit: int | None = None,
    max_tokens: int | None = None,
):
    """Yield a streamed response through the adapter."""

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    resolved_budget = budget or BudgetContext()
    resolved_adapter = adapter or LocalAdapter()
    assembler = Assembler(store=resolved_store, config=resolved_config)
    assembly = assembler.assemble(
        conscious=agent,
        budget=resolved_budget,
        query_text=query_text or turn,
        ctx_window=resolved_adapter.ctx_window,
        k=k,
        diversity_limit=diversity_limit,
        max_tokens=max_tokens,
    )
    start = time.perf_counter()
    chunks: list[str] = []
    for chunk in resolved_adapter.stream(assembly.context, turn):
        chunks.append(chunk)
        yield chunk

    content = "".join(chunks)
    response = _build_response(
        agent=agent,
        adapter=resolved_adapter,
        context=assembly.context,
        turn=turn,
        content=content,
        start=start,
        config=resolved_config,
    )
    assembler.post_turn(
        conscious=agent,
        response=response,
        result_summary=content.splitlines()[0] if content else "",
        result_full=content,
    )


def _build_response(
    *,
    agent: ConsciousBlock,
    adapter: BaseAdapter,
    context: str,
    turn: str,
    content: str,
    start: float,
    config: NCPConfig | None = None,
) -> NCPResponse:
    model = adapter.model_name
    usage = getattr(adapter, "last_usage", None)

    if usage is not None:
        # Real provider reported authoritative token counts: bill them.
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read_tokens = usage.cache_read_tokens
        cost_source = "measured"
        pricing = config.pricing if config is not None else None
        try:
            cost_usd = calculate_cost(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                pricing=pricing,
            ).total_cost_usd
        except KeyError:
            # Real tokens but no pricing entry for this model; leave cost unpriced.
            cost_usd = 0.0
    else:
        # Local/mock/in-process path: tokens are a chars/4 estimate, not
        # authoritative, and are never priced.
        input_tokens = estimate_tokens(context + "\n" + turn)
        output_tokens = estimate_tokens(content)
        cache_read_tokens = 0
        cost_usd = 0.0
        cost_source = "estimated"

    return NCPResponse(
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cost_usd=cost_usd,
        model=model,
        pipeline_id=agent.pipeline_id,
        turn_id=f"turn_{int(time.time() * 1000)}_{uuid4().hex[:8]}",
        latency_ms=int((time.perf_counter() - start) * 1000),
        cost_source=cost_source,
    )
