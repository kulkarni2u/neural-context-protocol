"""Batch / non-interactive JSONL processor for NCP operations."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from ncp.stores.base import BaseStore
from ncp.types import SubconsciousChunk, Whisper


def run_batch(
    operations: Iterable[dict[str, Any]],
    store: BaseStore,
    *,
    dry_run: bool = False,
    stop_on_error: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for op in operations:
        result = _dispatch(op, store, dry_run=dry_run)
        results.append(result)
        if stop_on_error and not result.get("ok", False):
            break
    return results


def _dispatch(
    op: dict[str, Any],
    store: BaseStore,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    op_name = op.get("op", "")
    if "error" in op and op.get("ok") is False:
        return dict(op)
    handler = _HANDLERS.get(op_name)
    if handler is None:
        return {"op": op_name, "ok": False, "error": "unknown op"}
    try:
        return handler(op, store, dry_run=dry_run)
    except Exception as exc:
        return {"op": op_name, "ok": False, "error": str(exc)}


def _handle_write_memory(
    op: dict[str, Any],
    store: BaseStore,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "content": op["content"],
        "layer": op["layer"],
        "src": op["src"],
        "written_by": op.get("written_by", "batch_agent"),
        "pipeline_id": op.get("pipeline_id"),
        "base_trust": op.get("base_trust", 0.7),
    }
    if "chunk_id" in op:
        kwargs["chunk_id"] = op["chunk_id"]
    chunk = SubconsciousChunk(**kwargs)
    if dry_run:
        return {"op": "write_memory", "ok": True, "chunk_id": chunk.chunk_id, "written": False}
    written = store.write(chunk)
    return {"op": "write_memory", "ok": True, "chunk_id": chunk.chunk_id, "written": written}


def _handle_emit_whisper(
    op: dict[str, Any],
    store: BaseStore,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    whisper = Whisper(
        from_agent=op["from_agent"],
        target=op["to"],
        whisper_type=op["whisper_type"],
        payload=op["payload"],
        confidence=op.get("confidence", 0.8),
        pipeline_id=op.get("pipeline_id"),
    )
    if not dry_run:
        store.emit_whisper(whisper)
    return {"op": "emit_whisper", "ok": True}


def _handle_query(
    op: dict[str, Any],
    store: BaseStore,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    k = op.get("k", 4)
    pipeline_id = op.get("pipeline_id")
    results = store.query(op["text"], k=k, pipeline_id=pipeline_id)
    serialized = [
        {
            "chunk_id": chunk.chunk_id,
            "relevance": chunk.relevance,
            "content": chunk.content,
            "layer": chunk.layer,
            "base_trust": chunk.base_trust,
        }
        for chunk in results
    ]
    return {"op": "query", "ok": True, "results": serialized}


def _handle_consolidate(
    op: dict[str, Any],
    store: BaseStore,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    pipeline_id = op.get("pipeline_id")
    effective_dry_run = dry_run or op.get("dry_run", False)
    report = store.consolidate(pipeline_id=pipeline_id, dry_run=effective_dry_run)
    return {
        "op": "consolidate",
        "ok": True,
        "merged": report.merged,
        "tombstoned": report.tombstoned,
        "clusters_scanned": report.clusters_scanned,
        "duration_seconds": report.duration_seconds,
    }


def _handle_calibrate(
    op: dict[str, Any],
    store: BaseStore,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    pipeline_id = op.get("pipeline_id")
    effective_dry_run = dry_run or op.get("dry_run", False)
    report = store.calibrate(pipeline_id=pipeline_id, dry_run=effective_dry_run)
    return {
        "op": "calibrate",
        "ok": True,
        "adjusted": report.adjusted,
        "protected": report.protected,
        "duration_seconds": report.duration_seconds,
    }


_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "write_memory": _handle_write_memory,
    "emit_whisper": _handle_emit_whisper,
    "query": _handle_query,
    "consolidate": _handle_consolidate,
    "calibrate": _handle_calibrate,
}
