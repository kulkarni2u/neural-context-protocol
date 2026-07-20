"""Shared helpers for the typed ``chunk_edges`` graph substrate.

Kept backend-agnostic so SQLiteStore, PgvectorStore, and AsyncPgvectorStore
derive identical ``ChunkEdge`` rows from the same legacy ``caused_by``/
``supersedes`` columns instead of drifting apart.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher
import json

from ncp.types import ChunkEdge, SubconsciousChunk


def parse_supersedes_ids(raw: str | None) -> list[str]:
    """Normalize ``SubconsciousChunk.supersedes`` into a list of chunk ids.

    ``supersedes`` is a single chunk_id on manual writes and a JSON-encoded
    list of ids after consolidation merges several losers into one keeper;
    both shapes are handled here (mirrors ``Assembler._superseded_ids``).
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [str(raw)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]


def resolve_caused_by_fallback(edges: list[ChunkEdge]) -> dict[str, str]:
    """Resolve a fallback ``caused_by`` parent per src chunk from edge rows.

    Used by multi-hop calibration propagation (WI-G3) and graph traversal
    (WI-G2) for chunks whose ``caused_by`` scalar is empty (e.g. edges added
    via the MCP ``edges`` arg without ever setting the legacy column) but
    that do have a ``caused_by`` edge row. Callers only consult this when the
    scalar is absent -- the scalar always wins when both exist, since the
    additive-only edge table can carry stale rows from an earlier rewrite
    (see ``backfill_edges_for_chunk``).

    A src with multiple candidate ``caused_by`` edges (fan-out, or several
    rewrites) resolves deterministically to the highest-weight edge, ties
    broken by the most recently created one.
    """
    best: dict[str, ChunkEdge] = {}
    for edge in edges:
        if edge.edge_type != "caused_by":
            continue
        current = best.get(edge.src_chunk_id)
        if current is None or (edge.weight, edge.created_at) > (current.weight, current.created_at):
            best[edge.src_chunk_id] = edge
    return {src: edge.dst_chunk_id for src, edge in best.items()}


def backfill_edges_for_chunk(chunk: SubconsciousChunk) -> list[ChunkEdge]:
    """Derive ``chunk_edges`` rows implied by a chunk's legacy scalar fields.

    Write-time backfill: the legacy ``caused_by``/``supersedes`` columns stay
    authoritative and are never removed -- this only mirrors them into the
    typed edge table so multi-hop traversal doesn't need to special-case the
    1-hop legacy shape. Self-referencing ids (a chunk naming itself) are
    dropped rather than raising, since they're a caller bug, not a graph
    edge, and shouldn't block the chunk write itself.
    """
    edges: list[ChunkEdge] = []
    if chunk.caused_by and chunk.caused_by != chunk.chunk_id:
        edges.append(
            ChunkEdge(
                src_chunk_id=chunk.chunk_id,
                dst_chunk_id=chunk.caused_by,
                edge_type="caused_by",
                created_by=chunk.written_by,
            )
        )
    for target in parse_supersedes_ids(chunk.supersedes):
        if target and target != chunk.chunk_id:
            edges.append(
                ChunkEdge(
                    src_chunk_id=chunk.chunk_id,
                    dst_chunk_id=target,
                    edge_type="supersedes",
                    created_by=chunk.written_by,
                )
            )
    return edges


def infer_edges_for_chunk(
    chunk: SubconsciousChunk,
    candidates: Sequence[SubconsciousChunk],
    *,
    threshold: float,
    max_edges: int,
    existing_dst_ids: Iterable[str] | None = None,
) -> list[ChunkEdge]:
    """Deterministically infer ``refines`` edges from recent chunk content (CAP-C7/WI-P2).

    No model calls: a plain ``difflib.SequenceMatcher`` ratio over ``content``
    decides candidacy. ``candidates`` is typically a bounded, most-recent-first
    scan of chunks from the same pipeline (``[graph].infer_scan_limit``) -- this
    function owns every exclusion rule so callers only need to hand through the
    raw candidate list plus what already has a ``refines`` edge from ``chunk``:
    self-references, low-trust ``raw_*`` backup chunks (see ``ncp_write_memory``'s
    reversible ``raw_ref`` filtering), empty content, and (src, dst, "refines")
    pairs that already exist. Ties in ratio break on ``dst_chunk_id`` so the
    result is reproducible across runs given the same inputs.
    """
    if max_edges <= 0 or not chunk.content:
        return []
    already = set(existing_dst_ids or ())
    scored: list[tuple[float, SubconsciousChunk]] = []
    for candidate in candidates:
        if candidate.chunk_id == chunk.chunk_id:
            continue
        if candidate.chunk_id.startswith("raw_"):
            continue
        if not candidate.content:
            continue
        if candidate.chunk_id in already:
            continue
        ratio = SequenceMatcher(None, chunk.content, candidate.content).ratio()
        if ratio >= threshold:
            scored.append((ratio, candidate))
    scored.sort(key=lambda pair: (-pair[0], pair[1].chunk_id))
    return [
        ChunkEdge(
            src_chunk_id=chunk.chunk_id,
            dst_chunk_id=candidate.chunk_id,
            edge_type="refines",
            weight=ratio,
            created_by="ncp:inferred",
        )
        for ratio, candidate in scored[:max_edges]
    ]
