"""Shared helpers for the typed ``chunk_edges`` graph substrate.

Kept backend-agnostic so SQLiteStore, PgvectorStore, and AsyncPgvectorStore
derive identical ``ChunkEdge`` rows from the same legacy ``caused_by``/
``supersedes`` columns instead of drifting apart.
"""

from __future__ import annotations

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
