"""Shared helpers for ``overview_rows`` (cross-pipeline overview, capability D).

Holds the constants and pure functions every store backend uses to build the
per-pipeline raw-fact rows, plus the generic ``BaseStore`` implementation that
composes only public store methods.

Row shape (all keys always present unless noted)::

    {
      "pipeline_id": str,
      "last_activity": float,
      "tasks": {"open": n, "claimed": n, "done": n, "failed": n, "cancelled": n},
                                             # omitted when the ledger is unavailable
      "expiring_claims": int,                # omitted with "tasks"
      "stale_open": int,                     # omitted with "tasks"
      "inbox": {whisper_type: count},        # viewer's undelivered whispers; {} without a viewer
      "last_result": {"source": "whisper"|"task", "status": str,
                      "summary": str, "at": float} | None,
      "last_decision": {"schema_id": str, "choice": str} | None,
      "drift": float | None,
      "dissent_24h": int,
    }

``last_result.summary`` and ``last_decision.choice`` are agent-written DATA.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ncp.stores.base import NCPStoreError
from ncp.types import DecisionRecord, TaskRecord

if TYPE_CHECKING:
    from ncp.stores.base import BaseStore

# A claim is "expiring" when its lease ends within this many seconds. A fixed
# window (rather than 20% of each lease) keeps the SQL grouped and the rule
# explainable; with the default 300s lease it equals 20%.
EXPIRING_CLAIM_SECONDS = 60.0
# Same confidence floor ncp_get_context / peek use for non-exempt whispers.
INBOX_MIN_CONFIDENCE = 0.60
DISSENT_WINDOW_SECONDS = 86400.0
OVERVIEW_TASK_STATUSES = ("open", "claimed", "done", "failed", "cancelled")
RESULT_SUMMARY_MAX = 300


def parse_result_payload(payload: str) -> tuple[str, str]:
    """Return ``(status, summary)`` from a ``result`` whisper payload (best effort)."""

    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return "result", str(payload)[:RESULT_SUMMARY_MAX]
    if not isinstance(data, dict):
        return "result", str(payload)[:RESULT_SUMMARY_MAX]
    return str(data.get("status") or "result"), str(data.get("summary") or "")[:RESULT_SUMMARY_MAX]


def choice_text(raw: object) -> str:
    """Stringify a decision ``choice`` (a JSON column in SQL stores)."""

    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def decision_fields(record: DecisionRecord) -> dict[str, str]:
    return {"schema_id": record.schema_id, "choice": choice_text(record.choice)}


def task_summary(
    tasks: Sequence[TaskRecord],
    *,
    now: float,
    stale_open_seconds: float,
    terminal_cutoff: float | None,
) -> dict[str, Any]:
    """Counts by status plus expiring/stale numbers from already-expired task rows.

    Open and claimed tasks always count. Terminal tasks (done, failed,
    cancelled) count only when updated at or after ``terminal_cutoff``, so old
    history does not keep a pipeline flagged.
    """

    counts = dict.fromkeys(OVERVIEW_TASK_STATUSES, 0)
    expiring = 0
    stale = 0
    for task in tasks:
        if task.status in ("done", "failed", "cancelled") and terminal_cutoff is not None:
            if task.updated_at < terminal_cutoff:
                continue
        counts[task.status] = counts.get(task.status, 0) + 1
        if task.status == "claimed" and task.lease_expires_at is not None:
            if task.lease_expires_at <= now + EXPIRING_CLAIM_SECONDS:
                expiring += 1
        elif task.status == "open" and stale_open_seconds > 0:
            if task.updated_at <= now - stale_open_seconds:
                stale += 1
    return {"tasks": counts, "expiring_claims": expiring, "stale_open": stale}


def pick_last_result(
    whisper: tuple[str, str, float] | None,
    failed_task: tuple[str, float] | None,
) -> dict[str, Any] | None:
    """Newest of a result whisper ``(status, summary, at)`` and a failed-task ``(error, at)``."""

    if whisper is not None and (failed_task is None or whisper[2] >= failed_task[1]):
        return {"source": "whisper", "status": whisper[0], "summary": whisper[1], "at": whisper[2]}
    if failed_task is not None:
        return {
            "source": "task",
            "status": "failed",
            "summary": (failed_task[0] or "")[:RESULT_SUMMARY_MAX],
            "at": failed_task[1],
        }
    return None


def inbox_counts(whispers: Sequence[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for whisper in whispers:
        counts[whisper.whisper_type] = counts.get(whisper.whisper_type, 0) + 1
    return counts


def wanted_pipeline(pipeline_id: str, pipelines: set[str] | None, prefix: str | None) -> bool:
    if pipelines is not None and pipeline_id not in pipelines:
        return False
    return prefix is None or pipeline_id.startswith(prefix)


def latest_drift(
    timeline: Sequence[dict[str, Any]], pipeline_id: str, viewer: str | None
) -> float | None:
    """Newest reading whose session is ``pipeline_id`` or ``pipeline_id:viewer``.

    The coherence sensor logs ``session_id = pipeline_id``; the viewer-scoped
    key is accepted too so a future per-agent writer is picked up unchanged.
    """

    keys = {pipeline_id}
    if viewer:
        keys.add(f"{pipeline_id}:{viewer}")
    best: tuple[float, float] | None = None
    for entry in timeline:
        if entry.get("session_id") in keys:
            ts = float(entry.get("ts", 0.0))
            if best is None or ts > best[0]:
                best = (ts, float(entry["drift_score"]))
    return None if best is None else best[1]


def generic_overview_rows(
    store: BaseStore,
    *,
    pipelines: Sequence[str] | None = None,
    pipeline_prefix: str | None = None,
    since: float | None = None,
    viewer: str | None = None,
    now: float | None = None,
    include_tasks: bool = True,
    stale_open_seconds: float = 1800.0,
) -> list[dict[str, Any]]:
    """Compose ``overview_rows`` from public store methods only.

    Works on any backend but is bounded by what those methods expose: pipelines
    are discovered through ``viz_data()['pipeline_summary']`` (chunk-bearing
    pipelines only, unless ``pipelines`` is given), task lists are capped at 50
    per status, ``list_whispers``/``list_turns`` return nothing on backends
    without them (so ``last_result`` falls back to failed tasks), and
    ``dissent_24h`` is 0 because no public method exposes dissent timestamps.
    """

    now = time.time() if now is None else float(now)
    cutoff = None if since is None else now - float(since)
    wanted = None if pipelines is None else set(pipelines)

    activity: dict[str, float] = {}
    if wanted is not None:
        activity = dict.fromkeys(wanted, 0.0)
    try:
        summary = store.viz_data().get("pipeline_summary", [])
    except Exception:
        summary = []
    for item in summary if isinstance(summary, list) else []:
        pid = item.get("pipeline_id")
        if pid:
            activity[str(pid)] = max(activity.get(str(pid), 0.0), float(item.get("last_activity") or 0.0))

    try:
        drift_timeline = list(store.trust_drift_data().get("drift_timeline", []))  # type: ignore[call-overload]
    except Exception:
        drift_timeline = []

    tasks_ok = include_tasks
    rows: list[dict[str, Any]] = []
    for pid in sorted(activity):
        if not wanted_pipeline(pid, wanted, pipeline_prefix):
            continue
        last = activity[pid]
        row: dict[str, Any] = {"pipeline_id": pid}

        try:
            whisper_rows = store.list_whispers(pipeline_id=pid, limit=100)
        except Exception:
            whisper_rows = []
        if whisper_rows:
            last = max(last, max(float(w["created_at"]) for w in whisper_rows))
        try:
            turn_rows = store.list_turns(pipeline_id=pid, limit=50)
        except Exception:
            turn_rows = []
        if turn_rows:
            last = max(last, max(float(t.created_at) for t in turn_rows))

        failed_task: tuple[str, float] | None = None
        if tasks_ok:
            try:
                store.expire_task_leases(pid, now)
                task_rows: list[TaskRecord] = []
                for status in OVERVIEW_TASK_STATUSES:
                    task_rows.extend(store.list_tasks(pipeline_id=pid, status=status, limit=50, now=now))  # type: ignore[arg-type]
            except NCPStoreError:
                tasks_ok = False
            else:
                if task_rows:
                    last = max(last, max(t.updated_at for t in task_rows))
                row.update(
                    task_summary(
                        task_rows, now=now, stale_open_seconds=stale_open_seconds, terminal_cutoff=cutoff
                    )
                )
                failed = [t for t in task_rows if t.status == "failed"]
                if failed:
                    newest = max(failed, key=lambda t: t.updated_at)
                    failed_task = (newest.error or "", newest.updated_at)

        if cutoff is not None and last < cutoff:
            continue
        row["last_activity"] = last

        row["inbox"] = {}
        if viewer:
            try:
                row["inbox"] = inbox_counts(
                    store.peek_whispers(
                        agent_id=viewer,
                        pipeline_id=pid,
                        max_items=200,
                        min_confidence=INBOX_MIN_CONFIDENCE,
                    )
                )
            except Exception:
                row["inbox"] = {}

        result_whisper: tuple[str, str, float] | None = None
        for w in whisper_rows:  # newest first
            if w.get("whisper_type") == "result":
                status, text = parse_result_payload(str(w.get("payload", "")))
                result_whisper = (status, text, float(w["created_at"]))
                break
        row["last_result"] = pick_last_result(result_whisper, failed_task)

        try:
            decisions = store.query_decisions(pipeline_id=pid, k=1)
        except Exception:
            decisions = []
        row["last_decision"] = decision_fields(decisions[0]) if decisions else None
        row["drift"] = latest_drift(drift_timeline, pid, viewer)
        row["dissent_24h"] = 0
        rows.append(row)

    if not tasks_ok:
        for row in rows:
            for key in ("tasks", "expiring_claims", "stale_open"):
                row.pop(key, None)
    rows.sort(key=lambda r: (-r["last_activity"], r["pipeline_id"]))
    return rows
