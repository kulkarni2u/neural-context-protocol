from __future__ import annotations

from dataclasses import dataclass
import re

from ncp.chunker import filter_content
from ncp.config import NCPConfig, load_config
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.types import ChunkEdge, ChunkLayer, ChunkSource, ChunkType, SubconsciousChunk


@dataclass(slots=True)
class MemoryCompileResult:
    source_chunk: SubconsciousChunk
    atoms: list[SubconsciousChunk]
    edges: list[ChunkEdge]
    filtered: bool = False
    reduction_ratio: float = 0.0


def compile_memory(
    content: str,
    *,
    query: str | None = None,
    layer: ChunkLayer = "semantic",
    src: ChunkSource = "synthesis",
    written_by: str = "system",
    pipeline_id: str | None = None,
    chunk_type: ChunkType = "auto",
    max_atoms: int = 8,
) -> MemoryCompileResult:
    filtered = filter_content(content, content_type=chunk_type)
    source = SubconsciousChunk(
        layer="episodic",
        content=content.strip(),
        src="tool_result",
        written_by=written_by,
        pipeline_id=pipeline_id,
        chunk_type=chunk_type,
        base_trust=0.95,
    )
    atom_texts = _atom_texts(filtered.filtered, chunk_type=chunk_type, max_atoms=max_atoms)
    atoms = [
        SubconsciousChunk(
            layer=layer,
            content=text,
            src=src,
            written_by=written_by,
            pipeline_id=pipeline_id,
            chunk_type="prose",
            source_refs=[source.chunk_id],
            raw_ref=source.chunk_id,
            base_trust=0.7,
            generation=1,
        )
        for text in atom_texts
    ]
    edges = [
        ChunkEdge(src_chunk_id=atom.chunk_id, dst_chunk_id=source.chunk_id, edge_type="derived_from")
        for atom in atoms
    ]
    return MemoryCompileResult(
        source_chunk=source,
        atoms=atoms,
        edges=edges,
        filtered=filtered.was_filtered,
        reduction_ratio=filtered.reduction_ratio,
    )


def remember(
    content: str,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    dataset: str | None = None,
    pipeline_id: str | None = None,
    written_by: str = "system",
    layer: ChunkLayer = "semantic",
    src: ChunkSource = "synthesis",
    max_atoms: int = 8,
) -> MemoryCompileResult:
    resolved_config = config or load_config()
    resolved_store = store or create_store(resolved_config)
    result = compile_memory(
        content,
        layer=layer,
        src=src,
        written_by=written_by,
        pipeline_id=pipeline_id or dataset,
        max_atoms=max_atoms,
    )
    resolved_store.write(result.source_chunk)
    for atom in result.atoms:
        resolved_store.write(atom)
    if result.edges:
        resolved_store.add_chunk_edges(result.edges)
    return result


def recall(
    query: str,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    pipeline_id: str | None = None,
    k: int = 5,
    mode: str = "auto",
) -> list[SubconsciousChunk]:
    resolved_config = config or load_config()
    resolved_store = store or create_store(resolved_config)
    retrieval_mode = "hybrid" if mode in ("auto", "hybrid", "graph") else mode
    return resolved_store.query(
        query,
        k=k,
        layer="semantic",
        pipeline_id=pipeline_id,
        retrieval_mode=retrieval_mode,
    )


def improve(
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    pipeline_id: str | None = None,
) -> dict[str, object]:
    resolved_config = config or load_config()
    resolved_store = store or create_store(resolved_config)
    report = resolved_store.consolidate(pipeline_id=pipeline_id)
    return {"improved": True, "consolidated_groups": report.merged}


def _atom_texts(content: str, *, chunk_type: ChunkType, max_atoms: int) -> list[str]:
    if max_atoms < 1:
        return []
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", content) if s.strip()]
    seen: set[str] = set()
    atoms: list[str] = []
    for s in sentences:
        if not s or s in seen:
            continue
        seen.add(s)
        atoms.append(s)
        if len(atoms) >= max_atoms:
            break
    return atoms
