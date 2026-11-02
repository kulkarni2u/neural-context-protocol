"""Shared SQL for the Postgres task ledger (sync and async stores).

Pure statement builders: each returns ``(sql, params)`` with ``%s``
placeholders and ``{schema}``/``{prefix}`` left for the store's ``_sql``. The
sync and async stores only differ in how they execute these, so keeping the
statements here stops the two implementations from drifting apart.

Spec: docs/specs/2026-11-02-ncp-coordination-primitives-design.md section 5.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from ncp.types import TaskRecord

TABLE = "{schema}.{prefix}tasks"

TASK_COLUMNS = (
    "task_id, pipeline_id, slot, title, status, created_by, owner, lease_token,"
    " lease_expires_at, heartbeat_at, attempt, max_attempts, dedupe_key,"
    " parent_task_id, origin_whisper_id, checkpoint_chunk_id, result_chunk_ids,"
    " result_turn_id, error, version, created_at, updated_at"
)

Statement = tuple[str, tuple[Any, ...]]


def row_to_task(row: Mapping[str, Any]) -> TaskRecord:
    chunks = row["result_chunk_ids"]
    if isinstance(chunks, (str, bytes)):
        try:
            chunks = json.loads(chunks)
        except ValueError:
            chunks = []
    return TaskRecord(
        task_id=row["task_id"],
        pipeline_id=row["pipeline_id"],
        slot=row["slot"],
        title=row["title"],
        status=row["status"],
        created_by=row["created_by"],
        owner=row["owner"],
        lease_token=row["lease_token"],
        lease_expires_at=None if row["lease_expires_at"] is None else float(row["lease_expires_at"]),
        heartbeat_at=None if row["heartbeat_at"] is None else float(row["heartbeat_at"]),
        attempt=int(row["attempt"]),
        max_attempts=int(row["max_attempts"]),
        dedupe_key=row["dedupe_key"],
        parent_task_id=row["parent_task_id"],
        origin_whisper_id=row["origin_whisper_id"],
        checkpoint_chunk_id=row["checkpoint_chunk_id"],
        result_chunk_ids=[str(c) for c in (chunks or [])],
        result_turn_id=row["result_turn_id"],
        error=row["error"],
        version=int(row["version"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def expire_stmt(now: float, pipeline_id: str | None, task_id: str | None) -> Statement:
    """Lazy expiry (spec 5.3). Returns every row it transitioned."""
    clauses = ["status = 'claimed'", "lease_expires_at < %s"]
    params: list[Any] = [now]
    if pipeline_id is not None:
        clauses.append("pipeline_id = %s")
        params.append(pipeline_id)
    if task_id is not None:
        clauses.append("task_id = %s")
        params.append(task_id)
    sql = (
        f"UPDATE {TABLE} SET"
        " status = CASE WHEN attempt >= max_attempts THEN 'failed' ELSE 'open' END,"
        " owner = NULL, lease_token = NULL, lease_expires_at = NULL,"
        " error = CASE WHEN attempt >= max_attempts THEN 'lease_expired' ELSE error END,"
        " updated_at = %s, version = version + 1"
        f" WHERE {' AND '.join(clauses)} RETURNING {TASK_COLUMNS}"
    )
    return sql, (now, *params)


def insert_stmt(task: TaskRecord) -> Statement:
    """Insert; a ``(pipeline_id, dedupe_key)`` collision returns no row."""
    sql = (
        f"INSERT INTO {TABLE} ({TASK_COLUMNS})"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        " %s::jsonb, %s, %s, %s, %s, %s)"
        " ON CONFLICT (pipeline_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING"
        f" RETURNING {TASK_COLUMNS}"
    )
    return sql, (
        task.task_id, task.pipeline_id, task.slot, task.title, task.status,
        task.created_by, task.owner, task.lease_token, task.lease_expires_at,
        task.heartbeat_at, task.attempt, task.max_attempts, task.dedupe_key,
        task.parent_task_id, task.origin_whisper_id, task.checkpoint_chunk_id,
        json.dumps(task.result_chunk_ids), task.result_turn_id, task.error,
        task.version, task.created_at, task.updated_at,
    )


def select_by_dedupe_stmt(pipeline_id: str, dedupe_key: str) -> Statement:
    return (
        f"SELECT {TASK_COLUMNS} FROM {TABLE} WHERE pipeline_id = %s AND dedupe_key = %s",
        (pipeline_id, dedupe_key),
    )


def select_stmt(task_id: str, *, for_update: bool = False) -> Statement:
    lock = " FOR UPDATE" if for_update else ""
    return f"SELECT {TASK_COLUMNS} FROM {TABLE} WHERE task_id = %s{lock}", (task_id,)


def claim_stmt(
    *,
    agent_id: str,
    token: str,
    now: float,
    lease_seconds: float,
    task_id: str | None,
    pipeline_id: str | None,
    slot: str | None,
) -> Statement:
    """Atomic claim; the subquery uses ``FOR UPDATE SKIP LOCKED`` so concurrent
    claimers neither block each other nor pick the same row (spec 5.4)."""
    where = ["status = 'open'", "attempt < max_attempts"]
    params: list[Any] = []
    for column, value in (("task_id", task_id), ("pipeline_id", pipeline_id), ("slot", slot)):
        if value is not None:
            where.append(f"{column} = %s")
            params.append(value)
    sql = (
        f"UPDATE {TABLE} SET status = 'claimed', owner = %s, lease_token = %s,"
        " lease_expires_at = %s, heartbeat_at = %s, attempt = attempt + 1,"
        " updated_at = %s, version = version + 1"
        f" WHERE status = 'open' AND task_id = (SELECT task_id FROM {TABLE}"
        f" WHERE {' AND '.join(where)} ORDER BY created_at, task_id LIMIT 1"
        " FOR UPDATE SKIP LOCKED)"
        f" RETURNING {TASK_COLUMNS}"
    )
    return sql, (agent_id, token, now + float(lease_seconds), now, now, *params)


def owner_update_stmt(
    set_sql: str,
    set_params: Sequence[Any],
    *,
    task_id: str,
    lease_token: str,
    now: float,
) -> Statement:
    """Fenced owner mutation: zero rows back means the lease was lost."""
    sql = (
        f"UPDATE {TABLE} SET {set_sql}, updated_at = %s, version = version + 1"
        " WHERE task_id = %s AND status = 'claimed' AND lease_token = %s"
        f" AND lease_expires_at >= %s RETURNING {TASK_COLUMNS}"
    )
    return sql, (*set_params, now, task_id, lease_token, now)


def heartbeat_set(now: float, lease_seconds: float, checkpoint_chunk_id: str | None) -> tuple[str, tuple[Any, ...]]:
    return (
        "lease_expires_at = %s, heartbeat_at = %s,"
        " checkpoint_chunk_id = COALESCE(%s, checkpoint_chunk_id)",
        (now + float(lease_seconds), now, checkpoint_chunk_id),
    )


def release_set(error: str | None) -> tuple[str, tuple[Any, ...]]:
    bounded = None if error is None else error[:300]
    return (
        "status = CASE WHEN attempt >= max_attempts THEN 'failed' ELSE 'open' END,"
        " owner = NULL, lease_token = NULL, lease_expires_at = NULL,"
        " error = CASE WHEN attempt >= max_attempts"
        " THEN COALESCE(%s, 'max_attempts_exhausted') ELSE COALESCE(%s, error) END",
        (bounded, bounded),
    )


def complete_set(
    status: str, summary: str | None, result_chunk_ids: Sequence[str], turn_id: str | None
) -> tuple[str, tuple[Any, ...]]:
    error = summary[:300] if (status == "failed" and summary) else None
    return (
        "status = %s, lease_token = NULL, lease_expires_at = NULL,"
        " result_chunk_ids = %s::jsonb, result_turn_id = COALESCE(%s, result_turn_id),"
        " error = COALESCE(%s, error)",
        (status, json.dumps(list(result_chunk_ids)), turn_id, error),
    )


def cancel_stmt(task_id: str, reason: str | None, now: float) -> Statement:
    return (
        f"UPDATE {TABLE} SET status = 'cancelled', lease_token = NULL,"
        " lease_expires_at = NULL, error = COALESCE(%s, error),"
        " updated_at = %s, version = version + 1"
        f" WHERE task_id = %s RETURNING {TASK_COLUMNS}",
        (None if reason is None else reason[:300], now, task_id),
    )


def list_stmt(
    *,
    pipeline_id: str | None,
    status: str | None,
    slot: str | None,
    owner: str | None,
    limit: int,
) -> Statement:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("pipeline_id", pipeline_id),
        ("status", status),
        ("slot", slot),
        ("owner", owner),
    ):
        if value is not None:
            clauses.append(f"{column} = %s")
            params.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return (
        f"SELECT {TASK_COLUMNS} FROM {TABLE}{where} ORDER BY created_at, task_id LIMIT %s",
        (*params, max(1, min(50, int(limit)))),
    )
