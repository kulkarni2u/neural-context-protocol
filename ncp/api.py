"""Public API helpers for the NCP runtime."""

from __future__ import annotations

from pathlib import Path
import time

from ncp.adapters.base import BaseAdapter
from ncp.adapters.local import LocalAdapter
from ncp.assembler import Assembler
from ncp.config import NCPConfig, load_config
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
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
) -> NCPResponse:
    return NCPResponse(
        content=content,
        input_tokens=len((context + "\n" + turn).split()),
        output_tokens=len(content.split()),
        cost_usd=0.0,
        model=adapter.__class__.__name__.lower(),
        pipeline_id=agent.pipeline_id,
        turn_id=f"turn_{int(time.time() * 1000)}",
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
