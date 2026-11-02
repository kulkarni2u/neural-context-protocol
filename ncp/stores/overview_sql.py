"""Shared SQL and row assembly for the Postgres ``overview_rows`` (sync and async).

Statement builders return ``(sql, params)`` with ``%s`` placeholders and
``{schema}``/``{prefix}`` left for the store's ``_sql``, like
``ncp/stores/task_sql.py``. The sync and async stores differ only in how they
execute the statements and read Redis, so the semantics live here once. They
mirror the SQLite implementation (``SQLiteStore.overview_rows``): the same
windowing, the same fixed 60s expiring-claim window, the same stale-open rule
(``updated_at``), and drift keyed by ``pipeline_id``.

Whisper facts (the viewer's inbox, the newest queued ``result``, whisper
activity) are not in SQL on Postgres: whispers live in Redis. The stores read
them through the coordination layer and pass them to :func:`select_pipelines`
and :func:`build_rows`.

Spec: docs/specs/2026-11-02-ncp-coordination-primitives-design.md section 6.5.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ncp.stores.overview_facts import (
    DISSENT_WINDOW_SECONDS,
    EXPIRING_CLAIM_SECONDS,
    OVERVIEW_TASK_STATUSES,
    choice_text,
    latest_drift,
    pick_last_result,
    wanted_pipeline,
)

Statement = tuple[str, tuple[Any, ...]]

CHUNKS = "{schema}.{prefix}chunks"
TURNS = "{schema}.{prefix}turn_records"
TASKS = "{schema}.{prefix}tasks"
DECISIONS = "{schema}.{prefix}decisions"
DISSENT = "{schema}.{prefix}dissent_log"
DRIFT = "{schema}.{prefix}drift_history"


def statements(
    *,
    now: float,
    cutoff: float | None,
    include_tasks: bool,
    stale_open_seconds: float,
) -> dict[str, Statement]:
    """All grouped statements for one overview call, keyed by fact name."""

    activity = (
        f"SELECT pipeline_id, MAX(created_at) AS ts FROM {CHUNKS} WHERE pipeline_id IS NOT NULL GROUP BY pipeline_id"
        f" UNION ALL SELECT pipeline_id, MAX(created_at) FROM {TURNS} WHERE pipeline_id IS NOT NULL GROUP BY pipeline_id"
        f" UNION ALL SELECT pipeline_id, MAX(created_at) FROM {DECISIONS} WHERE pipeline_id IS NOT NULL GROUP BY pipeline_id"
    )
    if include_tasks:
        activity += f" UNION ALL SELECT pipeline_id, MAX(updated_at) FROM {TASKS} GROUP BY pipeline_id"
    out: dict[str, Statement] = {
        "activity": (
            f"SELECT pipeline_id, MAX(ts) AS ts FROM ({activity}) AS a GROUP BY pipeline_id",
            (),
        ),
        "decisions": (
            f"SELECT DISTINCT ON (pipeline_id) pipeline_id, schema_id, choice FROM {DECISIONS}"
            " WHERE pipeline_id IS NOT NULL ORDER BY pipeline_id, created_at DESC, decision_id DESC",
            (),
        ),
        "drift": (
            f"SELECT DISTINCT ON (session_id) session_id, drift_score, ts FROM {DRIFT}"
            " ORDER BY session_id, ts DESC",
            (),
        ),
        "dissent": (
            f"SELECT c.pipeline_id, COUNT(*) AS n FROM {DISSENT} dl JOIN {CHUNKS} c ON c.chunk_id = dl.chunk_id"
            " WHERE dl.created_at >= %s AND c.pipeline_id IS NOT NULL GROUP BY c.pipeline_id",
            (now - DISSENT_WINDOW_SECONDS,),
        ),
    }
    if include_tasks:
        terminal_floor = -1.0e18 if cutoff is None else cutoff
        out["task_counts"] = (
            f"SELECT pipeline_id, status, COUNT(*) AS n FROM {TASKS}"
            " WHERE status IN ('open', 'claimed') OR updated_at >= %s GROUP BY pipeline_id, status",
            (terminal_floor,),
        )
        out["expiring"] = (
            f"SELECT pipeline_id, COUNT(*) AS n FROM {TASKS}"
            " WHERE status = 'claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at <= %s"
            " GROUP BY pipeline_id",
            (now + EXPIRING_CLAIM_SECONDS,),
        )
        if stale_open_seconds > 0:
            out["stale"] = (
                f"SELECT pipeline_id, COUNT(*) AS n FROM {TASKS}"
                " WHERE status = 'open' AND updated_at <= %s GROUP BY pipeline_id",
                (now - stale_open_seconds,),
            )
        out["failed"] = (
            f"SELECT DISTINCT ON (pipeline_id) pipeline_id, error, updated_at FROM {TASKS}"
            " WHERE status = 'failed' ORDER BY pipeline_id, updated_at DESC, task_id DESC",
            (),
        )
    return out


def select_pipelines(
    activity_rows: Sequence[Mapping[str, Any]],
    whisper_activity: Mapping[str, float],
    *,
    wanted: set[str] | None,
    prefix: str | None,
    cutoff: float | None,
) -> dict[str, float]:
    """``{pipeline_id: last_activity}`` for pipelines inside the filter and window."""

    merged: dict[str, float] = {}
    for r in activity_rows:
        merged[str(r["pipeline_id"])] = float(r["ts"])
    for pid, ts in whisper_activity.items():
        merged[pid] = max(merged.get(pid, 0.0), ts)
    return {
        pid: ts
        for pid, ts in merged.items()
        if (cutoff is None or ts >= cutoff) and wanted_pipeline(pid, wanted, prefix)
    }


def build_rows(
    activity: Mapping[str, float],
    facts: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    include_tasks: bool,
    viewer: str | None,
    inbox: Mapping[str, dict[str, int]],
    result_whisper: Mapping[str, tuple[str, str, float]],
) -> list[dict[str, Any]]:
    """Assemble the public row shape (see ``ncp.stores.overview_facts``) from fetched facts."""

    task_counts: dict[str, dict[str, int]] = {}
    for r in facts.get("task_counts", ()):
        counts = task_counts.setdefault(str(r["pipeline_id"]), dict.fromkeys(OVERVIEW_TASK_STATUSES, 0))
        counts[str(r["status"])] = int(r["n"])
    expiring = {str(r["pipeline_id"]): int(r["n"]) for r in facts.get("expiring", ())}
    stale = {str(r["pipeline_id"]): int(r["n"]) for r in facts.get("stale", ())}
    failed_task = {
        str(r["pipeline_id"]): (str(r["error"] or ""), float(r["updated_at"])) for r in facts.get("failed", ())
    }
    decisions = {
        str(r["pipeline_id"]): {"schema_id": str(r["schema_id"]), "choice": choice_text(r["choice"])}
        for r in facts.get("decisions", ())
    }
    drift_timeline = [
        {"session_id": str(r["session_id"]), "drift_score": float(r["drift_score"]), "ts": float(r["ts"])}
        for r in facts.get("drift", ())
    ]
    dissent = {str(r["pipeline_id"]): int(r["n"]) for r in facts.get("dissent", ())}

    rows: list[dict[str, Any]] = []
    for pid, ts in activity.items():
        row: dict[str, Any] = {"pipeline_id": pid, "last_activity": ts}
        if include_tasks:
            row["tasks"] = task_counts.get(pid) or dict.fromkeys(OVERVIEW_TASK_STATUSES, 0)
            row["expiring_claims"] = expiring.get(pid, 0)
            row["stale_open"] = stale.get(pid, 0)
        row["inbox"] = dict(inbox.get(pid, {}))
        row["last_result"] = pick_last_result(result_whisper.get(pid), failed_task.get(pid))
        row["last_decision"] = decisions.get(pid)
        row["drift"] = latest_drift(drift_timeline, pid, viewer)
        row["dissent_24h"] = dissent.get(pid, 0)
        rows.append(row)
    rows.sort(key=lambda r: (-r["last_activity"], r["pipeline_id"]))
    return rows
